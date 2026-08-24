"""聊天驱动创作 Blueprint：维度感知多轮对话 + Action Card 落地 + 进度引导。

把"表单填空"创作模式升级为"边聊边写"：
  - 聊天时自动注入当前书的相关 bible 维度（AI 真懂你的书）
  - AI 回复中可产出结构化“落地卡片”，用户点确认即写入对应维度
  - AI 感知创作进度，主动引导下一步该做什么

所有新代码独立成模块，不增加 app.py 行数（架构门禁约束）。

接口：
  POST /api/ai/chat/smart                 维度感知流式聊天（注入 bible + 多轮 + Action Card）
  POST /api/ai/chat/smart/apply-card      采纳 Action Card，落地到维度
  GET  /api/books/<book_id>/ai/progress   创作进度地图（各维度完成度 + 建议下一步）
  GET  /api/books/<book_id>/ai/sessions   列出该书所有聊天会话
  POST /api/ai/sessions/<id>/messages     显式追加一条消息（用于落地的卡片回执）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from queue import Queue, Empty
from typing import Any, Dict, List, Optional  # 顶层显式导入，兼容CI高版本Python解释器

from flask import Blueprint, jsonify, request, Response, stream_with_context

chat_collab_bp = Blueprint('chat_collab', __name__)


# SSE 保活工具（SSE_HEARTBEAT_COMMENT / SSE_HB_INTERVAL_SEC / gw_stream_with_hb）
# 已抽到 sse_keepalive.py 独立模块，避免 chat_collab_bp.py 巨石继续增长（架构门禁约束）
from sse_keepalive import gw_stream_with_hb, SSE_HEARTBEAT_COMMENT, SSE_HB_INTERVAL_SEC, HEARTBEAT

# 会话消息持久化（load/_safe_save/断流抢救）已抽到 session_persist.py 独立模块
# （架构门禁约束：chat_collab_bp.py 行数禁止超过基线，新功能必须拆模块）
# 注意：session_persist.py 位于 backend 根目录（与 sse_keepalive.py 同级），
# 必须用绝对导入；`from .session_persist` 会在启动时 ModuleNotFoundError → Render 部署 503
from session_persist import (
    load_session_messages,
    _safe_save_session_messages,
    _save_partial_on_disconnect,
)


# ============================================================================
# 2026-08-21 按用户要求统一上调至 27000（宁可给足不留截断），不再区分维度档位；
# 模型实际输出上限低于 27000 时由 llm_gateway 自动适配：已知模型表预钳制 +
# 400 报错解析真实上限自学习（见 llm_gateway._KNOWN_OUTPUT_LIMITS）。
# ============================================================================
_DIM_MAX_TOKENS = 27000


def _dim_max_tokens(dim_key: str) -> int:
    """维度生成 max_tokens（用户要求统一 27000，防任何维度截断）。"""
    return _DIM_MAX_TOKENS


def _run_blocking_with_heartbeat(blocking_fn, sse_fn, extra_frames=None):
    """在线程里跑 blocking_fn()，主 generator 按 SSE_HB_INTERVAL_SEC 周期 yield 心跳，直到返回。
    - 先 yield 1 帧心跳立即占坑，再按间隔发后续心跳。
    - sse_fn(payload) -> str：用于发 data 帧；心跳是纯冒号注释帧，直接拼字符串。
    - extra_frames: 可选 list[str] 原始帧（含\\n\\n），在 blocking_fn 跑的过程中穿插发（用于进度通知）。
    - 返回：blocking_fn 的返回值。
    """
    result_box = []
    exc_box = []

    def _worker():
        try:
            result_box.append(blocking_fn())
        except Exception as e:
            exc_box.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    extra_iter = iter(extra_frames or [])
    while True:
        t.join(timeout=SSE_HB_INTERVAL_SEC)
        if not t.is_alive():
            break
        # 插 1 条原始帧再插心跳
        try:
            raw = next(extra_iter)
            yield raw
        except StopIteration:
            pass
        yield SSE_HEARTBEAT_COMMENT
    if exc_box:
        raise exc_box[0]
    return result_box[0] if result_box else None


# ----------------------------------------------------------------------------
# 会话隔离工具：严格按 book_id 校验 session 归属，防【串书记忆混乱】
# - 前端若把 A 书的 session_id 传给 B 书请求（URL/localStorage/state 残留常见）
#   会直接 load 其他书的历史对话（人设+剧情+正文）进 prompt → 污染新书
# - 策略：不匹配就丢弃旧 session，静默创建新 session 并清空历史（历史对话绝不跨书迁移）
# ----------------------------------------------------------------------------
def _get_or_create_session_for_book(session_id, book_id, scope='general', title='新会话'):
    """session 安全获取器：book_id 不匹配时创建新 session。永不跨书迁移历史。"""
    from app import db, AISession
    session = None
    if session_id:
        session = AISession.query.get(session_id)
        # ===== 隔离铁律：session.book_id != 当前请求 book_id → 坚决丢弃（防串书）=====
        if session is not None and str(getattr(session, 'book_id', None)) != str(book_id):
            session = None
    if not session:
        session = AISession(book_id=book_id, scope=scope, title=title[:30], messages_json='[]')
        db.session.add(session)
        db.session.commit()
    return session


# 滑窗上下文：保留最近 N 轮 + 系统提示，超出则保留首尾、中间摘要
MAX_HISTORY_ROUNDS = 8
# 单条消息最大字符（超长截断，防 token 爆炸）
MAX_MSG_CHARS = 2000

# ============================================================================
# 统一去AI味规则（正文写作/正文修改/去AI Tab 三处共用，保证口径一致）
# ============================================================================
DEAI_RULES = """【去AI味执行规则】
定位：去AI味操作手册。写作时主动规避，去AI时只做减法/替换，不做润色扩写。

【一、总则】
1. 去AI味 ≠ 润色。不改原意、不增剧情、不换风格。
2. 优先级：删 > 换 > 改写。能删半句解决的不重写整段。
3. 保留：事件顺序、因果关系、人物动机、设定信息、伏笔、作者已有语气、方言口癖、身份称呼、角色特有表达、清单步骤等整齐结构文本。
4. 禁止：不新增剧情/设定/观点/情绪/金句；不把所有句子都改成长句（冲突场景的爆发式密集短段=手机端阅读友好，允许保留；⚠️ 段数/段均长遵守【通用核心铁律·禁令5+铁律A段落≠碎句】，漫画分镜脚本化=必须主动合并；不能堆砌无信息短句凑字数）；不堆砌"嗯啊吧嘛"假装口语化；不把解释腔改文艺腔/抒情腔；不输出修改说明、自检清单、协作口吻。

【二、三大AI味识别与处理】
1. 解释腔（第一大AI味）
   - 识别关键词：这说明、这意味着、由此可见、换句话说、事实上、显然、本质上、归根结底、从某种意义上说、不得不说、毋庸置疑
   - 识别模式：动作后立刻旁白解释"他之所以这样，是因为…"；动作/表情已表情绪还要写"这让他感到…"；段尾升华成长/命运/人性/选择；同一因果讲两遍
   - 处理：①删无信息量解释句（换说法的=删）②必要因果压成短因果一句说完 ③交还行为：用动作/停顿/视线/语气/选择替代旁白 ④段尾总结改成具体后果/反应/环境变化
2. 对白AI味（第二大AI味）
   - 识别模式：角色像作者在解释剧情而非回应眼前人；自报动机/创伤/选择完整分析；对白硬塞世界观/规则/背景像说明书；冲突场景语气仍客气逻辑过顺；不同角色同句式同情绪；对白出现书面连接词"因此/然而/与此同时/换句话说"；**3句以上对白连续独立段不嵌动作/不夹反应（像剧本台词）**；角色语气同质化无区分度。
   - 处理：①保留必须剧情事实、关系变化 ②删角色不该明说的心理分析、主题总结、背景说明 ③调语气符合角色身份与当下情绪（不强行现代口语化）④制造缺口：用半句话/停顿/反问/打断替代完整解释 ⑤长对白拆短，中间用动作/停顿/对方插话承接 ⑥**对白同段/3人声纹差规则 详见【正文写作阶段行文规范·对白节】，去AI阶段按同口径补合并动作/打断点 ⑦群像对白声纹差维度=用词/句长/语气词至少1维/2人**。
   - 约束：角色越紧张/越隐瞒/越愤怒，越不该把话说完整漂亮；重要信息可留，但要像角色当下说出来的，不像作者塞的
3. 工整句式（第三大AI味）
   - 识别模式：连续多段长度接近像自动分块；连续句相同主谓结构/转折/因果；排比三连对仗过密（【通用核心铁律·禁令0排比三连清单】全口径命中，含同动作排比/更X更Y更Z力度堆砌）；段尾总总结点题升华；路标词密集"然而/同时/此外/更重要的是/总而言之"；每段都"观点句+展开解释+段尾总结"模板
   - 处理：①保顺序：事件/论证顺序不能乱 ②破模板：连续三段形状相似时至少调一段开头/长度/收束 ③删路标：能不用连接词直接删，用动作/后果/场景变化承接 ④调长短：短段中段厚段按内容需要交替，不为整齐均分 ⑤弱收束：段尾停在细节/动作/未说完余波上

【三、词表速查】
- 必删词（去AI阶段额外扩展=正文阶段行文规范的必删词超集）：一股、一抹、不由得、不禁、随即、旋即、仿佛、似乎、似乎在、缓缓、微微、淡淡、轻轻、静静地、默默地、不知不觉、若有所思、若有所悟
- 【必删句式·禁令0/排比三连/真相直白点破/对话漫画分镜病】全部按【通用核心铁律·禁令0】+【行文规范结尾钩子真相点破】+【行文规范段落合并对白同段】的全口径执行，识别清单与处理细则不再此处复述，写重复会让规则冗余杂乱。
- 必删路标词：然而、同时、此外、更重要的是、总而言之、换句话说
- 必删协作口吻：下面我们、希望这能帮助、作为AI
- 结尾禁用：总结性、升华性、点题性语句（成长、命运、人性、选择、未来、从此、那一刻等拔高词）

【四、执行流程（写完/改完后逐条过一遍·去AI阶段专属，按顺序过）】
1. 【先扫最高优先级·通用核心铁律禁令0】按禁令0+铁律A的完整清单执行（修正式否定句/排比三连/自我修正句/碎句残切=一律处理），识别/细则不再复述；最高优先级，先过再往下。
2. 扫解释腔关键词 → 删"这说明/这意味着/由此可见/换句话说/事实上/显然/本质上/归根结底/从某种意义上说" + 【三·必删词】
3. 扫段尾 → 段尾若是总结/升华/拔高，改成具体动作/画面/后果，或直接删
4. 扫章节结尾 → 【行文规范结尾钩子节】的全口径规则：禁总结升华+真相直白点破必改动态动作句收尾，细则不复述。
5. 扫段落合并 → 【行文规范段落合并节】+【通用核心铁律·禁令5+铁律A】的全口径规则：三同时合并/连续4句独立段合并/段均句数≤1.8/禁止残切碎句，细则不复述。
6. 扫对白 → 【行文规范对白节】的全口径规则：替作者说解释删/连续3句至少1句动作同段/3人以上声纹差，细则不复述；发现命中直接补动作/打断点。
7. 扫动作闭环 → 【行文规范黄金4型·动作链】全口径规则：每200-300字一个"小目标→决策→验证"，空转流水账必须补验证句或合并，细则不复述。
8. 扫句式工整度 → 连续三句同结构？连续三段同长度？→ 打散其中一处
9. 扫路标词 → 删不必要"然而/同时/此外/更重要的是/总而言之"
10. 通读自检 → 确认未新增内容、未换风格、只做减法/替换

【五、去AI阶段补充约束（通用铁律/冰山/人物/情节铁律见上方，此处不再复述）】
1. 极致模仿人的写作习惯，写得自然不做作。写事为主，景物一笔带过；比喻/拟人数量与禁词表遵守【通用核心铁律总则】（每千字≤3处 + 8大AI套话比喻词全禁）。
2. 段落句式指标（段均句数≤1.8/连续4句独立段合并）遵守【通用核心铁律·禁令5+铁律A段落≠碎句】，超标=按行文规范段落合并节主动合并，此处不再复述数值。
3. 去AI阶段**不新增不改动**：不改原意、不增剧情、不换风格、不加金句；只做减法/替换。
4. 人味注入：加入不完美细节（结巴/重复/打断/口误）、感官碎片（温度/气味/触感三选二叠，不用比喻词）、口语化表达（合着/整半天/好家伙/说白了…），删除冗余形容词和路标词。
5. 禁令0之前的"修正感句式=真人写法保留"旧结论已在【通用核心铁律·禁令0】中明确彻底推翻，所有否定-肯定修正句式一律禁绝，不再复述。

【六、输出契约】
- 只输出处理后的正文，不解释改了什么、为什么改，不要在文末附加字数统计或自检清单。
- 优先级铁律：人味 > 克制 > 流畅。""".strip()

# ----------------------------------------------------------------------------
# 【阶段隔离·规则拆分】不同阶段只注入该阶段需要的规则：
#   GENERAL_CORE_RULES（三阶段通用总则）/ CONCEPTION_EXTRA_RULES（构思 JSON 约束）
#   WRITING_STYLE_RULES（正文行文）/ DEAI_ONLY_RULES（去AI 专用）。
# DEAI 阶段不注入 WRITING_STYLE_RULES，避免禁词表/执行流程等大块内容重复 2 遍。
# ----------------------------------------------------------------------------

DEAI_ONLY_RULES = DEAI_RULES  # 别名：DEAI_RULES 就是"去AI阶段专用"的完整规则

GENERAL_CORE_RULES = """
【通用核心铁律·构思+正文+去AI三阶段通用】

0. 总则

· 所有输出（正文、大纲、设定、人物、世界观、伏笔）必须遵守本规则。
· 每章2300-2500字（中文汉字，含标点）。写事为主，景一笔带过；比喻/拟人每千字≤3处，必须贴合具体场景，严禁 8 大 AI 套话比喻词（宛如/犹如/恍若/宛若 + 大海/巨龙/深渊/星河，以及这 8 词的任意组合如"宛如巨龙""犹如大海"）。
· 段落句式：对白段一句一段是常态，整章段数不设硬上限（2400字章对白多时段数自然多，与句均字数不矛盾）；防碎不卡段数，只卡两条——「段均句数 ≤1.8」和「连续≥4句一句话独立段」，超过=漫画分镜脚本化→必须合并相邻2-4句同场景/同镜头/同POV的叙述/动作/对白为一段。叙述主力段=40-90字（1-2个逗号长句）；叙述句主力形态=整句20-35字、逗号串1-2个动作单元收一个句号（占叙述句70-80%）；短句（≤15字）只做重拍、连续≤2句；叙述短段（＜40字）占比15-25%（仅约束叙述段，对白段不计）。
· 【铁律A·段落≠碎句（禁令5配套·最高硬约束）】——段落（1–2句成段）只为阅读节奏服务；**段落内的每一句都必须是完整、自然、可读的句子**（语义齐全连贯、停顿合理、主谓宾或逻辑链条齐全）。**绝不为求短而把一句话硬剁成几截读不通的残片**：反例1「他不说话。只是盯着对面那人。」（虽各有句号，但语义是同一动作链的残切=碎句）→合并为「他不说话，只是盯着对面那人。」；反例2「刀光一闪。血溅了半尺。」（同镜头连续动作的残切=碎句）→合并为「刀光一闪，血溅了半尺。」。宁可合并成1段内2句完整短句，也绝不写半句语义残缺的碎片段。
· 【禁令0·最高优先级·AI修正式否定句密集症】——**全章禁绝，出现=直接重写，无例外**。
  · 识别（任一条即命中）：①「不是X是Y/不是X。是Y/不是X准确说是Y/不是A也不是B——是C」否定-肯定交替句 ②嵌套≥2层修正句 ③排比三连/四连「更X，更Y，更Z」/看见…看见…/举起…落下…等动作排比 ④独立句号拆开的"不是X。是Y"句式 ⑤"但/至少不该/其实/准确说/严格来说"类自我修正/抠字眼句。
  · 处理：一律改**直接陈述事实的正常句**；排比三连拆成自然单句推进。⚠️ 之前"不是X准确说是Y=真人写法保留"=错误，彻底推翻，所有形态一律禁绝。
· 章节衔接：人物状态、时间线、事件、地理、人物出场、物品/信息须前后连贯。
· 只输出处理后的正文，不解释改了什么、为什么改，不附加自检清单或协作口吻说明。
· 核心口诀：行动往上浮，动机往下潜；先让读者爽，再让他细思极恐。

1. 冰山理论与结构铁律

冰山理论
· 水上1/8（情节、爽点、打脸、升级、赚钱）必须清晰直白，直接喂给读者，不可藏。
· 水下7/8（动机、伏笔、创伤、执念、世界观深层）必须让读者能脑补，不能写成设定说明书，也不能全沉海底让读者一无所获。

起承转合
· 起：开局三秒内建立人物、危机、欲望或金手指，让读者立刻知道追什么。
· 承：按情绪节拍推进，每个场景制造小爽感或悬念，不空转。
· 转：转折必须来自已埋伏笔或人物深层动机，禁天降巧合。
· 合：结尾留钩子，或完成打脸/升级闭环，同时露出新冰山一角。

黄金三章
· 第一章亮出最刺激冲突和最鲜明金手指，禁大段背景说明和慢热铺垫；深层人性藏到第三章后引爆。

契诃夫之枪
· 特写物品、特殊能力、反复提及的符号，必须在后续情节中回收，禁只埋不收。

2. 人物

· 禁止贴标签（冷酷/温柔/腹黑），必须用可观察的反常行为刻画。
· 水面行为下埋可脑补动机，让反常行为有解释空间。
· 背景只露一角：在反常行为出现的瞬间露出，让读者追更求解。
· 有瑕疵、会纠结、口是心非；禁完美人设，禁OOC；无纯反派，所有行为有合理动机。

3. 情节、伏笔与世界观

· 情节结尾停在动态动作或悬念上，禁抒情总结升华。
· 对话有潜台词，不说大道理；出场人物有名有姓；章节无缝衔接。
· 爽点靠信息差和布局，不开上帝视角；感情线绑定主线。
· 禁顿悟式成长、大段景物抒情、人物语气同质化、行为逻辑割裂。
· 伏笔晒宝：三章内让读者意识到伏笔；水上情节节奏明快；水下伏笔在爽点事件中露出异常细节；三章内兑现，让读者恍然大悟。
· 世界观冰山：开篇不铺三千字设定，只展示当前层级；更高层级从他人惊恐、古籍、禁地传说、大佬失态中露一两个字；让读者自己补全。
""".strip()

WRITING_STYLE_RULES = """
【正文写作阶段专属·行文规范】

【行文速查·正文阶段新增要点（通用铁律/冰山/人物/情节见上方，不复述）】

1. 解释腔
· 禁词：这说明、这意味着、由此可见、换句话说、事实上、显然、本质上、归根结底、从某种意义上说、不得不说、毋庸置疑。
· 处理：删 > 压成一句 > 交还动作/视线/语气 > 落回场景反应（段尾不升华）。

2. 对白
· 禁：角色替作者讲设定/背景；自我剖析完整；冲突时仍客气过顺；书面连接词（因此/然而/与此同时）；**3 句以上对白连续独立段不嵌动作/不夹反应**（像剧本台词）。
· 【对白 6 式·硬执行·二次加固】
  ① 工整问答 → 答非所问/半句话/沉默/打断（**每 3 段 A→B→A→B 工整问答里至少 1 段是打断/沉默/答非所问**）。
  ② 提示语全放句首 → 嵌句中或放句尾+小动作收（**「XX说：对白」句首占比 ≤40%；≥60% 要么嵌对白中，要么放对白后**；**另外硬卡：连续 3 句对白里至少 1 句必须跟动作/反应在同一段，不许每句对白单独成段**）。
  ③ 反派明说威胁 → 歪逻辑翻话 + 盯物 + 提容器不提物品。
  ④ 上位者主动认错 → 甩锅转题 + 旁人圆场。
  ⑤ 问句完整答 → 每 4 段问答至少 1 段半句话/答别的。
  ⑥ 2 人机械轮换 → 每 3 句对白至少 1 句 POV 小动作或小吐槽。
· 【群像对话·角色声纹差硬卡】3 人以上对话，每 2 人的对白必须用：用词（粗口/文绉绉/方言口语）、句长（有的爱说短句有的啰嗦）、语气词差异（有的爱加"啊""呗"有的完全不用）至少 1 个维度区分开；**严禁 3 人以上对白全是"陈述句+句号"，读起来分不清谁是谁**。

3. 工整句式
· 禁：多段等长同构；排比三连过密；段尾总总结；路标词密集（然而/同时/此外/更重要的是/总而言之）。
· 处理：连续三段相似时至少调一段；路标词能删全删；短中段厚段交替；段尾停在细节/动作/余波。

4. 段落与句式·黄金 4 型
· 叙述主力段 40-90 字（1-2 个逗号长句）；叙述句主力形态=整句 20-35 字、逗号串联 1-2 个动作单元收在一个句号里（占叙述句 70-80%）；短句（≤15字）只做重拍（转折/爆发/收尾），连续不超 2 句；叙述短段（＜40字）占比 15-25%；对白段不限（一句一段是常态）；单段 >100 字占比 ≤3%；禁连续 3 句同主谓结构。
· 【段落密度】一段只承载一个动作/一个信息变化；逗号串超 2 个动作单元 = 拆句，连续 3 句 <12 字句号短句 = 漫画分镜碎句，必须合并成逗号长句。
· 【段落合并·硬执行（配禁令5段均句数+铁律A段落≠碎句）】——**合并前置铁律**：合并或拆分时，不得为了凑"一句话一段"或"段数指标"强行把一句话中间切句号（主谓宾没说完、动作链没闭合就句号=残句/碎句）；任何句号切分后的句子本身必须语义完整，不得是同镜头连续动作的半段残片。相邻 3 句满足【同 POV + 同场景 + 同镜头】三同时，**必须至少合并 2 句成一段**；连续 ≥4 句一句话独立段（不管是不是对白）直接合并相邻 2-3 句：
  - 叙述句接叙述句→同段（例："有人推了前面那人一把。他踩进泥水。"→合并成："有人推了前面那人一把，他踩进泥水。"——逗号串 1-2 个动作收一个句号，不再串第 3 个动作）。
  - 动作句接对白句→同段（例："旁人把手里的东西往地上一戳。「你叫啥？」"→合并同段即可）。
  - 对白句接对白句→若说话人不变/或上一句动作是对下一句对白的铺垫→同段（例：「你叫啥？」「某姓沈。」→可以保持两段，但如果是「A打断B，A又说了一句」→必须合并）。
· ① 递进比较链（多角色场景必写 ≥1 条）：X比起Y像Z→比起W差一截→比起Q差远→比起T小巫见大巫；最后 1-4 字动作短句收尾。
· ② 【禁令0 修正式否定句】详见通用核心铁律「禁令0」，所有「不是X是Y/不是X。是Y/不是X准确说是Y/不是A也不是B——是C」「更X更Y更Z」排比三连一律禁绝，改成直接陈述句，无例外。
· ③ 感官细节三叠：温度/气味/触感/声音 选 2-3 叠（不用比喻词，不用"不是X是Y"修正句铺垫）。
· ④ 动作链=小目标链：每 200-300 字一个"小目标→决策→验证"闭环**（任何重复性劳作/推进行为，全部纳入本条）**——**通用公式：小目标=当前阶段定量产出KPI → 决策=优化动作/路线/技巧以达成 → 验证=可量化的进度检查（做了多少/还差多少/工具或状态是否出问题）**；**不写"继续做/还在做/接着做"这种空转流水账，必须每 200-300 字给一个可验证的进度句**。

5. 禁词与口语化
· 必删词（"一股杀气""一抹笑意"除外）：一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为/甚为/极为、缓缓/淡淡/轻轻/微微、毫无疑问/毋庸置疑/不言而喻/显而易见、因此/然而/由此可见/总而言之/综上所述、深吸一口气、眼中闪过一丝、心中暗想、心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、淡漠/漠然、眸子、嘴角微微上扬、如同/宛如/犹如、周身/周遭/气息/威压、那道身影、说话间/话音未落、当即/顿时/瞬时、有意思、深深一眼。
· 推荐口语：合着、整半天、好家伙、说白了、得了吧、拉倒吧、至于么、啥玩意、啥情况、搁这、没跑了、差不离、差不多得了、说实话/说真的、怎么说呢、你别说/还真别说。
· 语气词：啊/嘛/呗/呢/嗷/哇/咧/哒/喽；标点：？？？/！！！/……

6. 开头/结尾/对话驱动
· 开头 5 选 1：时间/动作/对话/状态/事件。禁环境/心理/世界观/评价开场。
· 【结尾钩子·硬执行】
  - 结尾**必须停在动态动作或悬念**。禁总结/评价/升华。
  - ⚠️【真相直白点破禁】结尾涉及真相揭露（人物身份/关键物品归属/因果谜团答案等）时，**不得直接用陈述句点破答案**，必须把"答案句"改写成【动态动作句收尾】——停在人物动作/目光落点/手部动作/物品状态等外部可观察动作上，让读者自己补出结论（细则与更多示例见去AI阶段行文规范速查真相直白点破条）。
  - 结尾钩子 3 选 1：新威胁逼近的迹象、人物反常动作/对白、只露一个关键细节不说透答案。
· 对话三功能：推进剧情 / 塑造性格 / 制造爽感。删掉无影响的对白直接删。

7. 情绪与描写
· 情绪直给：只写外在（表情/动作/语言），不写内心感受；写了动作就不写感受。
· 环境描写 ≤15%；重点刻画动作、微表情、矛盾心理。
· 人味注入：不完美细节（结巴/重复/打断/口误）、思维跳跃、角色互怼/荒诞逻辑。

8. 自检速查（仅列正文阶段独有项，通用铁律项已在上文）
· 【禁令0 必过】整章有没有任何形式的「不是X是Y/不是X。是Y/不是X准确说是Y/不是A也不是B——是C」？有没有排比三连「更X更Y更Z」「看见…看见…看见…」「举起…落下…举起…」？有没有"至少不该""但""其实""准确说"这种自我修正/抠字眼句式？→ **全部砍**，改成直接陈述句。
· 【对白自检 3 条必过】
  - ① 对白6式二次加固硬卡有没有落实？（每3段工整问答至少1段打断/沉默/答非所问；句首提示语≤40%；每4段问答至少1段半句话/答别的；每3句对白至少1句小动作/小吐槽）
  - ② 连续3句对白有没有至少1句**与动作/反应同段**（不许每句对白单独成段像剧本台词）？提示语句首占比 ≤40%？
  - ③ 3 人以上对话有没有角色声纹差（用词/句长/语气词差异，至少每 2 人 1 维区分）？
· 【段落合并必过】相邻 3 句同 POV/同场景/同镜头有没有合并？连续 ≥4 句一句话独立段有没有合并相邻 2-3 句？段均句数是不是 ≤1.8（不卡总段数，对白一句一段是常态）？叙述句七成以上是不是逗号长句（20-35字、串1-2个动作）？连续 3 句 <12 字句号短句有没有合并？短句是不是只在转折/爆发/收尾做重拍（连续≤2句）？有没有违反铁律A——为求短段把一句话硬剁成几截（如"他不说话。只是盯着对面那人。""刀光一闪。血溅了半尺。"这种同镜头动作残切=碎句，必须合并成语义完整的单句/复句，不得留半句语义残缺的碎片段）？
· 【动作闭环必过】任何重复性劳作/推进行为类段落，每 200-300 字有没有「小目标→决策→验证」闭环（没写验证句=空转流水账=不合格）？
· 【结尾钩子必过】真相直白点破有没有改写成动态动作句收尾？结尾停在动态动作或悬念，没有总结/评价/升华？
· 多角色围观场景有没有写 ≥1 条递进比较链？
· 比喻/拟人每千字 ≤3 处？有没有命中 8 大 AI 套话比喻词？
""".strip()

# 向后兼容：旧常量名 NARRATIVE_CRAFT_RULES = 通用核心 + 正文行文规范（避免其他引用处报错）
NARRATIVE_CRAFT_RULES = (GENERAL_CORE_RULES + "\n\n" + WRITING_STYLE_RULES).strip()


# ============================================================================
# 【阶段隔离·System Prompt 构建函数】按阶段注入：构思（master包）/ 正文（style包）/
# 去AI审稿（review包 + 独立去AI手册），通用核心三阶段共用
# ============================================================================

def build_conception_rules(skill_pack_ids=None, mode='agent', extra_master_note: str = '', book=None) -> str:
    """构思阶段专属规则：屏蔽文风/去AI/一致性，只保留通用核心+构思格式+master技能包。
    - book 可选：传入时自动从 book.master_skill_ids 取已持久化ID，与请求 skill_pack_ids 取并集。"""
    parts = [GENERAL_CORE_RULES, CONCEPTION_EXTRA_RULES]
    master_note = ''
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        book_ids = _resolve_skill_ids_by_category(book, 'master') if book else []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_ids)))  # 有序并集
        master_note = _get_skill_prompts_by_category(merged_ids, 'master', mode=mode) or ''
    except Exception:
        master_note = ''
    if extra_master_note:
        master_note = (master_note + '\n\n' + extra_master_note).strip()
    if master_note:
        parts.append("【构思类·技能包专属方法论】\n" + master_note)
    return "\n\n".join(parts).strip()


def build_writing_rules(book=None, skill_pack_ids=None, mode='agent',
                        extra_style_pack: str = '', extra_style_note: str = '') -> str:
    """正文阶段专属规则：通用核心 + 行文规范 + 文风类(style)技能包。
    - book 必传：用于取已持久化文风包，并按 genre_target 匹配题材。
    - 文风技能包【只注入一次】：merged_ids = 请求传参 ids ∪ book.style_skill_ids（有序并集去重），
      不再重复走 _get_enabled_style_pack 那条路径，避免同一包出现 2 遍。"""
    parts = [GENERAL_CORE_RULES, WRITING_STYLE_RULES]
    style_note = ''
    book_genre = getattr(book, 'genre', None) if book is not None else None
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        # 合并 ids：请求 skill_pack_ids ∪ book.style_skill_ids（持久化的），有序并集去重
        if book is not None:
            book_style_ids = _resolve_skill_ids_by_category(book, 'style')
        else:
            book_style_ids = []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_style_ids)))
        style_note = _get_skill_prompts_by_category(merged_ids, 'style', mode=mode, book_genre=book_genre) or ''
    except Exception:
        style_note = ''
    if extra_style_pack or extra_style_note:
        extra = '\n\n'.join(s for s in [extra_style_pack, extra_style_note] if s).strip()
        style_note = (style_note + ('\n\n' + extra if style_note else extra)).strip()
    if style_note:
        parts.append("【文风类·技能包专属规则】\n" + style_note)
    return "\n\n".join(parts).strip()


def build_review_rules(skill_pack_ids=None, mode='agent',
                       prompt_keys_filter=None, extra_review_note: str = '', book=None) -> str:
    """去AI/审稿阶段专属规则：通用核心 + 独立去AI手册 + 审查类(review)技能包。
    ⚠️ 去AI阶段【不再注入 WRITING_STYLE_RULES】：
       DEAI_ONLY_RULES 内部已经完整覆盖解释腔/对白/工整句式/禁词表/执行流程等行文规范，
       再注入 WRITING_STYLE_RULES 会让同内容出现 2 遍（用户感知"啰里啰嗦重复好几遍"）。"""
    parts = [GENERAL_CORE_RULES, DEAI_ONLY_RULES]
    review_note = ''
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        book_ids = _resolve_skill_ids_by_category(book, 'review') if book else []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_ids)))
        if prompt_keys_filter:
            review_note = _get_skill_prompts_by_category(
                merged_ids, 'review', prompt_keys_filter, mode=mode
            ) or ''
        else:
            review_note = _get_skill_prompts_by_category(
                merged_ids, 'review', mode=mode
            ) or ''
    except Exception:
        review_note = ''
    if extra_review_note:
        review_note = (review_note + '\n\n' + extra_review_note).strip()
    if review_note:
        parts.append("【审查类·技能包专属规则】\n" + review_note)
    return "\n\n".join(parts).strip()


# ============================================================================
# 用户说话意图识别 → 自动同步核心创作参数（卷数/每卷章数/题材/风格）到 Book + BookBible
# 解决：用户在智驾里说“改成25卷”时，不能只当一句对话，要真正落地到 DB，
#       否则后续 prompt 中的【核心创作参数铁律】读的还是旧值，等于用户白说。
# ============================================================================

# 卷数：正则命中即提取数字（允许：总卷数/全书/一共/计划/改成/设为/按/做 等词 + N + 卷）
# 例："改成25卷"、"全书按30卷来写"、"总卷数15卷"、"一共8卷"、"做60卷"、"写10卷"、"搞18卷"、"按20卷规划"
_RE_TV = re.compile(
    r'(?:总卷数|全书|全本|整本书|一共|总共|合计|总计|计划|准备|打算|想|要|需要|改成|改为|设置为|设为|调整为|调成|调为|按|做成|写成|做|写|搞|设计成|规划成|规划|控制在|就|那就|那就按|就按|至少|最多|左右|大概|约|差不多)'
    r'\s*(\d{1,4})\s*卷',
)
# 反向宽松：数字+卷 在句中且含"卷"的意图词（兜底）；配合负向词表避免误判叙事
_RE_TV_LOOSE = re.compile(r'(?:^|[,，。；！？\s])(\d{1,4})\s*卷', re.IGNORECASE)

# 每卷章数：例 "每卷60章"、"改成每卷 80 章"、"每卷按40章规划"
_RE_CPV = re.compile(
    r'(?:每卷|一卷|单卷|一册)\s*(?:改成|改为|设置为|设为|调整为|按|做成|写成|控制在|计划|一共|大约|约)?\s*(\d{1,4})\s*章'
)
_RE_CPV_LOOSE = re.compile(r'每卷.*?(\d{1,4})\s*章', re.IGNORECASE)

# 防止被"第12卷"、"卷三"、"10卷公交"这种非总卷数/章数意图的纯叙事描述命中：负向关键词
_NEG_TV_TOKENS = re.compile(r'(第\s*\d+\s*卷|卷[一二三四五六七八九十百千零\d]+|回|话|公交|公卷|问卷|试卷|答卷|卷宗|卷(起|发|入|尺|子|心菜|心菜|曲|烟|叶|铺盖|包|云|))', re.IGNORECASE)


def _auto_sync_params_from_user_message(book, bb, message: str):
    """从用户最新一条聊天消息中识别“卷数/章数调整”意图，真正同步到 DB。

    Returns:
      list[str]：本次实际同步成功的 human-readable 说明（供前端 SSE meta 回显）。
                 空列表表示没有识别到需要同步的参数。
    """
    import app as app_module
    from app import db, _sync_book_meta_to_bible  # 复用现有同步机制，保证口径一致
    if not book or not message:
        return []
    msg = (message or '').strip()
    if not msg:
        return []
    synced_notes = []

    # -------- 1. 总卷数：提取并落 Book.total_volumes --------
    def extract_tv(text):
        m = _RE_TV.search(text)
        if m:
            return int(m.group(1))
        # 负向过滤：含“第N卷/卷X”字样时不再走宽松匹配，避免"我现在在写第25卷"被误判为改总卷数
        if _NEG_TV_TOKENS.search(text):
            return None
        m2 = _RE_TV_LOOSE.search(text)
        if m2:
            return int(m2.group(1))
        return None

    tv_new = extract_tv(msg)
    if tv_new is not None and 1 <= tv_new <= 2000:  # 合理性区间，防止"1卷"这种错别字
        try:
            cur_tv = int(getattr(book, 'total_volumes', 0) or 0)
        except Exception:
            cur_tv = 0
        if cur_tv != tv_new:
            # 先写 Book（权威口径）
            book.total_volumes = tv_new
            # 再调用同步机制把 Book → BookBible（内部已处理 Case A/B，不会把用户 Bible 手工修改覆盖）
            if bb is None:
                from app import BookBible as _BB
                bb = _BB.query.filter_by(book_id=book.id).first()
                if bb is None:
                    bb = _BB(book_id=book.id)
                    db.session.add(bb)
            _sync_book_meta_to_bible(book, bb, commit=False)
            db.session.commit()
            synced_notes.append(f'【已同步】检测到你要求“{tv_new}卷”，已自动将作品总卷数从 {cur_tv or "未设定"} 更新为 {tv_new} 卷（后续五幕总纲/分卷规划/正文写作都会严格按此卷数执行）')

    # -------- 2. 每卷章数：提取并落 Book.chapters_per_volume（若有该字段） --------
    def extract_cpv(text):
        m = _RE_CPV.search(text)
        if m:
            return int(m.group(1))
        m2 = _RE_CPV_LOOSE.search(text)
        if m2:
            return int(m2.group(1))
        return None
    cpv_new = extract_cpv(msg)
    if cpv_new is not None and 5 <= cpv_new <= 500:
        # chapters_per_volume 字段在不同版本项目里可能叫 chapters_per_volume / chapters_every_volume / 不存在
        field_candidates = ('chapters_per_volume', 'chapters_every_volume', 'chapters_per_book')
        cur_cpv = 0
        for f in field_candidates:
            if hasattr(book, f):
                try:
                    cur_cpv = int(getattr(book, f, 0) or 0)
                except Exception:
                    cur_cpv = 0
                break
        if cur_cpv != cpv_new:
            for f in field_candidates:
                if hasattr(book, f):
                    setattr(book, f, cpv_new)
                    break
            if bb is None:
                from app import BookBible as _BB
                bb = _BB.query.filter_by(book_id=book.id).first()
                if bb is None:
                    bb = _BB(book_id=book.id)
                    db.session.add(bb)
            _sync_book_meta_to_bible(book, bb, commit=False)
            db.session.commit()
            synced_notes.append(f'【已同步】检测到你要求“每卷 {cpv_new} 章”，已自动将每卷章数从 {cur_cpv or "默认"} 更新为 {cpv_new} 章（后续总章数上限会按 总卷数 × {cpv_new} 章计算）')

    return synced_notes


# ============================================================================
# Action Card 协议
# ============================================================================

# 卡片类型 → 目标维度字段 + 落地方式（append 覆盖/追加, character 走独立表, chapter 走章节表）
CARD_REGISTRY = {
    'SAVE_WORLDSETTING': {'field': 'worldbuilding', 'mode': 'append', 'label': '世界观'},
    'SAVE_CHARACTER':    {'field': 'character_profiles', 'mode': 'character', 'label': '人物'},
    'SAVE_FORESHADOW':   {'field': 'foreshadowing', 'mode': 'append', 'label': '伏笔'},
    'SAVE_OUTLINE_NODE': {'field': 'plot_design', 'mode': 'append', 'label': '大纲'},
    'SAVE_PLOT':         {'field': 'timeline', 'mode': 'timeline', 'label': '剧情线'},
    'SAVE_LOCATION':     {'field': 'locations', 'mode': 'append', 'label': '地点'},
    'SAVE_RULE':         {'field': 'key_rules', 'mode': 'append', 'label': '核心规则'},
    'APPLY_STYLE':       {'field': 'style_guide', 'mode': 'append', 'label': '文风'},
    'SAVE_CONCEPT':      {'field': 'concept', 'mode': 'append', 'label': '核心构思'},
    'SAVE_CHAPTER':      {'field': 'chapter', 'mode': 'chapter', 'label': '章节正文'},
}

# 安全提取卷号：接受 dict 或 str，返回 int 或 0
def _extract_volume_index_safe(vol):
    """从卷字典或字符串中提取卷号。dict 时取 volume/volume_id 字段。"""
    if isinstance(vol, dict):
        for k in ('volume_index', 'volume_id'):
            v = vol.get(k)
            if v is not None:
                try:
                    return int(v)
                except (ValueError, TypeError):
                    pass
        # 从 volume 名提取
        try:
            from app import _extract_volume_index
            return _extract_volume_index(vol.get('volume', '') or vol.get('volume_title', '') or '')
        except Exception:
            return 0
    if isinstance(vol, str):
        try:
            from app import _extract_volume_index
            return _extract_volume_index(vol)
        except Exception:
            return 0
    return 0

# LLM 输出的卡片标记格式：
#   [[CARD:SAVE_CHARACTER|标题|内容]]
#   [[CARD:SAVE_FORESHADOW|标题|内容]]
# 支持内容中含 | 时用最后一个 | 分隔（标题不含 |）
CARD_RE = re.compile(r'\[\[CARD:([A-Z_]+)\|([^\|]*)\|([\s\S]*?)\]\]')


def parse_cards(text: str) -> list[dict]:
    """从 LLM 文本中解析出所有 Action Card。"""
    cards = []
    for m in CARD_RE.finditer(text):
        ctype, title, content = m.group(1), m.group(2).strip(), m.group(3).strip()
        if ctype in CARD_REGISTRY:
            cards.append({
                'id': str(uuid.uuid4())[:8],
                'type': ctype,
                'title': title or CARD_REGISTRY[ctype]['label'],
                'content': content,
                'target': CARD_REGISTRY[ctype]['label'],
            })
    return cards


def strip_cards(text: str) -> str:
    """从文本中移除卡片标记，返回纯聊天文本。"""
    return CARD_RE.sub('', text).strip()


# ============================================================================
# 维度感知 system_prompt 构建
# ============================================================================

def _smart_truncate(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ''
    cut = text[:limit]
    last_break = max(cut.rfind('\n\n'), cut.rfind('。'), cut.rfind('\n'))
    return (cut[:last_break] if last_break > limit // 2 else cut) + '\n…（已截断）'


def _build_toc_block(book_id, max_items: int = 200) -> str:
    """构建章节目录（按卷分组）块，供智驾聊天 system prompt 注入。
    让 AI 知道完整目录，用户提"第N章/某卷"时可精准定位，不再索要资料。
    输出：
      第1卷《XXX》
        第1章 XXX（2350字）
        ...
    """
    try:
        from app import Chapter, parse_chapter_number
        from sqlalchemy import or_
        rows = Chapter.query.filter_by(book_id=book_id).all()
        if not rows:
            return ''
        # 按 order_index 排序（卷+章统一 order）
        rows_sorted = sorted(rows, key=lambda c: (c.parent_id or '', c.order_index or 0))
        vol_map = {v.id: v for v in rows_sorted if v.is_volume}
        # 分组：卷 -> 该卷章节
        from collections import OrderedDict
        groups: 'OrderedDict[str, list]' = OrderedDict()
        orphans = []
        for c in rows_sorted:
            if c.is_volume:
                groups.setdefault(c.id, [])
                continue
            pid = c.parent_id
            if pid and pid in vol_map:
                groups.setdefault(pid, []).append(c)
            else:
                orphans.append(c)
        lines = []
        total = 0
        for vid, chs in groups.items():
            vol = vol_map.get(vid)
            if vol:
                lines.append(f'第{vol.order_index or 1}卷《{vol.title or "未命名卷"}》')
            for ch in chs:
                if total >= max_items:
                    break
                wc = ch.word_count or 0
                lines.append(f'  {ch.title or ""}（{wc}字）')
                total += 1
            if total >= max_items:
                break
        if orphans and total < max_items:
            if groups:
                lines.append('【未分卷章节】')
            for ch in orphans[:max_items - total]:
                wc = ch.word_count or 0
                lines.append(f'  {ch.title or ""}（{wc}字）')
        if not lines:
            return ''
        return '\n'.join(lines)
    except Exception:
        return ''


def _core_params_iron_block(bb, book):
    """构建“核心创作参数·铁律·不可违反”块（所有创作链路统一注入）。

    比 _build_core_params_block 更强：
    - 标为“铁律·不可违反”放在 system prompt 最上方，避免被下文淹没
    - 追加“越界拦截警示”：大纲/剧情/卷规划必须严格等于总卷数；正文章号不得超过总章数上限
    - 所有 8 条创作链路都要调用（chat_smart / smart_general / smart_generate /
      smart_dimension_edit / smart_batch / smart_deai / chat_smart_action / _action_chapter）

    返回空串表示获取参数失败（静默降级，不阻断主流程）。
    """
    try:
        from app import _get_total_volumes, _get_genre_label, _get_novel_styles_text, _get_chapters_per_volume
        tv = _get_total_volumes(bb, book)
        genre_label = _get_genre_label(book, bb)
        styles_text = _get_novel_styles_text(bb, book)
        cpv = _get_chapters_per_volume(bb, book)
        max_chapters = (tv or 0) * cpv  # 总章数上限 = 总卷数 × 每卷章数（tv 未设置时为 0，下文铁律跳过）
        bt = getattr(book, 'book_type', 'novel') or 'novel'
        parts = ['【核心创作参数·铁律·不可违反】']
        if tv and tv >= 1:
            parts.append(f'1. 总卷数：{tv} 卷（全书所有分卷/五幕总纲/剧情大纲严格按此卷数规划，不得多不得少）')
        else:
            # 用户仍未显式设置总卷数 → 不给任何默认暗示（十/五/十二都不出现），
            # 等用户真正设定后，后续调用会按上面那条真正绑定。
            parts.append('1. 总卷数：由作者定义（创作时请按作者已经给定的分卷规模规划；若作者尚未指定分卷，请先把分卷规模显式写在方案里给作者确认）')
        parts.append(f'2. 题材：{genre_label}（人物设定、世界观、剧情走向、爽点类型须契合该题材的读者期待）')
        if bt == 'novel':
            if tv and tv >= 1:
                parts.append(f'3. 每卷章数：约 {cpv} 章/卷（全书总章数上限约 {max_chapters} 章，总字数约 {tv*12} 万字）')
            else:
                parts.append(f'3. 每卷章数：约 {cpv} 章/卷（总章数上限按作者后续指定的总卷数 × {cpv} 章来计算；在作者未指定前，请先把分卷规模明确写在方案里）')
        else:
            if tv and tv >= 1:
                parts.append(f'3. 短篇结构：{tv} 个单元/幕')
            else:
                parts.append('3. 短篇结构：单元/幕数由作者定义，先在方案里明确给出再推进。')
        if styles_text:
            parts.append(f'4. 风格流派：{styles_text}（人物塑造、节奏、爽点设计、叙事手法须契合所选流派，这是硬约束）')
        # 越界拦截：只有 tv 明确设置后才给具体 N 卷/N 章的硬上限，避免把 0/默认 当成越界依据
        if tv and tv >= 1:
            parts.append('')
            parts.append('【越界拦截警示·生成前自检】')
            parts.append(f'1. 生成五幕总纲/分卷大纲/剧情线时，卷数必须严格等于 {tv} 卷，多一卷或少一卷都不合格，必须重写。')
            parts.append(f'2. 生成正文章节时，章节号上限为第 {max_chapters} 章（= {tv} 卷 × {cpv} 章/卷），禁止产出超过此上限的章节号。')
            parts.append('3. 讨论规划或生成卡片前先对照上述铁律，若你的方案会突破卷数/章数上限，请立刻自我修正到范围内再输出。')
        else:
            parts.append('')
            parts.append('【分卷规则·用户定义优先】')
            parts.append('1. 在作者未显式给出总卷数前，禁止在方案/卡片/正文里擅自默认"十卷/五卷/八卷/十二卷/十余卷/5-8卷"等固定数字，必须先把"全书建议按 N 卷规划"写清楚让作者确认，或直接沿用作者方案里的数字。')
            parts.append('2. 生成正文章节时，章号连续、不重复、不跳号即可；等作者指定总卷数后再按上限收紧。')
        return '\n'.join(parts)
    except Exception:
        return ''


def build_chat_system_prompt(book, bb, recent_chapters: list = None, next_chapter_num: int = None, toc_block: str = None) -> str:
    """构建维度感知的聊天 system_prompt。

    注入当前书的全部 bible 维度 + 章节目录 + Action Card 使用说明 + 创作进度。
    维度内容完整注入，不截断（避免信息缺失导致错乱）。
    recent_chapters: 最近章节列表（dict: title/word_count/order_index），由 chat_smart 注入
    next_chapter_num: 下一章应使用的章节号（与写作/修改/去AI统一口径）
    toc_block: 按卷分组的章节目录（可选，_build_toc_block 生成）
    """
    parts = [
        '你是一位资深网文创作副驾，正在和作者协作创作一部小说。你的职责：',
        '1. 像同行一样讨论创作问题（人物、剧情、世界观、文风）',
        '2. 当讨论中形成明确结论时，主动产出“落地卡片”让作者一键采纳',
        '3. 感知创作进度，主动引导下一步该做什么',
        '',
        f'【当前作品】《{book.title or "未命名"}》',
    ]

    # =====================================================================
    # 【核心创作参数·铁律·不可违反】（用户创建小说时的总卷数/题材/风格，注入到最上方，避免被下文淹没）
    # =====================================================================
    core_iron = _core_params_iron_block(bb, book)
    if core_iron:
        parts.append('\n' + core_iron)

    # =====================================================================
    # 【三阶段通用铁律】（智驾聊天=跨阶段讨论场景：只注入 GENERAL_CORE_RULES 通用铁律，
    # 不注入 WRITING_STYLE_RULES 正文行文规范，也不注入 DEAI_ONLY_RULES 去AI手册
    # ——正文写作/去AI有独立接口精准注入专属规则，这里不一股脑全加载。）
    # =====================================================================
    parts.append('\n' + GENERAL_CORE_RULES)

    # 注入最近章节（让 AI 知道作者正在写哪一章，便于讨论"接下来怎么写"）
    if recent_chapters:
        parts.append('\n【最近章节】')
        for ch in recent_chapters[-5:]:
            title = ch.get('title') or f'第{ch.get("order_index", "?")}章'
            wc = ch.get('word_count', 0)
            parts.append(f'- {title}（{wc}字）')
        parts.append('作者可能在写最新章节的后续，讨论时可结合上文衔接。')

    # 注入下一章应使用的章节号（与写作/修改/去AI统一口径，避免产出重复章号的卡片）
    if next_chapter_num is not None:
        parts.append(
            f'\n【章节号铁律】当前正文章节维度下最新章节号已到第{next_chapter_num - 1}章。'
            f'产出 SAVE_CHAPTER 卡片时，新章节标题必须用“第{next_chapter_num}章”开头'
            f'（如：第{next_chapter_num}章 章节名），不得重复使用已有的章节号。'
            f'修改已有章节时，保持原章节号不变。'
        )

    # 注入 bible 维度（完整注入，不截断，避免信息缺失导致错乱）
    if bb:
        dims = [
            ('核心构思', 'concept'),
            ('世界观', 'worldbuilding'),
            ('核心规则', 'key_rules'),
            ('人物档案', 'character_profiles'),
            ('大纲', 'plot_design'),
            ('剧情时间线', 'timeline'),
            ('伏笔', 'foreshadowing'),
            ('地点', 'locations'),
            ('文风指南', 'style_guide'),
        ]
        filled = []
        empty = []
        for label, field in dims:
            val = (getattr(bb, field, '') or '').strip()
            if val:
                # 人物维度：JSON 数组转自然语言，避免 AI 模仿 JSON 格式
                if field == 'character_profiles' and val.startswith('['):
                    val = _character_profiles_to_text(val)
                parts.append(f'\n【已设定·{label}】\n{val}')
                filled.append(label)
            else:
                empty.append(label)

        if not filled:
            parts.append('\n【创作状态】这是一本新书，所有维度都还空白，需要从头讨论设定。')
        else:
            parts.append(f'\n【创作进度】已完成维度：{"、".join(filled)}')
            if empty:
                parts.append(f'待补充维度：{"、".join(empty)}（可引导作者讨论这些）')

        # 防遗忘检查报告回注：让智驾聊天也能感知已诊断出的一致性违规/待回收伏笔/叙事债务
        try:
            from app import _collect_anti_forget_alerts
            _af = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
            if _af:
                parts.append('\n【防遗忘检查诊断】（最近检查发现的问题，讨论与产出卡片时必须主动规避，不可重犯）')
                parts.append(_af)
        except Exception:
            pass
    else:
        parts.append('\n【创作状态】这是一本新书，还没有任何设定，需要从头讨论。')

    # 章节目录（按卷分组）：让AI根据章号/标题精准定位，不再向用户索要资料
    if toc_block:
        parts.append('\n【章节目录】（用户提"第N章""某卷""某章标题"时，你必须基于此定位章节，直接输出修改方案/续写，绝不要让用户"把资料发给你"）')
        parts.append(toc_block)

    # 维度入口：让AI知道改某维度内容直接用对应卡片/或基于已有内容输出
    parts.append("""
【维度与章节定位铁律·永远遵守】
1. 绝对禁止回复"请把大纲/设定/人物/章节资料发给我""你需要先提供XXX资料"这类话。
   上面【已设定·XXX】和【章节目录】中已有完整数据；若某个维度确实为空，直接说"这个维度还是空白，咱们从零开始…"，然后给出方案建议即可。
2. 用户提到以下关键词 → 直接基于对应的【已设定·XXX】内容进行讨论/修改：
   - "构思/故事核/核心冲突" → 核心构思
   - "世界观/世界设定/地理" → 世界观
   - "设定/规则/体系/能力/修炼" → 核心规则
   - "人物/角色/主角/配角/XXX（人名）" → 人物档案
   - "大纲/剧情线/主线/支线/五幕" → 大纲
   - "时间线/时间/年代" → 剧情时间线
   - "伏笔/铺垫/伏笔回收" → 伏笔
   - "地点/场景/XXX（地名）" → 地点
   - "文风/叙事风格" → 文风指南
3. 用户提到"第N章/某章标题/某卷"：
   - 若要求"修改/调整/润色/改改"：基于该章位置上下文讨论具体改动建议，产出 SAVE_CHAPTER 卡片或详细修改方案；
   - 若要求"接着写/续写"：严格按【章节号铁律】写新章节；
   - 不要让用户再把正文发给你（系统后续会自动把该章原文注入上下文）。
4. 当确实缺少具体细节（如改第5章但要改某句特定措辞），只问具体要改什么，不要笼统要资料。
""".strip())

    # Action Card 使用说明
    parts.append(_CARD_INSTRUCTIONS)

    # 平台级纯文字排版铁律（禁止 * 和 #）
    parts.append(PLAIN_TEXT_LAYOUT_RULES)

    # 用户采纳的"系统学习与优化建议"补丁 → 作为铁律段追加到 system prompt 末尾，
    # 保证智驾对话、后续维度生成都能按用户定制规则约束自身输出
    if bb:
        try:
            from meta_optimizer import build_active_patch_text
            _pp = build_active_patch_text(bb)
            if _pp:
                parts.append('\n' + _pp)
        except Exception:
            pass

    return '\n'.join(parts)


_CARD_INSTRUCTIONS = """
【落地卡片使用规则】
当讨论中形成明确、可落地的结论时，在回复末尾产出落地卡片，格式严格如下（可同时多张）：
[[CARD:卡片类型|标题|具体内容]]

支持的卡片类型：
- SAVE_WORLDSETTING  世界观设定（如：灵石体系、修炼境界）
- SAVE_CHARACTER     人物档案（格式：姓名|身份|性格|背景，用换行分隔字段）
- SAVE_FORESHADOW    伏笔（如：主角功法被夺的真相）
- SAVE_OUTLINE_NODE  大纲节点（如：第一幕·陷害）
- SAVE_PLOT          剧情线（如：主角流落凡界后的成长路线）
- SAVE_LOCATION      地点（如：天云宗、万妖谷）
- SAVE_RULE          核心规则/能力体系（如：修为突破需渡劫）
- APPLY_STYLE        文风指南（如：冷硬派叙事，短句为主）
- SAVE_CONCEPT       核心构思（一句话故事核）
- SAVE_CHAPTER       章节正文（标题=章节名，内容=完整章节正文，可直接作为章节保存）

注意：
- 卡片内容必须具体、可直接写入设定库或作为正文保存，不要写"建议讨论XXX"这种空话
- SAVE_CHAPTER 仅在用户明确要求"写一章""接着写正文"时产出，内容必须是完整的章节正文，且严格遵循【字数绝对铁律】：2400字±100（即 2300-2500 字区间，字数口径为中文字符+中文标点，不含标题）。低于 2300 字须扩写场景细节补足；超过 2500 字须精简删减。这是不可违反的硬约束。
- 不要每条回复都产卡片，只在确实有可落地结论时才产
- 先用对话讨论，达成共识后再产卡片
""".strip()


# ============================================================================
# 创作进度地图
# ============================================================================

def build_progress_map(bb) -> dict:
    """分析各维度完成度，给出下一步建议。"""
    dims = [
        ('concept', '核心构思', '一句话讲清故事核？先聊主角是谁、要什么、最大的阻碍是什么'),
        ('worldbuilding', '世界观', '故事发生在什么世界？有什么独特的规则或设定？'),
        ('key_rules', '核心规则', '能力体系/修炼体系/科技树是怎样的？有什么硬规则？'),
        ('character_profiles', '人物', '主角和核心配角定了吗？他们的动机和性格？'),
        ('plot_design', '大纲', '故事的主线走向？三幕式或起承转合？'),
        ('timeline', '剧情时间线', '关键剧情节点的时间顺序？'),
        ('foreshadowing', '伏笔', '埋了哪些长线伏笔？'),
        ('style_guide', '文风', '想要什么叙事风格？冷硬/细腻/幽默？'),
    ]
    result = []
    filled_count = 0
    for field, label, hint in dims:
        val = (getattr(bb, field, '') or '').strip() if bb else ''
        # 简易完成度：按字符量分级
        if not val:
            status, pct = 'empty', 0
        elif len(val) < 100:
            status, pct = 'sketch', 30
        elif len(val) < 500:
            status, pct = 'partial', 60
        else:
            status, pct = 'solid', 100
            filled_count += 1
        result.append({'field': field, 'label': label, 'status': status, 'pct': pct, 'hint': hint})

    total = len(dims)
    overall = round(filled_count / total * 100)

    # 下一步建议：优先指向第一个非 solid 的核心维度
    next_step = None
    priority_order = ['concept', 'character_profiles', 'worldbuilding', 'key_rules', 'plot_design', 'timeline', 'foreshadowing', 'style_guide']
    for f in priority_order:
        item = next((x for x in result if x['field'] == f), None)
        if item and item['status'] != 'solid':
            next_step = {'field': f, 'label': item['label'], 'hint': item['hint']}
            break

    return {
        'dims': result,
        'overall': overall,
        'filled': filled_count,
        'total': total,
        'next_step': next_step,
    }


# ============================================================================
# 上下文滑窗管理
# ============================================================================

def build_context_messages(system_prompt: str, history: list[dict], user_msg: str) -> list[dict]:
    """组装发给 LLM 的完整 messages：system + 滑窗历史 + 当前用户消息。"""
    # 截断每条历史消息
    trimmed = []
    for m in history:
        role = m.get('role', 'user')
        content = (m.get('content') or '')[:MAX_MSG_CHARS]
        if content:
            trimmed.append({'role': role, 'content': content})

    # 滑窗：保留最近 MAX_HISTORY_ROUNDS 轮（每轮 user+assistant = 2 条）
    max_msgs = MAX_HISTORY_ROUNDS * 2
    if len(trimmed) > max_msgs:
        trimmed = trimmed[-max_msgs:]

    return [{'role': 'system', 'content': system_prompt}] + trimmed + [{'role': 'user', 'content': user_msg}]


# ============================================================================
# 章节号统一口径：写作 / 修改 / 去AI 三者共用
# ============================================================================

def _get_latest_chapter_info(book_id):
    """获取最新章节的统一口径信息（写作/修改/去AI共用）。

    返回: { latest_num, next_num, latest_chapter }
      - latest_num: 最新章节号（优先 parse_chapter_number(title)，回退 order_index+1）
      - next_num:   下一章应使用的章节号 = latest_num + 1（无章节时为 1）
      - latest_chapter: 最新章节对象（按章节号/ order_index 排序的最后一章）

    解决问题：order_index 与标题章节号不一致时，三者用不同口径导致
    "已有第1章，写作又生成第1章"。
    """
    from app import Chapter, parse_chapter_number
    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    if not chs:
        return {'latest_num': 0, 'next_num': 1, 'latest_chapter': None}
    # 优先按标题章节号排序；无章节号者回退 order_index
    def sort_key(c):
        n = parse_chapter_number(c.title or '')
        return (0, n) if n is not None else (1, c.order_index)
    chs_sorted = sorted(chs, key=sort_key)
    latest = chs_sorted[-1]
    latest_num = parse_chapter_number(latest.title or '')
    if latest_num is None:
        latest_num = latest.order_index + 1
    return {'latest_num': latest_num, 'next_num': latest_num + 1, 'latest_chapter': latest}


# ============================================================================
# 章节标题剥离 + 字数统计统一口径
# 解决：AI 输出"标题+空行+正文"整体入 card.content → 正文混标题、字数口径不一致。
# 统一：content 只存纯正文；字数用 _count_cn_chars（去空白含标点）
# ============================================================================

def _strip_chapter_title(content, fallback_title=''):
    """从 AI 输出中剥离章节标题行，返回 (title, body)。

    AI 被要求输出格式：第一行标题，第二行空行，第三行起纯正文。
    本函数防御性处理：
      - 首行像章节标题（以"第N章/Chapter N"开头 + 短行≤30 + 非句末标点）
        且第二行为空行（匹配 AI 被要求的"标题+空行+正文"格式）→ 剥离标题及后续空行
      - 自动去除标题行首的 markdown # 标记（如 "# 第四章 左臂开狱" → "第四章 左臂开狱"）
      - 否则视为纯正文，title 回退到 fallback_title，body 为原文

    用于：正文写作/润色/去AI 产出 SAVE_CHAPTER 卡片前剥离标题，保证 card.content 为纯正文。
    "第二行必须为空行"的约束可避免误剥叙事句（如正文首行"第三章的秘密终于揭晓"）。
    """
    if not content:
        return fallback_title, ''
    text = content.strip()
    lines = text.split('\n')
    first_raw = lines[0].strip() if lines else ''
    # 去除行首 markdown # 标记（#、##、###...）
    first = re.sub(r'^#+\s*', '', first_raw).strip()
    from app import parse_chapter_number
    # 章节标题判定：以"第N章/Chapter N"开头 + 短行(≤30) + 非句末标点
    starts_with_chapter = bool(
        re.match(r'^第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*[章节回卷部篇话集幕折更段讲课夜日年季场]', first)
        or re.match(r'^(?:chapter|ch|episode|ep)\.?\s*\d+', first, re.IGNORECASE)
    )
    is_title_line = (
        starts_with_chapter
        and bool(parse_chapter_number(first))
        and len(first) <= 30
        and not first.endswith(('。', '！', '？', '；', '.', '!', '?', '"', '"', "'", "'", '…'))
    )
    # 必须满足"标题+空行+正文"格式：第二行（lines[1]）为空行，才剥离
    has_blank_after = len(lines) >= 2 and not lines[1].strip()
    if is_title_line and has_blank_after:
        title = first or fallback_title
        # 跳过标题后的所有空行
        body_start = 1
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
        body = '\n'.join(lines[body_start:]).strip()
        return title, body
    # 首行不像标题或不满足格式：原文整体作为正文，标题用兜底值
    return fallback_title, text


# ============================================================================
# 路由
# ============================================================================

@chat_collab_bp.route('/api/ai/chat/smart', methods=['POST'])
def chat_smart():
    """维度感知流式聊天。

    请求体：
      { book_id, session_id?, message, scope? }
    返回 SSE 流：
      data: {type:"delta", content:"..."}   文本片段
      data: {type:"card", card:{...}}       Action Card
      data: {type:"done"}                   结束
    """
    from app import db, AISession, Book, BookBible, AIConfig, Chapter
    from llm_gateway import LLMGateway, get_llm_config
    data = request.json or {}
    book_id = data.get('book_id')
    session_id = data.get('session_id')
    message = (data.get('message') or '').strip()
    scope = data.get('scope', 'general')

    if not book_id or not message:
        return jsonify({'error': '缺少 book_id 或 message'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ===== 【卷数/章数意图·落地前置】先于 LLM 调用执行 =====
    # 用户在智驾里说"改成25卷/每卷60章"时，必须真正写入 DB，
    # 否则后续 build_chat_system_prompt → _core_params_iron_block 读到的还是旧值，用户等于白说。
    params_sync_notes = _auto_sync_params_from_user_message(book, bb, message)
    # 同步后重新取一次 bb（可能刚刚新增了一条，避免后续 None 判空出问题）
    if bb is None:
        bb = BookBible.query.filter_by(book_id=book_id).first()

    # P4：加载最近 5 章标题（让 AI 懂作者正在写哪一章）+ 统一口径下一章号
    recent_chapters = []
    next_chapter_num = None
    try:
        # 统一口径：从章节表提取最新章节号（与写作/修改/去AI共用）
        ch_info = _get_latest_chapter_info(book_id)
        next_chapter_num = ch_info['next_num']
        # 最近 5 章：按章节号排序（与统一口径一致）
        from app import parse_chapter_number
        recent = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        def _recent_key(c):
            n = parse_chapter_number(c.title or '')
            return (0, n) if n is not None else (1, c.order_index)
        recent = sorted(recent, key=_recent_key)[-5:]
        recent_chapters = [{'title': c.title, 'word_count': c.word_count or 0,
                            'order_index': c.order_index} for c in recent]
    except Exception:
        pass

    # 获取或创建会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）
    session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=message[:30])
    session_id = session.id

    # 构建 system_prompt + 上下文（注入章节目录）
    toc_block = _build_toc_block(book_id)
    system_prompt = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)

    # 自动上下文注入：根据用户输入识别提及的章节/维度，将原文/维度内容作为"引用前言"前置到用户消息中
    # 同时生成命中信息，在 SSE 首个 meta 事件中回传给前端做"已定位"提示
    auto_ctx_block, auto_ctx_info = _build_auto_context_block(message, book_id, bb)
    enriched_user_message = message
    if auto_ctx_block:
        enriched_user_message = (
            '（以下为系统根据作者输入自动从当前书库载入的引用资料，用于辅助回答；作者原话为最后的"【作者原话】"段。\n'
            '回答时直接基于这些资料讨论/修改，严禁再让作者"把资料发给我"；若引用中的某维度为空，直接说明为空并给出建议。）\n\n'
            f'{auto_ctx_block}\n\n'
            '——————————————————\n'
            '【作者原话】\n'
            f'{message}'
        )

    history = load_session_messages(session)
    messages = build_context_messages(system_prompt, history, enriched_user_message)

    # 获取 LLM 配置 + gateway
    cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    import app as app_module
    base_url, api_key, model = get_llm_config(app_module)
    gw = LLMGateway(base_url, api_key, model)

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        full_text = []
        try:
            # SSE 首帧 ①：核心创作参数同步结果（若用户这条消息触发了卷数/章数调整，先告诉前端已落地）
            if params_sync_notes:
                yield f'data: {json.dumps({"type": "meta", "kind": "params_sync", "info": {"notes": params_sync_notes}}, ensure_ascii=False)}\n\n'
            # SSE 首帧 ②：返回命中的章节/维度（用于前端回显"已定位并注入…"提示）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield f'data: {json.dumps({"type": "meta", "kind": "auto_context", "info": auto_ctx_info}, ensure_ascii=False)}\n\n'

            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=4096):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT  # 裸注释心跳帧：前端自动忽略，不进正文
                    continue
                full_text.append(chunk)
                yield f'data: {json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False)}\n\n'

            # 解析卡片
            complete = ''.join(full_text)
            cards = parse_cards(complete)
            # 统一纯文本清理（卡片内容、卡片标题、回复正文）
            for c in cards:
                c['content'] = _clean_text_to_plain(c.get('content', ''))
                if c.get('title'):
                    c['title'] = _clean_text_to_plain(c['title'])
            for card in cards:
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            # 持久化对话（剥离卡片标记后存历史，cards 单独存以便历史会话恢复）
            clean_text = _clean_text_to_plain(strip_cards(complete))
            # 卡片持久化时标记为 pending，前端历史会话加载后可继续采纳
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending'} for c in cards]
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text,
                            'cards': persisted_cards})
            _safe_save_session_messages(session, history)

            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _persist_card_status(session_id, card_id, new_status, new_content=None):
    """更新会话 messages_json 中指定卡片的 status（采纳/编辑/忽略后持久化）。

    用于解决：重新打开聊天界面时卡片又恢复为 pending 的问题。
    """
    if not session_id or not card_id:
        return
    try:
        from app import db, AISession
        session = AISession.query.get(session_id)
        if not session:
            return
        msgs = load_session_messages(session)
        changed = False
        for m in msgs:
            if m.get('role') != 'assistant' or not m.get('cards'):
                continue
            for c in m['cards']:
                if c.get('id') == card_id:
                    c['status'] = new_status
                    if new_content is not None:
                        c['content'] = new_content
                    changed = True
        if changed:
            _safe_save_session_messages(session, msgs)
    except Exception:
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass


@chat_collab_bp.route('/api/ai/chat/smart/apply-card', methods=['POST'])
def apply_card():
    """采纳 Action Card，落地到对应维度。

    SAVE_CHAPTER 模式：落地到 Chapter 表。
      - 自动识别章节号（parse_chapter_number）
      - 同章节号（或同标题）的章节存在 → 覆盖内容，不再追加
      - 不存在 → 新建章节
      - 落地后调用 resort_chapters_by_title(rebin_volumes=True)
        按章节号自动排序，按 50 章/卷自动新建/归入卷
    其他维度：
      - status='edited'（编辑后落地）→ 覆盖该维度原内容
      - status='adopted' 或默认（直接采纳）→ 追加到该维度原内容后
    落地成功后会持久化卡片 status（adopted/edited），避免重开聊天又提示采纳。
    """
    from app import db, BookBible, Character, Chapter, parse_chapter_number, resort_chapters_by_title, AISession
    data = request.json or {}
    book_id = data.get('book_id')
    card = data.get('card', {})
    ctype = card.get('type', '')
    content = (card.get('content') or '').strip()
    title = card.get('title', '')
    card_id = card.get('id', '')
    session_id = data.get('session_id')
    # 编辑后落地用覆盖模式，直接采纳用追加模式
    is_edit_overwrite = (card.get('status') == 'edited')
    # 落地后要回写的卡片状态（采纳=adopted，编辑后落地=edited）
    new_card_status = 'edited' if is_edit_overwrite else 'adopted'

    if ctype not in CARD_REGISTRY or not content:
        return jsonify({'error': '无效的卡片或内容为空'}), 400

    spec = CARD_REGISTRY[ctype]

    # 平台级后处理：统一清理 Markdown 符号 * 和 #，保证落地内容为好看的纯文字排版
    # timeline 模式跳过清理（content 是 JSON 数组，清理会破坏 JSON 结构）
    if spec.get('mode') != 'timeline':
        content = _clean_text_to_plain(content)
    title = _clean_text_to_plain(title) if title else title

    result_extra = {}

    # 章节正文卡：落地到 Chapter 表（覆盖同章节号/同标题，自动分卷排序）
    if spec['mode'] == 'chapter':
        # ====== 落地二次校验：章号越界坚决不入库（最后一道门） ======
        from app import Book, _get_total_volumes, _get_chapters_per_volume
        _book = Book.query.get(book_id)
        _bb = BookBible.query.filter_by(book_id=book_id).first()
        _tv = _get_total_volumes(_bb, _book)
        _cpv = _get_chapters_per_volume(_bb, _book)
        _max_chapters = _tv * _cpv
        # 防御性剥离标题行：保证 chapter.content 为纯正文
        # 兜底场景：chat_smart 产出的 SAVE_CHAPTER 卡片或历史会话恢复的卡片可能仍含标题
        stripped_title, body_content = _strip_chapter_title(content, fallback_title=title)
        # 若剥离出更具体的标题（含章节名），优先用剥离结果
        if stripped_title and stripped_title != title:
            title = stripped_title
        # 字数统计与章节保存 API（app.py count_words）口径一致，避免落地后字数跳变
        from app import count_words
        wc = count_words(body_content)
        ch_num = parse_chapter_number(title)
        # 最后一道门：章号解析成功且越界 → 拒绝落地
        if ch_num is not None and ch_num > _max_chapters:
            return jsonify({'error': (f'【落地拦截·总章数越界】全书设定总卷数 {_tv} 卷 × 每卷 {_cpv} 章 = 总章数上限 {_max_chapters} 章，'
                                    f'“{title or f"第{ch_num}章"}”(第{ch_num}章) 已超出上限，未保存。若需要继续，请先到作品基本信息中调大总卷数。')}), 400
        existing_ch = None
        # 优先按章节号匹配（覆盖同章节号的章节）
        if ch_num is not None:
            candidates = Chapter.query.filter_by(
                book_id=book_id, is_volume=False
            ).all()
            for c in candidates:
                if parse_chapter_number(c.title or '') == ch_num:
                    existing_ch = c
                    break
        # 兜底：按完全相同标题匹配（覆盖同章节名的章节）
        if not existing_ch and title:
            existing_ch = Chapter.query.filter_by(
                book_id=book_id, is_volume=False, title=title
            ).first()

        if existing_ch:
            # 覆盖模式：同章节号/同标题存在，更新内容、标题、字数
            existing_ch.title = title or existing_ch.title
            existing_ch.content = body_content
            existing_ch.word_count = wc
            existing_ch.updated_at = datetime.now(timezone.utc)
            ch = existing_ch
            action = 'updated'
        else:
            # 新增模式：不存在同章节号/同标题，追加新章节
            max_idx = db.session.query(db.func.max(Chapter.order_index)) \
                .filter_by(book_id=book_id, is_volume=False).scalar() or 0
            ch = Chapter(
                book_id=book_id,
                title=title or f'第{max_idx + 1}章',
                content=body_content,
                order_index=max_idx + 1,
                word_count=wc,
                status='draft',
                is_volume=False,
                parent_id='',
            )
            db.session.add(ch)
            action = 'created'
        db.session.commit()

        # 自动按章节号排序 + 按 50 章/卷重新归入卷（新建卷及章节归属）
        try:
            resort_chapters_by_title(book_id, rebin_volumes=True)
            db.session.commit()
        except Exception:
            db.session.rollback()

        # M1a: 章节入库后自动抽取事件 → EventLog，并索引本章埋/收伏笔
        # P1-1 升级：关键章（卷首/高潮/卷末）自动启用 LLM 抽取；普通章走正则；并返回关键章信息给前端
        try:
            from event_log_manager import append_chapter_events_auto
            from llm_gateway import LLMGateway, get_llm_config as _get_cfg
            known_actors = [c.name for c in Character.query.filter_by(book_id=book_id).all() if c.name]
            known_locations = []
            try:
                locs = json.loads(_bb.locations or '[]') if _bb else []
                if isinstance(locs, list):
                    known_locations = [str(x) for x in locs if x]
            except Exception:
                pass
            total_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).count()
            # 取 LLM 配置（若用户未配置则 fallback 正则，不抛错）
            _gw = None
            _base = _api = _model = ''
            try:
                _base, _api, _model = _get_cfg()
                if _base and _api and _model:
                    _gw = LLMGateway(_base, _api, _model)
            except Exception:
                _base = _api = _model = ''
            ev_result = append_chapter_events_auto(
                _bb, ch, body_content,
                known_actors=known_actors,
                known_locations=known_locations,
                total_chapters=total_chapters,
                gw=_gw, base_url=_base, api_key=_api, model=_model,
            )
            result_extra['event_log'] = {
                'added': ev_result.get('events_added', 0),
                'ids': (ev_result.get('event_ids') or [])[:5],
                'use_llm': ev_result.get('use_llm_actual', False),
                'key_chapter': ev_result.get('key_chapter'),
            }
            # 索引本章埋/收伏笔（从 DAG 反查）
            if _bb and _bb.foreshadowing_graph:
                try:
                    from foreshadowing_manager import ForeshadowingGraph
                    graph = ForeshadowingGraph.from_dict(json.loads(_bb.foreshadowing_graph))
                    hooks = graph.get_nodes_for_chapter(ch.order_index)
                    ch.hooks_set_json = json.dumps({
                        'setup': [n.id for n in hooks.get('setup', [])],
                        'payoff': [n.id for n in hooks.get('payoff', [])],
                    }, ensure_ascii=False)
                except Exception:
                    pass
            db.session.commit()
        except Exception:
            db.session.rollback()

        result_extra = {
            'action': action,
            'chapter_id': ch.id,
            'chapter_title': ch.title,
            'word_count': wc,
            'order_index': ch.order_index,
        }
        # 持久化卡片状态（避免重开聊天又提示采纳/保存为新章节）
        _persist_card_status(session_id, card_id, new_card_status, body_content)
        # bible 可能不存在，但 progress 仍要返回
        bb = BookBible.query.filter_by(book_id=book_id).first()
        return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                        'progress': build_progress_map(bb),
                        **result_extra})

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    try:
        if spec['mode'] == 'character':
            # 人物卡：解析"姓名|身份|性格|动机|背景|关系|能力|物品"或按行/【字段】解析
            # 前端人物及关系维度期望 character_profiles 为 JSON 数组，每元素含：
            # name/role/identity/personality/motivation/background/relationships/abilities/items
            # 一次生成多个人物时，content 含多个人物块（空行分隔），全部解析后追加
            char_list = _parse_character_card_multi(title, content)

            # 编辑后落地：覆盖模式——清空原人物列表后写入新人物
            # 直接采纳：追加模式——保留原人物后追加新人物
            if is_edit_overwrite:
                # 覆盖：删除原 Character 表记录 + 重置 character_profiles
                try:
                    Character.query.filter_by(book_id=book_id).delete()
                except Exception:
                    pass
                existing_list = []
            else:
                # 追加：保留原数据
                existing_list = []
                try:
                    parsed = json.loads(bb.character_profiles or '[]')
                    if isinstance(parsed, list):
                        existing_list = parsed
                except Exception:
                    existing_list = []

            for char_data in char_list:
                db.session.add(Character(
                    book_id=book_id,
                    name=char_data.get('name') or '未命名',
                    role=char_data.get('role') or ('protagonist' if '主角' in (title or '') or '主角' in content else 'supporting'),
                    description=char_data.get('identity') or '',
                    personality=char_data.get('personality') or '',
                    background=char_data.get('background') or '',
                ))
                existing_list.append(char_data)
            bb.character_profiles = json.dumps(existing_list, ensure_ascii=False)
        elif spec['mode'] == 'timeline':
            # 剧情线卡：content 是 JSON 数组（按卷），需按 volume_index upsert 到已有 timeline
            # 修复 main_events/summary 丢失：合并时保留旧 nodes，缺字段时用 summary 回填 main_plot，
            # 确保 summary/main_events/nodes 三层结构在 DB 里完整保留，不被旧 UI 判定为"只采纳了概要"。
            raw = content.strip()
            # 剥离可能的 markdown 代码块包裹
            fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
            if fence:
                raw = fence.group(1).strip()
            try:
                new_vols = json.loads(raw)
                if not isinstance(new_vols, list):
                    new_vols = [new_vols] if isinstance(new_vols, dict) else []
            except (json.JSONDecodeError, ValueError, TypeError):
                # JSON 解析失败：退回纯文本模式存储
                new_vols = []

            if new_vols:
                # 解析已有 timeline
                existing_vols = []
                try:
                    parsed_tl = json.loads(bb.timeline or '[]')
                    if isinstance(parsed_tl, list):
                        existing_vols = parsed_tl
                except (json.JSONDecodeError, ValueError, TypeError):
                    existing_vols = []

                def _merge_volume(old_v: dict, new_v: dict) -> dict:
                    """同卷合并：新结构覆盖大部分字段，但保留旧卷已有的 nodes（新卷自带非空 nodes 除外）。
                       并保证向后兼容：summary → main_plot，end_hook → ending_hook，核心字段不空。"""
                    if not isinstance(new_v, dict):
                        return new_v
                    merged = dict(new_v)
                    # 保留旧 nodes（节点设计结果不被上层重新生成 timeline 覆盖丢失）
                    if isinstance(old_v, dict):
                        new_has_nodes = isinstance(merged.get('nodes'), list) and len(merged['nodes']) > 0
                        old_has_nodes = isinstance(old_v.get('nodes'), list) and len(old_v['nodes']) > 0
                        if old_has_nodes and not new_has_nodes:
                            merged['nodes'] = old_v['nodes']
                    # 向后兼容：summary → main_plot（旧代码/旧 UI 只认 main_plot）
                    if (not merged.get('main_plot') or not str(merged['main_plot']).strip()) and merged.get('summary'):
                        merged['main_plot'] = str(merged['summary'])
                    # 核心冲突/结尾钩子兜底：用 summary/end_hook 填，避免空
                    if (not merged.get('core_conflict') or not str(merged['core_conflict']).strip()) and isinstance(old_v, dict):
                        merged['core_conflict'] = old_v.get('core_conflict') or merged.get('core_conflict') or ''
                    if not merged.get('ending_hook'):
                        merged['ending_hook'] = merged.get('end_hook') or merged.get('ending') or (isinstance(old_v, dict) and old_v.get('ending_hook')) or ''
                    # main_events 缺字段兜底：保证 main_events 是数组，元素至少含 index/title
                    me = merged.get('main_events')
                    if not isinstance(me, list) and isinstance(old_v, dict) and isinstance(old_v.get('main_events'), list):
                        merged['main_events'] = old_v['main_events']
                    elif isinstance(me, list):
                        cleaned = []
                        for idx, ev in enumerate(me):
                            if not isinstance(ev, dict):
                                continue
                            ev.setdefault('index', idx + 1)
                            ev.setdefault('title', f'事件{idx+1}')
                            ev.setdefault('summary', ev.get('summary') or ev.get('event') or ev.get('events') or '')
                            ev.setdefault('bury', '')
                            ev.setdefault('payoff', '')
                            cleaned.append(ev)
                        merged['main_events'] = cleaned
                    # nodes 兜底：确保至少空数组
                    if not isinstance(merged.get('nodes'), list):
                        merged['nodes'] = (isinstance(old_v, dict) and isinstance(old_v.get('nodes'), list) and old_v['nodes']) or []
                    return merged

                # 按 volume_index upsert（覆盖同卷时走 _merge_volume 保字段）
                for nv in new_vols:
                    if not isinstance(nv, dict):
                        continue
                    nv_idx = nv.get('volume_index') or _extract_volume_index_safe(nv)
                    matched = False
                    for i, ev in enumerate(existing_vols):
                        if not isinstance(ev, dict):
                            continue
                        ev_idx = ev.get('volume_index') or _extract_volume_index_safe(ev)
                        if str(ev_idx) == str(nv_idx):
                            existing_vols[i] = _merge_volume(ev, nv)
                            matched = True
                            break
                    if not matched:
                        existing_vols.append(_merge_volume({}, nv))

                # 按 volume_index 排序
                existing_vols.sort(key=lambda v: (
                    v.get('volume_index') or _extract_volume_index_safe(v) or 0
                    if isinstance(v, dict) else 0
                ))
                bb.timeline = json.dumps(existing_vols, ensure_ascii=False)
            else:
                # JSON 解析失败，退回纯文本存储
                entry = f'【{title}】\n{content}' if title else content
                if is_edit_overwrite:
                    bb.timeline = entry
                else:
                    existing_tl = (bb.timeline or '').strip()
                    bb.timeline = f'{existing_tl}\n\n{entry}'.strip() if existing_tl else entry
        else:
            field = spec['field']
            existing = (getattr(bb, field, '') or '').strip()
            entry = f'【{title}】\n{content}' if title else content
            if is_edit_overwrite:
                # 编辑后落地：覆盖原内容（用户已编辑，以新内容为准）
                setattr(bb, field, entry)
            else:
                # 直接采纳：追加到原内容后
                setattr(bb, field, f'{existing}\n\n{entry}'.strip() if existing else entry)

            # M1a: 伏笔维度落地后自动解析为 DAG（结构化状态追踪）
            if field == 'foreshadowing':
                try:
                    from foreshadowing_manager import parse_text_to_dag
                    final_text = getattr(bb, field, '') or ''
                    graph = parse_text_to_dag(final_text)
                    if graph.nodes:
                        bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
                except Exception:
                    pass

        db.session.commit()

        # ====== 落地卡片保存到 Bible 后，立即同步实体注册表 ======
        # 用户反馈：实体注册表经常"识别不到智驾刚采纳落地/各维度已写入"的实体，
        # 原因是之前要等用户手动打开实体 Tab 调 list_entities 或触发 planner sync。
        # 现在每张落地卡片保存后就抽一次，实体注册表会立刻更新：
        #  含人物卡(character_profiles JSON) / 剧情 timeline / 世界观 / 势力 / 功法 / 地点……
        try:
            from app import Chapter
            from entity_registry import extract_and_save_registry
            recent_chs = (
                Chapter.query.filter_by(book_id=book_id, is_volume=False)
                .order_by(Chapter.order_index.desc())
                .limit(10)
                .all()
            ) or []
            extract_and_save_registry(bb, chapters_query=recent_chs)
            db.session.commit()
        except Exception:
            db.session.rollback()

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'落地失败：{str(e)}'}), 500

    # 持久化卡片状态（避免重开聊天又提示采纳）
    _persist_card_status(session_id, card_id, new_card_status, content)
    return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                    'progress': build_progress_map(bb),
                    **result_extra})


def _parse_character_card_multi(title, content):
    """解析可能含多个人物的内容，返回人物字典列表。

    拆分策略：
      1) | 分隔的纯文本 → 单个人物
      2) 按"姓名：xxx"行作为每个人物的起始边界拆块（最常见格式）
      3) 每个块再走 _parse_character_card 解析
      4) 无"姓名："引导时，整段作为单个人物
    """
    text = (content or '').strip()
    if not text:
        return []

    # 策略1：| 分隔的单行纯文本（无换行）→ 单个人物
    if '|' in text and '\n' not in text:
        return [_parse_character_card(title, text)]

    # 策略2：按"姓名："行拆块。匹配"姓名：xxx"作为新人物块的起始。
    lines = [l.rstrip() for l in text.split('\n')]
    blocks = []
    cur_block = []
    name_re = re.compile(r'^(?:【|\[)?(姓名|名字|名称)(?:】|\])?[:：]\s*\S')
    for line in lines:
        if name_re.match(line.strip()) and cur_block:
            blocks.append('\n'.join(cur_block).strip())
            cur_block = []
        cur_block.append(line)
    if cur_block:
        tail = '\n'.join(cur_block).strip()
        if tail:
            blocks.append(tail)

    # 若只拆出1块（或0块），回退为整段单人物
    if len(blocks) <= 1:
        return [_parse_character_card(title, text)]

    # 每块解析为人物字典
    result = []
    for i, blk in enumerate(blocks):
        # 第一块继承卡片标题；后续块用块内"姓名："作标题（在 _parse_character_card 内会取到）
        blk_title = title if i == 0 else ''
        parsed = _parse_character_card(blk_title, blk)
        # 兜底：若解析后姓名为空或"未命名"，取块首行
        if not parsed.get('name') or parsed['name'] == '未命名':
            first_line = blk.split('\n', 1)[0].strip()
            m = re.match(r'^(?:【|\[)?(姓名|名字|名称)(?:】|\])?[:：]\s*(.+)$', first_line)
            if m:
                parsed['name'] = m.group(2).strip()
        if parsed.get('name') and parsed['name'] != '未命名':
            result.append(parsed)
    return result if result else [_parse_character_card(title, text)]


def _parse_character_card(title, content):
    """解析人物卡片内容为结构化字段（与前端 CharacterData 对齐）。
    支持格式：
      1) 姓名|身份|性格|动机|背景|关系|能力|物品  （| 分隔）
      2) 姓名：xxx\\n身份：xxx\\n...  （字段名引导）
      3) 【姓名】xxx\\n【身份】xxx\\n...
      4) 纯文本：首行/标题为姓名，其余为性格
    """
    fields = ['name', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items']
    # 字段关键词映射（支持中文标签）
    key_map = {
        'name': ['姓名', '名字', '名称'],
        'role': ['角色', '定位'],
        'identity': ['身份', '职业'],
        'personality': ['性格', '个性'],
        'motivation': ['动机', '目的'],
        'background': ['背景', '来历'],
        'relationships': ['关系', '人际关系'],
        'abilities': ['能力', '技能', '金手指'],
        'items': ['物品', '装备'],
    }
    result = {f: '' for f in fields}
    result['name'] = title or '未命名'
    result['role'] = ''

    text = content.strip()
    # 策略1：| 分隔
    if '|' in text and '\n' not in text:
        parts = [p.strip() for p in text.split('|') if p.strip()]
        for i, f in enumerate(fields):
            if i < len(parts):
                result[f] = parts[i]
        if result['name'] and title:
            result['name'] = title
        return result

    # 策略2/3：按行解析，匹配字段关键词
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    matched_any = False
    for line in lines:
        m = re.match(r'^(?:【|\[)?(姓名|名字|名称|角色|定位|身份|职业|性格|个性|动机|目的|背景|来历|关系|人际关系|能力|技能|金手指|物品|装备)(?:】|\])?[:：]\s*(.+)$', line)
        if m:
            matched_any = True
            label = m.group(1)
            value = m.group(2).strip()
            for f, keys in key_map.items():
                if label in keys:
                    result[f] = value
                    break
    if matched_any:
        if title and not result['name']:
            result['name'] = title
        elif not result['name'] or result['name'] == '未命名':
            result['name'] = title or lines[0][:20]
        return result

    # 策略4：纯文本兜底
    result['name'] = title or (lines[0][:20] if lines else '未命名')
    result['personality'] = text
    if '主角' in (title or '') or '主角' in text:
        result['role'] = '主角'
    return result


@chat_collab_bp.route('/api/ai/chat/smart/update-card-status', methods=['POST'])
def update_card_status():
    """更新卡片状态（用于忽略等不落地的操作持久化）。

    body: { session_id, card_id, status: 'ignored'|'adopted'|'edited' }
    返回: { ok: true }
    """
    data = request.json or {}
    session_id = data.get('session_id')
    card_id = data.get('card_id')
    new_status = data.get('status', 'ignored')
    if not session_id or not card_id:
        return jsonify({'error': '缺少 session_id 或 card_id'}), 400
    if new_status not in ('ignored', 'adopted', 'edited'):
        return jsonify({'error': '无效的 status'}), 400
    _persist_card_status(session_id, card_id, new_status)
    return jsonify({'ok': True})


@chat_collab_bp.route('/api/books/<book_id>/ai/progress', methods=['GET'])
def get_progress(book_id):
    """创作进度地图。"""
    from app import BookBible
    bb = BookBible.query.filter_by(book_id=book_id).first()
    return jsonify(build_progress_map(bb))


@chat_collab_bp.route('/api/books/<book_id>/ai/sessions', methods=['GET'])
def list_sessions(book_id):
    """列出该书所有聊天会话。"""
    from app import AISession
    sessions = AISession.query.filter_by(book_id=book_id).order_by(AISession.updated_at.desc()).all()
    return jsonify({'sessions': [
        {'id': s.id, 'scope': s.scope, 'title': s.title,
         'updated_at': s.updated_at.isoformat() if s.updated_at else None,
         'message_count': len(json.loads(s.messages_json or '[]'))}
        for s in sessions
    ]})


@chat_collab_bp.route('/api/ai/sessions/<session_id>/messages', methods=['GET'])
def get_session_messages(session_id):
    """获取单个聊天会话的全部消息（用于历史会话切换时加载聊天记录）。

    返回：{ id, title, scope, messages: [...] }
    messages 元素结构：{ role, content, cards? }
    """
    from app import AISession
    session = AISession.query.get(session_id)
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    msgs = load_session_messages(session)
    return jsonify({
        'id': session.id,
        'title': session.title,
        'scope': session.scope,
        'messages': msgs,
        'updated_at': session.updated_at.isoformat() if session.updated_at else None,
    })


# ============================================================================
# 方案A：副驾做指挥官，总创作/章节创作降级为被调度的能力
# 统一动作调度接口：前端点快捷按钮 → 后端代理调用现有能力 → 统一转成副驾卡片协议
# ============================================================================
# 支持的动作：master_create 批量生成设定（每维度产一张卡）/ continue 续写本章 /
#             polish 润色本章（均调 ai-continue/stream，正文产 SAVE_CHAPTER 卡）
# SSE 副驾协议：delta 流式正文 / card 落地卡片 / done 结束 / error 错误
# ============================================================================

# 动作 → 默认维度（master_create 用）
_ACTION_DIMENSIONS = {
    'master_create': ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design'],
}

# 维度字段 → 卡片类型（master_create 产出时映射）
_DIM_TO_CARD = {
    'concept': 'SAVE_CONCEPT',
    'key_rules': 'SAVE_RULE',
    'worldbuilding': 'SAVE_WORLDSETTING',
    'character_profiles': 'SAVE_CHARACTER',
    'plot_design': 'SAVE_OUTLINE_NODE',
    'timeline': 'SAVE_PLOT',
    'locations': 'SAVE_LOCATION',
    'style_guide': 'APPLY_STYLE',
}


@chat_collab_bp.route('/api/ai/chat/smart/action', methods=['POST'])
def chat_smart_action():
    """统一动作调度：副驾快捷按钮入口。

    body: { book_id, action, session_id?, instruction?, target_chapter_num?, prev_chapter_content?, skill_pack_ids? }
    返回 SSE，统一副驾卡片协议。
    """
    from app import (db, AISession, Book, BookBible, AIConfig, Chapter)
    from llm_gateway import LLMGateway, get_llm_config
    data = request.json or {}
    book_id = data.get('book_id')
    action = data.get('action')
    session_id = data.get('session_id')
    instruction = (data.get('instruction') or '').strip()
    target_chapter_num = data.get('target_chapter_num')
    prev_chapter_content = data.get('prev_chapter_content')
    skill_pack_ids = data.get('skill_pack_ids') or []

    if not book_id or action not in ('master_create', 'continue', 'polish'):
        return jsonify({'error': '参数无效，action 必须为 master_create/continue/polish'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404

    # 复用或创建会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）
    title_map = {'master_create': '批量生成设定', 'continue': '续写本章', 'polish': '润色本章'}
    session = _get_or_create_session_for_book(session_id, book_id, scope='general',
                                              title=title_map.get(action, 'AI动作'))
    session_id = session.id

    # ===== 【卷数/章数意图·落地前置】副驾快捷按钮的 instruction 也可能含卷数/章数要求 =====
    bb = BookBible.query.filter_by(book_id=book_id).first()
    params_sync_notes_action = _auto_sync_params_from_user_message(book, bb, instruction or '')
    if bb is None:
        bb = BookBible.query.filter_by(book_id=book_id).first()

    # 取激活配置
    try:
        base_url, api_key, model = get_llm_config()
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    def sse(payload: dict) -> str:
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        try:
            # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧 + start受理帧，占住连接防超时 ===
            yield ': ping-heartbeat-keepalive\n\n'
            yield sse({'type': 'meta', 'kind': 'start', 'info': {'action': action, 'session_id': session_id}})
            # 副驾首帧：参数同步说明（若有）
            if params_sync_notes_action:
                yield sse({'type': 'meta', 'kind': 'params_sync', 'info': {'notes': params_sync_notes_action}})
            if action == 'master_create':
                yield from _action_master_create(book, session, instruction, gw, sse)
            elif action == 'continue':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='continue',
                                           base_url=base_url, api_key=api_key, model=model,
                                           skill_pack_ids=skill_pack_ids)
            elif action == 'polish':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='polish',
                                           base_url=base_url, api_key=api_key, model=model,
                                           skill_pack_ids=skill_pack_ids)
        except Exception as e:
            try:
                yield sse({'type': 'error', 'error': f'{type(e).__name__}: {e}'})
            except Exception:
                # yield 本身失败说明连接已彻底断开，静默即可
                pass
        finally:
            try:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            except Exception:
                # finally 里的 commit/rollback 再失败也不能裸抛，否则 SSE 帧格式畸形
                pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _action_master_create(book, session, instruction, gw, sse):
    """批量生成设定：逐维度调用 LLM，每维度产出一张落地卡片。"""
    from app import BookBible
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()
    dims = _ACTION_DIMENSIONS['master_create']

    # 构建各维度上下文（已生成维度作为下游上下文）
    generated = {}
    emitted_cards = []  # 收集已发出的卡片对象（id 与前端一致，用于持久化）
    for dim in dims:
        label = _DIM_LABELS.get(dim, dim)
        card_type = _DIM_TO_CARD.get(dim, 'SAVE_CONCEPT')
        yield sse({'type': 'delta', 'content': f'\n\n正在生成【{label}】…\n\n'})

        # 维度 prompt
        existing = ''
        if bb:
            existing = (getattr(bb, dim, '') or '').strip()
        ctx_parts = []
        for k, v in generated.items():
            # 人物维度转自然语言
            if k == 'character_profiles' and v.startswith('['):
                v = _character_profiles_to_text(v)
            ctx_parts.append(f'【{_DIM_LABELS.get(k, k)}】\n{v}')
        ctx_block = '\n\n'.join(ctx_parts) if ctx_parts else '（暂无）'

        # 注入核心创作参数铁律（批量设定也要遵守总卷数/题材/风格）
        core_iron = _core_params_iron_block(bb, book)

        # =====【P0 修复·统一短 prompt 也加细项约束·禁止两句话就结束】=====
        # 各维度的分节清单/字数下限/细项强制说明，master_create 与 smart_generate 共享口径
        concept_more = (
            '\n\n【构思维度铁律·禁止两句话】必须输出 10 节：'
            '①一句话故事核Logline ②主题曲线(起点→反诘→抉择→终局) ③核心冲突三角(主角×对手×世界规则) '
            '④目标分层(短/中/长/终极+失败代价) ⑤核心爽感机制(3-5种主爽点+触发→爆发→余波+卷1/3/5/终局排布) '
            '⑥金手指/外挂(类型+核心能力+分级+硬约束代价+贴合执念+终极风险) '
            '⑦主角魅力公式(记忆符号+三重反差+具体创伤+核心执念) ⑧对手/反派魅力(前/中/终局三级反派的合理诉求/创伤/赢面/软肋/镜像点) '
            '⑨世界观3-5个独特卖点钩子 ⑩全书情感底色+读者定位+文风力向。'
            '总字数不少于 1200 字，每节必须写具体可落地内容，禁止"待设定/后续再定"。'
        ) if dim == 'concept' else ''
        key_rules_more = (
            '\n\n【设定维度铁律·禁止只写境界表】必须输出 11 节：'
            '①力量总体系(2主1辅+克制) ②等级阶梯表(命名+战力差+突破门槛+社会地位+寿元+战力天花板) '
            '③至少2主1偏的提升路径差异 ④功法/技能树(5类分级+代表性技能+配搭+获取方式) '
            '⑤资源与货币体系(通用货币+等价物+至少10项物品价格表+资源产地+修炼成本贫富差) '
            '⑥装备/法宝/载具(分级+获取+认主+损耗+3-5件代表作) '
            '⑦至少2-3种副职业(炼丹/炼器/阵法/符箓/御兽/编程/制造)+代表物品配方价格 '
            '⑧硬约束+反噬代价(越级/禁术/心魔/境界掉落+金手指冷却/资源消耗) '
            '⑨种族/职业/阵营总表+历史矛盾+跨种族禁忌 ⑩至少8条世界硬规则禁忌(执法者+灰色地带) '
            '⑪文明水平总览(科技树/修炼文明层级)。'
            '总字数不少于 1500 字，必须有具体数字和例子。'
        ) if dim == 'key_rules' else ''
        worldbuilding_more = (
            '\n\n【世界观铁律·禁止只写四大域】必须输出 15 节（最少 2000 字）：'
            '①世界总览(宇宙/位面/主舞台轮廓) ②创世元史三段(神话版/正史版/隐藏版) + 至少3纪元大事 '
            '③至少6大地理分块(含开局/中期/禁地/天堑4类区域) ④气候天象体系(周期+对修炼/战争/经济影响+节日仪式) '
            '⑤至少8大主要势力(名/领袖/地盘/战力/经济/理念/剧情位) ⑥完整阶级金字塔(顶/中/底/禁忌群体+流动通道) '
            '⑦政治律法(国体/法律/执法/审判/灰色地带) ⑧经济贸易(产业/商路/3个交易都市/黑市/货币权) '
            '⑨至少5个智慧种族(外貌/寿命/栖息/优势/短板/文化/关系/混血地位) '
            '⑩至少2正统+1邪教宗教信仰(神系/教会结构/信仰力量/教权皇权关系/邪教土壤) '
            '⑪语言文字度量衡历法(通用语/古语/识字率/度量/节日) ⑫风俗礼仪服饰饮食建筑 '
            '⑬军事体系(组织/兵种/阵法/战争规模/战后恢复) ⑭交通通讯(跑图速度成本+传信速度保密) '
            '⑮至少5个世界未解之谜/禁忌之地/上古遗留(每谜都要写与主线关联)。'
        ) if dim == 'worldbuilding' else ''
        character_more_master = (
            '\n\n【人物维度铁律·禁止只给姓名+一句话身份】至少写出 主角 + 1女主/重要女配 + 2核心配角 + 1前期反派 + 1中期反派：'
            '每个角色按以下 15 项写满：'
            '1)姓名(含字号/外号/别名) 2)性别/年龄(开篇时) 3)外貌特征(含专属记忆符号：疤痕/旧物/佩饰/小动作/口头禅) '
            '4)身份地位(出身/职业/阶层/职称) 5)核心性格三原色(主色/辅色/应激色)+优点/缺点/道德底线 '
            '6)核心价值观+行为准则 7)人生三目标(短/中/长) 8)深层动机+执念来源 '
            '9)具体核心创伤(哪年哪日谁做了什么/失去了什么/留下什么身体或心理印记) 10)恐惧与软肋(最怕什么/被捏住什么就失控) '
            '11)能力体系(主职能力+辅助能力+金手指权限等级/掌握度/战力量化) '
            '12)擅长战斗/不擅长战斗的情况、习惯武器/法器、战斗风格 '
            '13)人物背景故事(家庭/成长经历/教育经历/重要事件/形成原因300字以上) '
            '14)关键关系网(家人/师友/爱人/仇敌/上司/下属 至少8人，每人关系类型+态度+羁绊来源+利益交集) '
            '15)角色弧线：开篇状态 → 转折事件 → 中期转变 → 终局归宿/结局。'
            '每个角色至少 300 字，合计不少于 1800 字；纯中文按字段分行输出，禁止 JSON 符号。'
        ) if dim == 'character_profiles' else ''
        plot_design_more = (
            '\n\n【大纲维度铁律·禁止只写几句话五幕】必须写满：'
            '五幕(立身/立足/立势/立威/立命)对应到连续卷号 + 每卷6项指标 '
            '(①本卷爽点4小1大 ②人物方向1句话(具体名单归人物/剧情维度) ③地点动线方向1句话(具体清单归剧情/地图维度) '
            '④修炼/事业/财富/关系/势力五项进展 ⑤伏笔主题方向1-2句(具体条目归剧情/伏笔维度) ⑥卷尾得到/失去/新任务)；'
            '结尾附一张【跨卷尾钩子承接总览】(卷1尾 ←接→ 卷2头 ...)。'
            '总字数不少于 1500 字。'
        ) if dim == 'plot_design' else ''

        # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽正文行文规范/去AI手册）
        master_conception_rules = build_conception_rules(mode='agent', book=book)
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》生成"{"label"}"设定。'
            f'\n\n{core_iron}'
            f'\n\n【构思阶段·平台内置规则（只注入通用核心+构思格式，不注入正文行文规范，不一股脑全加载）】'
            f'\n{master_conception_rules}'
            f'\n\n已有设定参考：\n{ctx_block}'
            f'\n用户补充要求：{instruction or "无"}'
            f'{concept_more}{key_rules_more}{worldbuilding_more}{character_more_master}{plot_design_more}'
            f'\n请直接输出该维度的完整设定内容（构思≥1200、设定≥1500、世界观≥2000、人物≥1800、大纲≥1500字；文风/伏笔/地图如有也不少于1000字），不要寒暄，不要解释，严格按上面分节依次输出，禁止跳节，禁止出现"待设定/后续再定"这类空话。'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
        )
        if existing:
            sys_prompt += f'\n\n已有内容（可在其基础上补充完善，不要简单重复）：\n{existing[:400]}'

        messages = [{'role': 'system', 'content': sys_prompt},
                    {'role': 'user', 'content': f'请生成{label}'}]
        content = ''
        try:
            # master_create 多维度分节清单更长，统一走按维度配额（见 _DIM_MAX_TOKENS 注释）
            _max_tok = _dim_max_tokens(dim)
            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=_max_tok):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                content += chunk
                yield sse({'type': 'delta', 'content': chunk})
        except GeneratorExit:
            # 客户端断开：同步抢救当前维度已生成部分（已完成的维度已随卡片发出，前端有）
            _save_partial_on_disconnect(session, f'批量设定·{label}', instruction or '', content)
            raise
        except Exception as e:
            yield sse({'type': 'error', 'error': f'{label}生成失败：{e}'})
            continue

        content = _clean_text_to_plain(content)
        generated[dim] = content
        # 产出落地卡片
        card = {
            'id': str(uuid.uuid4())[:8],
            'type': card_type,
            'title': f'{label}（AI生成）',
            'content': content,
            'target': _CARD_TARGET.get(card_type, label),
        }
        emitted_cards.append(card)
        yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话（用已发出的卡片对象，保留 id 与前端一致）
    yield from _persist_action_session(session, f'批量生成设定：{instruction or "默认五维度"}',
                                       generated, dims, cards_out=emitted_cards)


# ============================================================================
# 视点感知注入（第三人称有限视角）：正文写作时只给AI看POV角色能知道的信息
# 目标：减少token + 防剧透（不注入未来卷剧情、伏笔谜底、无关人物）
# ============================================================================

def _infer_pov_character(book_id, target_chapter_num, prev_chapter_content):
    """推断当前章的视点人物（POV）。
    策略：从上一章内容中找出现频率最高的已注册人物名；回退到主角；再回退到None。
    """
    from app import Character
    try:
        all_chars = Character.query.filter_by(book_id=book_id).all()
        if not all_chars:
            return None
        # 主角兜底
        protagonist = next((c for c in all_chars if c.role == 'protagonist'), None)
        if not prev_chapter_content:
            return protagonist.name if protagonist else (all_chars[0].name if all_chars else None)
        # 统计每个人物名在上一章出现的次数
        counts = {}
        for c in all_chars:
            n = c.name or ''
            if n and len(n) >= 2:
                counts[n] = prev_chapter_content.count(n)
        # 取出现次数最多的（至少出现1次）
        best = max(counts.items(), key=lambda x: x[1], default=(None, 0))
        if best[1] > 0:
            return best[0]
        return protagonist.name if protagonist else None
    except Exception:
        return None


def _filter_characters_by_pov(book_id, pov_name):
    """人物维度过滤：只注入POV角色 + POV关系网中的人 + 主角。
    返回自然语言文本。大幅减少token，且避免无关人物干扰当前视角。
    """
    from app import Character
    try:
        all_chars = Character.query.filter_by(book_id=book_id).all()
        if not all_chars:
            return ''
        # 确定要注入的人物集合
        target_names = set()
        if pov_name:
            target_names.add(pov_name)
        protagonist = next((c for c in all_chars if c.role == 'protagonist'), None)
        if protagonist:
            target_names.add(protagonist.name)
        # POV的关系网：从relationships_json提取关联人物名
        if pov_name:
            pov_char = next((c for c in all_chars if c.name == pov_name), None)
            if pov_char:
                try:
                    rels = json.loads(pov_char.relationships_json or '[]')
                    for r in rels:
                        if isinstance(r, dict):
                            tn = r.get('target_name') or r.get('name') or r.get('with') or ''
                            if tn:
                                target_names.add(tn)
                except (json.JSONDecodeError, ValueError):
                    pass
        # 过滤 + 转自然语言
        blocks = []
        for c in all_chars:
            if c.name not in target_names:
                continue
            lines = []
            if c.role and c.role != 'supporting':
                role_label = {'protagonist': '主角', 'antagonist': '反派'}.get(c.role, c.role)
                lines.append(f'角色定位：{role_label}')
            for label, val in [('身份描述', c.description), ('外貌', c.appearance),
                               ('性格', c.personality), ('背景', c.background)]:
                v = (val or '').strip()
                if v:
                    lines.append(f'{label}：{v}')
            try:
                rels = json.loads(c.relationships_json or '[]')
                if rels:
                    rel_lines = []
                    for r in rels:
                        if isinstance(r, dict):
                            tn = r.get('target_name') or r.get('name') or r.get('with') or ''
                            rel = r.get('relation') or r.get('type') or ''
                            if tn:
                                rel_lines.append(f'{tn}（{rel}）' if rel else tn)
                    if rel_lines:
                        lines.append('关系：' + '、'.join(rel_lines))
            except (json.JSONDecodeError, ValueError):
                pass
            if lines:
                blocks.append(f'姓名：{c.name}\n' + '\n'.join(lines))
        return '\n\n'.join(blocks)
    except Exception:
        return ''


def _filter_timeline_for_chapter(timeline_raw, target_chapter_num):
    """剧情维度过滤：只注入当前卷及之前卷的剧情。
    当前卷内注入：已发生节点 + 当前节点 + 卷尾钩子（写作目标）。
    不注入后续卷（防剧透）。返回 (文本, 是否截断了后续卷)。
    """
    if not timeline_raw or not timeline_raw.strip():
        return '', False
    text = timeline_raw.strip()
    # 尝试解析为JSON卷列表
    try:
        vols = json.loads(text)
        if not isinstance(vols, list):
            return text, False
        kept = []
        truncated = False
        for v in vols:
            if not isinstance(v, dict):
                continue
            vol_idx = v.get('volume_index') or v.get('volume_id') or '?'
            vol_name = v.get('volume', f'第{vol_idx}卷')
            # 判断该卷是否在target之前或当前
            nodes = v.get('nodes') or []
            vol_has_current = False
            vol_is_past = False
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                ch_range = str(n.get('chapters', ''))
                nums = re.findall(r'\d+', ch_range)
                if len(nums) >= 2:
                    if int(nums[-1]) < target_chapter_num:
                        vol_is_past = True
                    if int(nums[0]) <= target_chapter_num <= int(nums[-1]):
                        vol_has_current = True
            # 只保留"有已发生或当前节点"的卷
            if vol_is_past or vol_has_current:
                vol_lines = [f'第{vol_idx}卷《{vol_name}》']
                main_plot = v.get('main_plot') or ''
                if main_plot:
                    vol_lines.append(f'本卷主线：{main_plot}')
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    ch_range = n.get('chapters', '')
                    summary = n.get('summary') or n.get('plot') or ''
                    if summary:
                        nums = re.findall(r'\d+', str(ch_range))
                        # 当前卷内：只注入已发生+当前节点，未来节点不注入
                        if vol_has_current and len(nums) >= 2:
                            if int(nums[0]) > target_chapter_num:
                                continue  # 跳过本卷未来节点
                        vol_lines.append(f'  · [{ch_range}] {summary}')
                ending_hook = v.get('ending_hook') or ''
                if ending_hook and (vol_is_past or vol_has_current):
                    vol_lines.append(f'卷尾钩子：{ending_hook}')
                kept.append('\n'.join(vol_lines))
            else:
                truncated = True  # 有后续卷被跳过
        return ('\n\n'.join(kept) if kept else text, truncated)
    except (json.JSONDecodeError, ValueError):
        # 纯文本timeline：无法结构化过滤，返回原文但标记
        return text, False


def _get_chapter_plot_node(timeline_raw, outline_hierarchy_raw, target_chapter_num):
    """精确命中本章情节节点（写作/润色时注入，让AI知道"本章该写什么剧情"）。

    优先级：
    1. outline_hierarchy（四级层级，精确到单章+戏剧位置起/承/转/合）
    2. timeline JSON（卷>情节节点，按 nodes.chapters 章节范围匹配单个节点）
    3. 都拿不到 → 返回空串（由调用方走 _filter_timeline_for_chapter 卷级注入兜底）

    返回：拼好的"本章剧情"文本块（含所属卷/节点标题/摘要/戏剧位置），空串表示未命中。
    """
    if not target_chapter_num:
        return ''

    # ---- 优先级1：outline_hierarchy 精确到单章 ----
    if outline_hierarchy_raw and outline_hierarchy_raw.strip():
        try:
            from outline_hierarchy_builder import get_dramatic_context, build_dramatic_position_prompt
            hierarchy = json.loads(outline_hierarchy_raw)
            ctx = get_dramatic_context(hierarchy, target_chapter_num)
            if ctx:
                lines = []
                ch = ctx.get('chapter') or {}
                sec = ctx.get('section') or {}
                arc = ctx.get('arc') or {}
                if arc.get('arc_name'):
                    lines.append(f'所属卷：{arc["arc_name"]}')
                if arc.get('arc_theme'):
                    lines.append(f'卷主题：{str(arc["arc_theme"])[:60]}')
                if sec.get('purpose') or sec.get('title'):
                    lines.append(f'所属情节节点：{sec.get("purpose") or sec.get("title")}')
                if sec.get('summary') or sec.get('section_emotional_arc'):
                    lines.append(f'节点概要：{sec.get("summary") or sec.get("section_emotional_arc")}')
                if ch.get('dramatic_position'):
                    lines.append(f'本章戏剧位置：{ch["dramatic_position"]}')
                if ch.get('content_focus'):
                    lines.append(f'本章重点：{ch["content_focus"]}')
                if lines:
                    return '\n'.join(lines)
        except Exception:
            pass  # 降级到 timeline 匹配

    # ---- 优先级2：timeline JSON 按章号范围匹配单节点 ----
    if not timeline_raw or not timeline_raw.strip():
        return ''
    try:
        vols = json.loads(timeline_raw.strip())
        if not isinstance(vols, list):
            return ''
        for v in vols:
            if not isinstance(v, dict):
                continue
            nodes = v.get('nodes') or []
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                ch_range = str(n.get('chapters', ''))
                nums = re.findall(r'\d+', ch_range)
                if len(nums) >= 2 and int(nums[0]) <= target_chapter_num <= int(nums[-1]):
                    # 命中该节点
                    lines = []
                    vol_name = v.get('volume', f'第{v.get("volume_index","?")}卷')
                    lines.append(f'所属卷：{vol_name}')
                    if v.get('main_plot'):
                        lines.append(f'卷主线：{str(v["main_plot"])[:80]}')
                    node_title = n.get('title', '未命名节点')
                    lines.append(f'所属情节节点：{node_title}（{ch_range}章）')
                    summary = n.get('summary') or n.get('plot') or ''
                    if summary:
                        lines.append(f'节点概要：{summary}')
                    if n.get('cool_type'):
                        lines.append(f'爽点类型：{n["cool_type"]}')
                    if v.get('ending_hook') and int(nums[-1]) == target_chapter_num:
                        lines.append(f'卷尾钩子：{v["ending_hook"]}')
                    return '\n'.join(lines)
        return ''  # 没命中任何节点
    except (json.JSONDecodeError, ValueError):
        return ''


def _filter_foreshadowing_for_chapter(foreshadow_raw, foreshadowing_graph_json, target_chapter_num, bb=None):
    """M3: 伏笔维度过滤：用 ContextBus 算完整任务清单（应埋/应收/禁揭示），fallback 到文本+强约束。"""
    if not target_chapter_num:
        return foreshadow_raw.strip() if foreshadow_raw else ''

    # 优先从结构化 DAG 计算完整任务清单
    if foreshadowing_graph_json and bb:
        try:
            from context_ranker import ContextBus
            mission = ContextBus.get_hook_mission(bb, target_chapter_num)
            if mission:
                return mission + '\n\n【伏笔防剧透铁律】严禁提前揭示未到回收时机的伏笔谜底；POV 未察觉的伏笔只能以客观现象/旁枝线索出现，不能给读者上帝视角。'
        except Exception:
            pass

    # fallback：文本全量注入 + 强约束
    if not foreshadow_raw or not foreshadow_raw.strip():
        return ''
    return (
        foreshadow_raw.strip()
        + '\n\n【伏笔防剧透铁律】以上伏笔中，未到回收时机的严禁揭示谜底；'
        '只允许呼应 POV 已察觉的客观线索。'
    )


def _log_validation_issues(bb, dim_key: str, issues):
    """M4: 将 error 级自检问题写入 FailureDB"""
    if not bb or not issues:
        return
    try:
        from meta_optimizer import log_failure
        for issue in issues:
            if issue.severity != 'error':
                continue
            cat_map = {
                'JSON_INVALID': 'format',
                'VOL_COUNT_MISMATCH': 'structure',
                'VOL_INDEX_GAP': 'structure',
                'CH_NUM_OVERFLOW': 'structure',
                'CH_NUM_UNDERFLOW': 'structure',
                'FIELD_MISSING': 'structure',
                'ACT_COUNT_MISMATCH': 'structure',
                'CHAR_JSON_LEAK': 'format',
            }
            category = cat_map.get(issue.code, 'content')
            log_failure(bb, category, dim_key=dim_key, summary=f'[{issue.code}] {issue.message}',
                        snippet=issue.auto_fix or issue.message, fix_hint=issue.auto_fix)
    except Exception:
        pass


def _get_event_log_ctx(bb, target_chapter_num, limit=5):
    """M2: 从 EventLog 拉取上一章/最近事件，作为本章写作前的"最新动态"。"""
    if not bb or not bb.event_log_json or not target_chapter_num:
        return ''
    try:
        from event_log_manager import EventLogManager
        events = EventLogManager.load(bb)
        if not events:
            return ''
        # 取上一章的事件 + 最近几条更早的事件
        prev_events = [e for e in events if e.chapter_num == target_chapter_num - 1]
        recent = [e for e in events if e.chapter_num < target_chapter_num and e not in prev_events]
        recent.sort(key=lambda x: x.chapter_num, reverse=True)
        selected = (prev_events + recent)[:limit]
        if not selected:
            return ''
        lines = ['【前情提要·事件序列】']
        for e in sorted(selected, key=lambda x: x.chapter_num):
            actors = '、'.join(e.actors) if e.actors else '（无）'
            loc = f'｜地点：{e.location}' if e.location else ''
            lines.append(f'· 第{e.chapter_num}章｜{e.type}｜{actors}{loc}｜{e.summary}')
        return '\n'.join(lines)
    except Exception:
        return ''


def _filter_dynamic_reports_for_chapter(book_id, target_chapter_num, limit=5):
    """动态报告过滤：只注入 chapter_end < target_chapter_num 的报告（已发生事件摘要）。
    防止未来事件泄露给当前章创作。
    """
    from app import DynamicReport
    try:
        reports = DynamicReport.query.filter_by(book_id=book_id) \
            .filter(DynamicReport.chapter_end < target_chapter_num) \
            .order_by(DynamicReport.chapter_end.desc()).limit(limit).all()
        if not reports:
            return ''
        lines = []
        for r in reports:
            title = r.title or f'动态({r.chapter_start}-{r.chapter_end}章)'
            content = (r.content or '').strip()
            lines.append(f'· {title}：\n{content}')
        return '\n\n'.join(lines)
    except Exception:
        return ''




# ========================================================================
# B2：风格对齐 SkillPack（style_packs/ 目录下的 txt 范本自动注入）
# 开关优先级：① style_skill_ids 含禁用词（none/off/禁用等）→ 彻底关
#   ② 含具体 pack id（如 fantasy_xuanhuan_v1）→ 强制启用  ③ 否则按 book.genre 自动匹配
# ========================================================================

_STYLE_PACK_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'style_packs')
_style_manifest_cache: Optional[Dict] = None


def _load_style_manifest() -> Dict:
    global _style_manifest_cache
    if _style_manifest_cache is not None:
        return _style_manifest_cache
    try:
        with open(os.path.join(_STYLE_PACK_ROOT, '_manifest.json'), 'r', encoding='utf-8') as f:
            _style_manifest_cache = json.load(f)
    except Exception:
        _style_manifest_cache = {'packs': [], 'disable_keywords': []}
    return _style_manifest_cache


def _parse_style_ids(book) -> List[str]:
    """从 Book.style_skill_ids（JSON 数组字符串）解析用户显式指定/禁用的文风包 id 列表。"""
    raw = getattr(book, 'style_skill_ids', None) or '[]'
    try:
        arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [str(x).strip() for x in arr if str(x).strip()]
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def _get_enabled_style_pack(book) -> str:
    """返回当前书应该启用的风格 pack 注入正文（空字符串表示不启用任何 pack）。"""
    manifest = _load_style_manifest()
    packs = manifest.get('packs') or []
    if not packs:
        return ''
    # 1) 显式禁用
    explicit = _parse_style_ids(book)
    disabled = {k.lower() for k in (manifest.get('disable_keywords') or [])}
    if any(e.lower() in disabled for e in explicit):
        return ''
    # 2) 显式指定 pack id（只取第一个命中的）
    if explicit:
        for p in packs:
            if p.get('id') in explicit:
                try:
                    with open(os.path.join(_STYLE_PACK_ROOT, p['file']), 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception:
                    return ''
    # 3) 按 genre 自动匹配
    genre = (getattr(book, 'genre', None) or 'other').strip()
    if not genre:
        return ''
    genre_low = genre.lower()
    matched = None
    for p in packs:
        if not p.get('enabled_by_default', True):
            continue
        for g in (p.get('genre_match') or []):
            if not g:
                continue
            gl = str(g).lower()
            if gl in genre_low or genre_low in gl:
                matched = p
                break
        if matched:
            break
    if not matched:
        return ''
    try:
        with open(os.path.join(_STYLE_PACK_ROOT, matched['file']), 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''




def _action_chapter(book, session, instruction, gw, sse, target_chapter_num, prev_chapter_content, mode,
                    base_url=None, api_key=None, model=None, skill_pack_ids=None):
    """续写/润色本章正文：产 SAVE_CHAPTER 卡。
    视点感知注入（第三人称有限视角）：只给AI看POV角色能知道的信息，减少token + 防剧透。
    - 人物：只注入POV + POV关系网 + 主角
    - 剧情：只注入当前卷及之前卷（当前卷内只注入已发生节点）
    - 动态报告：只注入target章之前的报告
    - 伏笔：注入但加强约束（严禁揭示未到回收时机的谜底）
    - 世界观/规则/文风/地点/构思：全量注入（写作基础，不剧透）
    """
    # === SSE 双兜底：正文阶段上下文组装耗时较久（DB查询+规则拼接=200-500ms），先yield一帧占住连接 ===
    mode_label = '续写' if mode == 'continue' else '润色'
    yield sse({'type': 'delta', 'content': f'\n正在{mode_label}第 {target_chapter_num or "?"} 章…\n\n'})
    from app import db, BookBible, Chapter, _get_total_volumes, _get_chapters_per_volume, parse_chapter_number
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ====== 核心创作参数铁律 + 越界硬拦截（正文写作第一道门） ======
    tv = _get_total_volumes(bb, book)
    cpv = _get_chapters_per_volume(bb, book)
    max_chapters = tv * cpv  # 总章数上限 = 总卷数 × 每卷章数
    core_iron = _core_params_iron_block(bb, book)

    # 统一口径：从章节表提取最新章节号（写作/修改/去AI共用）
    ch_info = _get_latest_chapter_info(book_id)
    # 确定当前章号 + 上一章内容
    if not target_chapter_num:
        # 续写用“最新章节号+1”，润色用“最新章节号”
        target_chapter_num = ch_info['next_num'] if mode == 'continue' else ch_info['latest_num']

    # 越界硬拦截：章号超过总章数上限立即停止，不发 LLM 请求
    if target_chapter_num and target_chapter_num > max_chapters:
        yield sse({'type': 'error',
                   'error': (f'【核心参数越界拦截】全书设定总卷数 {tv} 卷 × 每卷 {cpv} 章 = 总章数上限 {max_chapters} 章，'
                             f'当前请求第 {target_chapter_num} 章已超出上限。若需要继续写作，请先到作品基本信息中调大总卷数。')})
        return

    if not prev_chapter_content:
        # 统一口径：target_chapter_num 是 1-based 章号；
        # 找"上一章" = 找 display 章号为 target_chapter_num - 1 的章节，
        # order_index 最大不超过 (target_chapter_num - 1) - 1 = target_chapter_num - 2
        # （因为纯 order_index 兜底时 displayNum = order_index + 1）
        prev = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .filter(Chapter.order_index < target_chapter_num - 1) \
            .order_by(Chapter.order_index.desc()).first()
        # 但若上面没找到（比如中间有删章/标题章号与顺序错位），回退到最接近 target_chapter_num 之前的那一章
        if not prev:
            # 按标题解析 displayNum < target_chapter_num 的最大那章，兜底用 order_index
            all_prev_candidates = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
            best_prev, best_display = None, 0
            for cp in all_prev_candidates:
                p_num = parse_chapter_number(cp.title or '') or (cp.order_index + 1 if cp.order_index is not None else 0)
                if p_num < target_chapter_num and p_num > best_display:
                    best_prev, best_display = cp, p_num
            prev = best_prev
        # 再回退：若按上面取不到，用统一口径的最新章节
        if not prev and ch_info['latest_chapter'] and mode == 'continue':
            prev = ch_info['latest_chapter']
        prev_chapter_content = (prev.content or '')[:2000] if prev else ''

    # 推断POV视点人物
    pov_name = _infer_pov_character(book_id, target_chapter_num, prev_chapter_content)

    # === 视点感知上下文构建 ===
    # 1. 人物维度：只注入POV + 关系网 + 主角（大幅省token，防无关人物干扰）
    char_ctx = _filter_characters_by_pov(book_id, pov_name)
    # 若Character表为空，回退到bible.character_profiles
    if not char_ctx and bb and bb.character_profiles:
        cp = bb.character_profiles.strip()
        if cp.startswith('['):
            char_ctx = _character_profiles_to_text(cp)
        else:
            char_ctx = cp

    # 2. 剧情维度：
    #    2a. 精确命中本章情节节点（让AI知道"本章该写什么"）
    #    2b. 当前卷及之前卷的剧情脉络（宏观补充，防后续卷剧透）
    chapter_plot_ctx = ''
    if bb:
        chapter_plot_ctx = _get_chapter_plot_node(
            bb.timeline, bb.outline_hierarchy, target_chapter_num)

    timeline_ctx, timeline_truncated = '', False
    if bb and bb.timeline:
        timeline_ctx, timeline_truncated = _filter_timeline_for_chapter(bb.timeline, target_chapter_num)

    # 3. 动态报告：只注入target章之前的（防未来事件泄露）
    dynamic_reports_ctx = _filter_dynamic_reports_for_chapter(book_id, target_chapter_num)

    # 4. 伏笔：注入但后续prompt加强约束
    foreshadow_ctx = _filter_foreshadowing_for_chapter(
        bb.foreshadowing if bb else '',
        bb.foreshadowing_graph if bb else '',
        target_chapter_num,
        bb)

    # M2: 事件序列上下文（前情提要）
    event_log_ctx = _get_event_log_ctx(bb, target_chapter_num)

    # 5. 世界观/规则/文风/地点/构思/大纲：全量注入（写作基础，不剧透）
    #    大纲(plot_design)是总纲，注入但加约束"仅作宏观方向，不可剧透未发生转折"
    static_dims = []
    for d in SMART_DIMENSIONS:
        if d['key'] in ('character_profiles', 'timeline', 'foreshadowing'):
            continue  # 这三个已单独处理
        v = (getattr(bb, d['field'], '') or '').strip() if bb else ''
        if v:
            static_dims.append(f'【{d["label"]}】\n{v}')
    static_ctx = '\n\n'.join(static_dims)

    # === ONE 主钩子 + 本章剧情数字硬约束（与core_iron并列，优先级高于所有设定/规则/字数）===
    # 从 chapter_plot_ctx 里推断 ONE 主钩子（取第一个节点里的核心对象/核心任务）
    chapter_plot_iron = ''
    if chapter_plot_ctx:
        chapter_plot_iron = (
            '【本章剧情·最高指令】接上一章剧情，读取剧情维度里的「本章剧情节点」，保证ONE主钩子贯穿本章。禁止无目标流水账。'
            f'\n\n本章必须写完且只写以下 {len([x for x in chapter_plot_ctx.splitlines() if x.strip()])} 个节点（顺序不得调换、不得跳过、不得新增）：'
            f'\n{chapter_plot_ctx}'
            '\n\n【本章字数铁律】纯正文（不含标题）2300-2500字，全角中文标点；不足扩场景细节/对话停顿/POV感官（温度/气味/触感/视线压力），超了删枝节。（⚠️ 遵守【禁令0】不得凑字/超标）'
        )

    # === B2 风格对齐 SkillPack：自动匹配玄幻/都市范本，注入在 chapter_plot_iron 之后（第 3 高位）===
    style_pack_prompt = _get_enabled_style_pack(book)

    # === 事前生成禁令·6条（反例矿道文暴露出的核心硬约束，第3高位，优先级最高档）===
    #   注入位置：core_iron → ONE主钩子 → 【事前禁令7条】 → style_pack → writing_rules
    #   ⚠️ 禁令5（段均句数≤1.8，不卡总段数）已移到 GENERAL_CORE_RULES 总则里全阶段通用，这里不再重复写 2 遍。
    PRE_GENERATE_BAN_RULES = '''【事前生成禁令·6条（比任何规则都高，违反直接扣分/重写）】
禁令0·禁AI修正式否定句密集症 → 【详见通用核心铁律·禁令0】；禁令5·漫画分镜一句话一段话 → 【详见通用核心铁律·禁令5+铁律A+行文规范段落合并节】（以上两条下文均有完整细则，此处不复述）
禁令1·禁背景板一次性名字段≥8段：群像围观（点卯台/集会/街道/战场围观群众）只允许1段集中描写；严禁把 8 个以上路人/背景板角色各自拆成独立一段，必须合成≤2段【详见行文规范群像并列节】。
禁令2·禁空转流水账重复动作：任何重复性劳作/推进行为 → 必须遵守【行文规范黄金4型·动作链】"小目标→决策→验证"每 200-300 字一个闭环，严禁只写"继续/还在/接着做"无验证句。
禁令3·禁对白提示语句首+对白独立段：对白提示语句首占比 ≤40%；连续 3 句对白至少 1 句与动作/反应同段，禁止每句对白单独成段像剧本台词【详见行文规范对白节】。
禁令4·禁群像平级并列段：点卯台/围观/集会/站队类多角色场景，必须有≥1条「递进比较链」；不许 3 段以上平级并列堆段【详见行文规范黄金4型·递进比较链】。
禁令6·禁结尾真相直白点破：结尾为真相揭露时绝对禁止直接陈述句点破，必须改写成动态动作句收尾【详见行文规范结尾钩子·真相直白点破禁】。'''


    # 组装上下文块（本章剧情已提上去写在core_iron之后，这里bible_ctx里就不再重复chapter_plot_ctx）
    ctx_blocks = []
    if static_ctx:
        ctx_blocks.append(static_ctx)
    if char_ctx:
        pov_note = f'（本章视点人物：{pov_name}，第三人称有限视角，只写{pov_name}能感知到的事物）' if pov_name else '（第三人称有限视角）'
        ctx_blocks.append(f'【人物档案·视点感知】{pov_note}\n{char_ctx}')
    if timeline_ctx:
        trunc_note = '\n（注：后续卷剧情已省略，防剧透）' if timeline_truncated else ''
        ctx_blocks.append(f'【本卷及过往剧情脉络】{trunc_note}\n{timeline_ctx}')
    if foreshadow_ctx:
        ctx_blocks.append(f'【伏笔线索】\n{foreshadow_ctx}')
    if event_log_ctx:
        ctx_blocks.append(event_log_ctx)
    if dynamic_reports_ctx:
        ctx_blocks.append(f'【近期动态文件（已发生事件摘要）】\n{dynamic_reports_ctx}')
    bible_ctx = '\n\n'.join(ctx_blocks) or '（暂无设定）'

    # 【P2改进】长篇上下文相关性加权裁剪：避免低相关内容膨胀占满 token
    # 仅在 bible_ctx 较长时触发（短篇直接全量注入，无裁剪开销）
    try:
        from context_ranker import ContextRanker, ContextChunk
        _ctx_total_chars = len(bible_ctx)
        # 粗估 token：中文约 2 字/token，超过 4000 token（约 8000 字）才触发裁剪
        if _ctx_total_chars > 8000:
            # 把 ctx_blocks 拆成带标签的 chunks 用于加权排序
            raw_chunks = []
            for d in SMART_DIMENSIONS:
                if d['key'] in ('character_profiles', 'timeline', 'foreshadowing'):
                    continue
                v = (getattr(bb, d['field'], '') or '').strip() if bb else ''
                if v:
                    raw_chunks.append(ContextChunk(
                        dim_key=d['key'], label=d['label'], content=v,
                        priority=ContextRanker.BASE_PRIORITY.get(d['key'], 3)
                    ))
            if char_ctx:
                raw_chunks.append(ContextChunk('character_profiles', '人物档案', char_ctx, priority=1))
            if chapter_plot_ctx:
                raw_chunks.append(ContextChunk('timeline', '本章剧情', chapter_plot_ctx, priority=1))
            if timeline_ctx:
                raw_chunks.append(ContextChunk('timeline', '本卷剧情脉络', timeline_ctx, priority=1))
            if foreshadow_ctx:
                raw_chunks.append(ContextChunk('foreshadowing', '伏笔线索', foreshadow_ctx, priority=3))
            if event_log_ctx:
                raw_chunks.append(ContextChunk('event_log', '前情提要', event_log_ctx, priority=2))
            if dynamic_reports_ctx:
                raw_chunks.append(ContextChunk('dynamic', '近期动态', dynamic_reports_ctx, priority=2))
            if raw_chunks:
                ranker = ContextRanker(max_tokens=4000)
                pov_name_for_rank = pov_name or None
                ranked = ranker.rank_for_chapter(raw_chunks, target_chapter_num, pov_name_for_rank, book_id)
                # 重新组装带 POV 注解的 bible_ctx
                ranked_parts = []
                for c in ranked:
                    if c.dim_key == 'character_profiles':
                        pov_note = f'（本章视点人物：{pov_name}，第三人称有限视角，只写{pov_name}能感知到的事物）' if pov_name else '（第三人称有限视角）'
                        ranked_parts.append(f'【人物档案·视点感知】{pov_note}\n{c.content}')
                    elif c.dim_key == 'timeline' and c.label == '本章剧情':
                        # 本章剧情已写在core_iron之后 chapter_plot_iron 段（位置最高），此处避免重复
                        continue
                    elif c.dim_key == 'timeline':
                        ranked_parts.append(f'【本卷及过往剧情脉络】\n{c.content}')
                    elif c.dim_key == 'foreshadowing':
                        ranked_parts.append(f'【伏笔线索】\n{c.content}')
                    elif c.dim_key == 'event_log':
                        ranked_parts.append(c.content)
                    elif c.dim_key == 'dynamic':
                        ranked_parts.append(f'【近期动态文件（已发生事件摘要）】\n{c.content}')
                    else:
                        ranked_parts.append(f'【{c.label}】\n{c.content}')
                bible_ctx = '\n\n'.join(ranked_parts) or '（暂无设定）'
    except Exception:
        pass  # 裁剪失败时回退到全量注入

    # 防剧透硬约束（伏笔+大纲）
    anti_spoiler_rule = (
        '\n\n【防剧透铁律·第三人称有限视角】'
        '\n1. 严禁在正文中揭示伏笔的谜底/真相，只能呼应POV已察觉的表象线索'
        '\n2. 严禁写出POV不在场时发生的事件（POV不知道的事不能写）'
        '\n3. 大纲/总纲中的未来转折不可在当前章节提前泄露'
        '\n4. 严禁出现POV不认识的人物内心活动（只能通过POV观察推测他人）'
    )

    mode_label = '续写' if mode == 'continue' else '润色'
    yield sse({'type': 'delta', 'content': f'正在{mode_label}第 {target_chapter_num} 章…\n\n'})

    if mode == 'polish':
        # 润色：按章节号定位原文（与前端 displayChapterNum 口径一致：优先标题解析，回退 order_index+1）
        cur = None
        candidates = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        # ① 优先：标题能解析出 target_chapter_num（比如"第3章 觉醒"→3）
        for c in candidates:
            if parse_chapter_number(c.title or '') == target_chapter_num:
                cur = c
                break
        # ② 回退：按 displayChapterNum 兜底（order_index + 1 = target_chapter_num），即 order_index = target_chapter_num - 1
        if not cur:
            cur = next((c for c in candidates if (
                (parse_chapter_number(c.title or '') is None or parse_chapter_number(c.title or '') == 0)
                and c.order_index is not None and c.order_index + 1 == target_chapter_num
            )), None)
        # ③ 最终兜底：直接查 order_index = target_chapter_num - 1
        if not cur:
            cur = Chapter.query.filter_by(book_id=book_id, is_volume=False,
                                           order_index=target_chapter_num - 1).first()
        if not cur or not (cur.content or '').strip():
            yield sse({'type': 'error', 'error': f'第 {target_chapter_num} 章无正文，无法润色'})
            return
        cur_len = len((cur.content or '').strip())
        # 正文阶段·专属规则（只含通用核心+行文规范+文风技能包，屏蔽构思专属规则）
        writing_rules = build_writing_rules(book, skill_pack_ids, extra_style_pack=style_pack_prompt)
        sys_prompt = (
            f'你是资深网文润色编辑。请润色《{book.title}》第 {target_chapter_num} 章正文。'
            f'\n\n{core_iron}'
            f'\n\n{chapter_plot_iron if chapter_plot_iron else ""}'
            f'\n\n{PRE_GENERATE_BAN_RULES}'
            f'\n\n{writing_rules}'
            f'\n要求：保持剧情和人物不变，优化文笔节奏，提升画面感。（⚠️ 【禁令0】任何情形不得写「不是X是Y/更X更Y更Z」修正句式/排比三连）'
            f'\n用户要求：{instruction or "无"}'
            f'\n\n【输出格式】第一行章节标题（如"第{target_chapter_num}章 标题"），第二行空行，第三行起纯正文。'
            f'\n【字数铁律】纯正文（不含标题）2300-2500字，全角中文标点；原文{cur_len}字：不足扩，超了删，区间内保持篇幅。'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【原文】\n{cur.content}'
            f'{anti_spoiler_rule}'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
            f'\n\n直接输出，不要解释，不要附字数统计。'
        )
        user_msg = f'请润色第 {target_chapter_num} 章'
    else:
        # 正文阶段·专属规则（只含通用核心+行文规范+文风技能包，屏蔽构思专属规则）
        writing_rules = build_writing_rules(book, skill_pack_ids, extra_style_pack=style_pack_prompt)
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》续写第 {target_chapter_num} 章正文。'
            f'\n\n{core_iron}'
            f'\n\n{chapter_plot_iron if chapter_plot_iron else ""}'
            f'\n\n{PRE_GENERATE_BAN_RULES}'
            f'\n\n{writing_rules}'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【上一章结尾】\n{prev_chapter_content or "（第一章）"}'
            f'\n用户要求：{instruction or "自然推进剧情"}'
            f'\n\n【输出格式】第一行章节标题（如"第{target_chapter_num}章 标题"），第二行空行，第三行起纯正文。'
            f'\n【字数铁律】纯正文（不含标题）2300-2500字，用全角中文标点；不足扩场景细节/对话停顿/POV感官（温度/气味/触感/视线压力），超了删枝节。（⚠️ 遵守【禁令0】不得凑字/超标）'
            f'{anti_spoiler_rule}'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
            f'\n\n直接输出，不要解释，不要附字数统计。'
        )
        user_msg = f'请续写第 {target_chapter_num} 章'

    messages = [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': user_msg}]
    content = ''
    try:
        for chunk in gw_stream_with_hb(gw, messages, temperature=0.85, max_tokens=4096):
            if chunk is HEARTBEAT:
                yield SSE_HEARTBEAT_COMMENT
                continue
            content += chunk
            yield sse({'type': 'delta', 'content': chunk})
    except GeneratorExit:
        # 客户端断开（锁屏/切后台/网络切换）：同步抢救已生成的半章正文再退出
        _save_partial_on_disconnect(session, f'{mode_label}第 {target_chapter_num or "?"} 章',
                                    instruction or '', content)
        raise
    except Exception as e:
        yield sse({'type': 'error', 'error': f'{mode_label}失败：{e}'})
        return

    # 剥离标题行：card.content 只存纯正文，card.title 用 AI 生成的章节名（去 # 标记）
    # 先执行一次平台级纯文本清理（去 * 和 #），再剥离标题行
    content = _clean_text_to_plain(content)
    extracted_title, body_content = _strip_chapter_title(
        content, fallback_title=f'第{target_chapter_num}章')

    # 【字数铁律】用 count_words 校验纯正文字数（与章节保存/列表显示口径一致）
    # AI 自数字数往往偏高（把半角标点/空白也算进去），实际 count_words 常偏低约 200 字
    # 不在 2300-2500 区间则调 _ensure_word_count 重写补正
    from app import count_words, _ensure_word_count
    draft_wc = count_words(body_content)
    if (draft_wc < 2300 or draft_wc > 2500) and api_key and base_url and model:
        yield sse({'type': 'delta', 'content': f'\n\n[字数校验] 初稿 {draft_wc} 字，正在修正至 2400±100…'})
        # _ensure_word_count 内部是 requests.post 同步阻塞（非流式），可能 TTFT>30s 触发 Render idle timeout
        # → 用 _run_blocking_with_heartbeat 包一下，后台线程跑阻塞函数，主 generator 每 10s yield 1 帧心跳
        yield SSE_HEARTBEAT_COMMENT  # 先打 1 帧，占住连接

        def _wc_blocking_call():
            return _ensure_word_count(
                body_content, api_key=api_key, base_url=base_url,
                model=model, max_tokens=4096, chapter_num=target_chapter_num,
                count_fn=count_words)

        _wc_result = yield from _run_blocking_with_heartbeat(
            _wc_blocking_call, sse,
            extra_frames=[sse({'type': 'delta', 'content': '…'})])
        corrected, wc_note = _wc_result if _wc_result is not None else (None, None)
        if corrected and corrected.strip() and count_words(corrected) != draft_wc:
            # 修正后字数更接近目标，采用修正版（再剥一次标题防御 + 纯文本清理）
            corrected = _clean_text_to_plain(corrected)
            _, body_content = _strip_chapter_title(
                corrected, fallback_title=extracted_title)
            final_wc = count_words(body_content)
            yield sse({'type': 'delta', 'content': f'\n[字数校验] 已修正至 {final_wc} 字。'})
        elif wc_note:
            yield sse({'type': 'delta', 'content': f'\n[字数校验] {wc_note}'})

    card = {
        'id': str(uuid.uuid4())[:8],
        'type': 'SAVE_CHAPTER',
        'title': extracted_title,
        'content': body_content,
        'target': '章节正文',
    }
    yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话（用已发出的卡片对象，保留 id 与前端一致，采纳状态可回写）
    yield from _persist_action_session(session, f'{mode_label}第{target_chapter_num}章：{instruction or ""}',
                                       {f'chapter_{target_chapter_num}': body_content},
                                       [f'chapter_{target_chapter_num}'],
                                       cards_out=[card])


def _persist_action_session(session, title, generated, dims, cards_out=None):
    """动作执行完后持久化会话消息。

    cards_out: 动作函数已生成并发给前端的卡片对象列表（含 id/title/type/content/target）。
               若提供，直接用这些卡片持久化（保留 id，与前端一致，确保采纳状态可回写）；
               若不提供，回退到旧逻辑（按 dims 从 generated 取内容，重新生成 id）。
    """
    from app import db
    history = load_session_messages(session)
    history.append({'role': 'user', 'content': title})
    # 优先用动作函数已发出的卡片对象（id 与前端一致，采纳/编辑/忽略状态可回写）
    if cards_out:
        cards = [{**c, 'status': 'pending'} for c in cards_out if c]
    else:
        # 兼容旧调用：按 dims 从 generated 取内容，重新生成 id（不推荐，id 会与前端不一致）
        cards = []
        for dim in dims:
            c = generated.get(dim)
            if c:
                card_type = _DIM_TO_CARD.get(dim, 'SAVE_CHAPTER') if dim != dims[0] or 'chapter' not in dim else 'SAVE_CHAPTER'
                if 'chapter' in dim:
                    card_type = 'SAVE_CHAPTER'
                cards.append({
                    'id': str(uuid.uuid4())[:8],
                    'type': card_type,
                    'title': _DIM_LABELS.get(dim, dim),
                    'content': c,
                    'target': _CARD_TARGET.get(card_type, dim),
                    'status': 'pending',
                })
    history.append({'role': 'assistant', 'content': title, 'cards': cards})
    # session.title 先改好，_safe_save_session_messages 的断连重试分支会同步它
    if not session.title or session.title == 'AI动作':
        session.title = title[:30]
    _safe_save_session_messages(session, history)
    yield f'data: {json.dumps({"type": "done", "session_id": session.id}, ensure_ascii=False)}\n\n'


# 维度标签/卡片目标映射（供动作调度用）
_DIM_LABELS = {
    'concept': '核心构思', 'key_rules': '核心规则', 'worldbuilding': '世界观',
    'character_profiles': '人物档案', 'plot_design': '剧情大纲',
    'timeline': '时间线', 'locations': '地点', 'style_guide': '文风指南',
}
_CARD_TARGET = {
    'SAVE_CONCEPT': '核心构思', 'SAVE_RULE': '核心规则', 'SAVE_WORLDSETTING': '世界观',
    'SAVE_CHARACTER': '人物', 'SAVE_OUTLINE_NODE': '大纲', 'SAVE_PLOT': '剧情线',
    'SAVE_LOCATION': '地点', 'APPLY_STYLE': '文风', 'SAVE_CHAPTER': '章节正文',
    'SAVE_FORESHADOW': '伏笔',
}


# ============================================================================
# 纯文字排版统一约束：平台所有生成内容（正文/大纲/设定/人物/卡片内容）
# 一律去除 Markdown 符号 * 和 #，保留中文数字+顿号/句号/空格/空行排版
# ============================================================================

# -------------------------------------------------------------------------
# 剧情时间线（timeline）维度专用：输出是 JSON 数组，但 main_plot / core_conflict /
# ending_hook / nodes[].title / nodes[].summary / nodes[].cool_type 等字段都是
# 面向读者的自然语言文本，同样必须遵守叙事工艺铁律。格式与排版约束做了 JSON 兼容
# 改写，避免影响数组语法合法。
# -------------------------------------------------------------------------
TIMELINE_NARRATIVE_RULES = ("""
【叙事工艺铁律·剧情维度专用·JSON 字段文本必须遵守】
你输出的是按卷 JSON 数组，但以下所有自然语言文本字段同样要遵守叙事工艺铁律：
  - volume 卷名
  - summary 每卷总体剧情概要（覆盖整卷，150-250字）
  - main_plot 本卷主线剧情（卷内主线推进路径，100-160字）
  - core_conflict 核心冲突
  - ending_hook 卷尾钩子
  - main_events[].title 主要剧情事件标题
  - main_events[].summary 主要剧情事件概要
  - main_events[].bury / main_events[].payoff（伏笔埋收）
  - nodes[].title 节点标题
  - nodes[].summary 节点概要
  - nodes[].cool_type 爽感类型

以上字段文本内容必须遵守：

【0. 总则】
· 写事为主，景一笔带过；多用对话推进，动作与神态紧跟对话；叙述句以逗号长句为主（20-35字串1-2个动作），短句只做重拍。
· 爽点直白清晰，深层动机让读者脑补；转折来自已埋伏笔或人物动机，禁天降巧合。
· 核心口诀：行动往上浮，动机往下潜；先让读者爽，再让他细思极恐。

【1. 冰山与结构】
· 水上1/8（情节/爽点/打脸/升级/赚钱）清晰直白，直接喂给读者；
· 水下7/8（动机/伏笔/创伤/执念/世界观深层）让读者能脑补，不写成设定说明书。
· 每卷 ending_hook 必须是动态悬念/冲突/转折，禁抒情总结升华。
· main_events[] 中的主要剧情事件必须能被后续章节回收（契诃夫之枪原则）。
· 节点设计（nodes[]，由用户点「节点设计」生成）要承接对应 main_event，不能脱离主线。

【2. 人物】
· summary / main_plot / main_events[].summary / nodes[].summary 中禁止贴"冷酷/温柔/腹黑"这类标签，须用反常行为刻画；
· 水面行为下埋可脑补动机；背景只露一角；人物有瑕疵/纠结/口是心非；禁完美人设。

【3. 去 AI 味】
· 禁信号词：这说明/这意味着/由此可见/换句话说/事实上/显然/本质上/归根结底/不得不说/毋庸置疑/因此/然而/与此同时/总而言之/综上所述；
· 禁工整句式：多段排比三连/段尾总总结升华/路标词密集/观点句+解释+段尾总结模板；
· 禁典型 AI 短语：一股杀气/一抹笑意/不由得/不禁/随即/与此同时/缓缓/淡淡/微微/眼中闪过一丝/心中暗想/心念电转/恍然大悟/面无表情/淡漠/眸子/嘴角微微上扬/如同/宛如/犹如/周身/威压/那道身影/话音未落/当即/顿时；
· 推荐口语化表达：合着/整半天/好家伙/说白了/得了吧/啥情况/搁这/没跑了/差不离/差不多得了/说实话；
· summary / main_plot / ending_hook / 事件概要 / 节点概要结尾停在动态动作或悬念，禁总结升华句；
· 爽感类型（cool_type）用精确分类名（实力碾压/智商碾压/扮猪吃虎/打脸装逼/信息差爽感/情感爆发/悬念反转…），不说空话。

【4. 伏笔埋收标注铁律（绝对要填）】
· 主要剧情事件（main_events）和节点（nodes）都要明确标注：哪里埋了什么伏笔、后面哪一卷/哪一章回收。
· 埋伏笔字段：bury = "第XX章（或第X卷前期/中期/后期）埋下：XXX；预计回收：第YY章（第Z卷）"
· 回收伏笔字段：payoff = "第XX章回收：前文第YY章埋下的XXX；效果：XXX"
· 若无埋/收，字段留空字符串，但绝不乱填。
· 卷与卷的连贯：第N卷最后一个 main_event 的 payoff 允许关联第N+1卷的 bury 或第N-1卷伏笔回收。

【5. JSON 兼容排版约束】
· 为保持 JSON 语法合法，所有字符串值内：
  1）绝对禁止出现未转义的反斜杠 \\ ；
  2）绝对禁止出现未转义的双引号 " ；
  3）绝对禁止 Markdown 符号 * 、 # 、 行首 - 、 > 引用、 ``` 代码块；
  4）列表/条目用"一、二、三、"或"1）2）3）"或"其一其二"，不要 1. 2. 3. 编号；
  5）强调用书名号《》或中文引号，不要 **加粗** 不要 *斜体* 。
· 直接写干净中文短句，段落感用中文标点自然体现。

【6. 自检清单】
· 设定一致：人物行为/性格/语言与大纲一致；势力数量/分布/关系一致；战力不超设定；物品/技能不超前；关系转变有铺垫。
· 卷间连贯：第N卷 ending_hook 与第N+1卷开头严格衔接；各卷 main_events 连续编号不重叠；卷间伏笔埋收跨卷对应。
· AI 味特征：总结升华/排比抒情/比喻/拟人每千字超过3处或命中 8 大 AI 套话词（宛如/犹如/恍若/宛若 + 大海/巨龙/深渊/星河）/评价旁白/对称结构/三连排/解释性叙述/**解释性排比「不是A是B」三连**（⚠️ 自我修正型"不是X，准确说是Y"保留） → 即砍。
· 主要剧情事件：每个 main_event 的 title+summary 必须是一个明确的、可用约5章展开的事件推进（10个≈支撑50章12万字），不是空话。
""").strip()

# 构思阶段专用：输出格式约束（剧情维度的 JSON 兼容、伏笔标注铁律等）
CONCEPTION_EXTRA_RULES = TIMELINE_NARRATIVE_RULES

PLAIN_TEXT_LAYOUT_RULES = """
【纯文字排版铁律·平台级约束·所有输出必须遵守】
（本条对正文、大纲、设定、人物、世界观、伏笔等所有内容生效；Action Card 内的卡片标题和卡片内容也必须遵守）

一、平台级排版约束（只负责排版格式，不包含任何写作规则/行文规范——那些由各阶段专属 build_*_rules() 精准注入：构思=通用核心+构思格式，正文=通用核心+行文规范，去AI=通用核心+去AI手册，绝不一股脑全加载）

1. 绝对禁止任何 Markdown 标记符号，包括：
   一）禁止 # 开头的标题（不要写 # 标题、## 二级标题这类形式）
   二）禁止 * 作为强调/列表/斜体/粗体（不要写 *xxx*、**xxx**、行首 * 列表）
   三）禁止行首 - 短横线列表（" - xxx" / "- xxx" 都不允许）
   四）禁止行首 > 引用块
   五）禁止 ``` 代码块
   六）禁止用 1. / 2. / (1) 这类编号列表符号
2. 正确的纯文字排版形式：
   一）分节标题：直接写成“第一幕：XXX”“本卷目标”“第3卷·XX卷”“主角名”等，前后各空一行即可（不要加#、不要加*）
   二）条目列表：用“一、二、三、…”“1）2）3）…”“甲、乙、丙…”或中文顿号直接并列，缩进用空格，禁止用 - 或 * 或 1. 开头
   三）强调/专有名词：直接用书名号《》、引号“”或不加符号即可，不要用 **粗体** 或 *斜体*
3. 章节正文专属：只输出自然叙述，段落直接用换行/空行分隔。除对话中的正常标点外，正文内容里也不能出现 * 和 # 符号本身。
4. 检查自检：你输出的完整文本中如果出现了独立的 * 字符（除正常数学乘号含义外，极少用到）或行首 #，一律删掉或改成等价中文形式再输出。
""".strip()


# 拒答/客套模板关键词：如果内容主要由这些构成（即使有几百字），也算"有效为空"，应当作 EMPTY_OUTPUT 触发重试
_REFUSAL_OR_FLUFF_PATTERNS = [
    re.compile(r'(?:作(?:为|成).{0,6}AI|我.{0,6}(?:无法|不能|抱歉|抱歉.{0,4}无法|做不到))', re.S),
    re.compile(r'(?:好的|没问题|收到|明白|了解|好哒|好嘞|OK)[，。！,.!\s]*$', re.S),
    re.compile(r'(?:我来(?:帮你|为你|给你)|我会(?:帮你|为你|给你)|下面我(?:将|会|来))[^。！\n]{0,30}$', re.S),
]
# 合理客套最大长度：如果内容 <= 这个长度 且 主要是客套/道歉，则视为空
_FLUFF_MAX_LEN = 60


def _strip_think_tags(text: str) -> str:
    """统一剥离推理模型的 <think>...</think> 标签。
    很多深度推理模型（R1 系列）会先吐出一大段 <think> 内省文字，占满 max_tokens 后正文还没开始，
    结果就是 raw_joined 很长但实际全是 think，清理后内容为空 → 触发 EMPTY_OUTPUT。
    这里在所有内容清理前先把 think 整块剥离，让正文真正进入后续流程。
    """
    if not text:
        return ''
    s = text
    # 标准配对 <think>...</think>（允许跨行、贪婪匹配到最后一个闭合）
    s = re.sub(r'<think[^>]*>[\s\S]*?</think>', '', s, flags=re.IGNORECASE)
    # 兼容未闭合的 <think ...>（开到文尾）
    s = re.sub(r'<think[^>]*>[\s\S]*$', '', s, flags=re.IGNORECASE)
    # 兼容 </think> 残留闭合标签（有闭合没开始）
    s = re.sub(r'</think[^>]*>', '', s, flags=re.IGNORECASE)
    return s


def _is_refusal_or_fluff(text: str) -> bool:
    """判断内容是否是"拒答/客套道歉/承诺开头但没实质内容"。
    这是模型实际"吐字了但等于没吐"的一大类，是 EMPTY_OUTPUT 的隐性来源。
    """
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    for p in _REFUSAL_OR_FLUFF_PATTERNS:
        if p.search(s) and len(s) <= _FLUFF_MAX_LEN:
            return True
    return False


def _clean_text_to_plain(text: str) -> str:
    """统一后处理：移除 Markdown (#、##、###, **xxx**, *xxx*, 行首 *, 行首 -, 代码块 ```，数字. 列表前缀)
    同时保留中文顿号数字+顿号排版（一、二、三、1）2））。输出排版好看的纯文字。
    注意：只做无损清理（如 # 标题 改成 标题；行首 - xxx 改成 　　xxx；**加粗** 去掉加粗符；``` 代码块去掉包裹）。
    """
    if not text:
        return ''
    s = _strip_think_tags(text)  # 先去 think 标签（推理模型常见前置垃圾）
    # 1) 三重反代码块围栏（整行 ``` 或 ```lang）
    s = re.sub(r'^```[a-zA-Z0-9_\-]*\s*$', '', s, flags=re.M)
    # 2) 行首 #/##/### + 空格 → 改成原标题文本（前置空一行 + 标题）
    s = re.sub(r'^#{1,6}\s+', '', s, flags=re.M)
    # 3) **粗体** / *斜体* —— 去掉星号保留原文（贪婪匹配，多行安全）
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s, flags=re.S)
    s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', s, flags=re.S)
    # 3.1) 移除孤立的 * 字符（Markdown 残留）：所有未被上文成对匹配消耗掉的 *
    #       —— 只保留极少量确实有语义的 "*123" 这种模式（中文场景极少），其余全部直接去掉
    #       中文创作环境下基本不会出现合法 *，用户明确要求 * 和 # 都不要，因此整体安全移除
    s = s.replace('*', '')
    # 3.2) 移除孤立 # 字符：所有行内未在单词中的 #，以及行首 # 全部去掉
    s = s.replace('#', '')
    # 4) 行首 - / * / + 无序列表：替换成两个中文全角空格（缩进保留"列表感"，不丢内容）
    #    —— 也匹配"先有若干全角/半角空格 + 短横"的情形（case5 带缩进的子列表）
    s = re.sub(r'^([ \t\u3000]*)[-*+][ \t]+', lambda m: (m.group(1) or '') + '　　', s, flags=re.M)
    # 5) 行首 > 引用前缀去掉
    s = re.sub(r'^[ \t]*>[ \t]?', '', s, flags=re.M)
    # 6) 行首 "1. " / "2) " / "(1) " 这类编号列表转成全角空格 + 同内容（保留序号但避免半角点列表符号）
    #    - 允许一、二、… 这种中文数字+顿号原样保留（不动）
    s = re.sub(r'^[ \t]*(\d+)\.[ \t]+', lambda m: '　　' + m.group(1) + '、', s, flags=re.M)
    s = re.sub(r'^[ \t]*\((\d+)\)[ \t]+', lambda m: '　　(' + m.group(1) + ') ', s, flags=re.M)
    s = re.sub(r'^[ \t]*(\d+)\)[ \t]+', lambda m: '　　' + m.group(1) + '）', s, flags=re.M)
    # 7) 连续空行最多保留两行
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _downgrade_prompt_for_retry(messages, keep_dim=None):
    """【空内容自动重试】用户反馈经常 EMPTY_OUTPUT，很常见一个根因是：
    首调 messages 过长（铁律+上下文+N条历史）→ 模型 tokens 超限或因 system prompt 过长拒答 →
    直接吐空内容 / 只有 think tokens / 前置空白 / 道歉客套话。
    这里做一个"激进降级精简"：
    - 保留首条 system 但砍到 <= 1800 字（更狠，首条都精简）
    - **末尾追加强制指令**：不要道歉、不要客套、不要 think 标签、直接输出正文
    - 保留最后 6 条对话（retry 场景 last 2 必须带），中间历史全部扔掉
    """
    if not messages:
        return messages
    try:
        msgs = list(messages)
        max_system_chars = 1800
        processed = []
        # 尾部强制指令（重试时才加，首调不加 —— 否则相当于告诉模型你会吐空）
        _force_block = """
————————————————
【重试·强制输出铁律·违反即失败】
1. 绝对禁止道歉/客套/解释：不要说"好的/抱歉/我来帮你/作为AI"这类废话
2. 绝对禁止输出 <think> 标签或任何推理过程
3. 绝对禁止空答：哪怕内容不够完美，也必须输出实质性的创作内容
4. 直接开始输出正文，不要任何前置语，不要总结
————————————————""".strip()
        for i, m in enumerate(msgs):
            if i == 0 and isinstance(m, dict) and m.get('role') == 'system':
                s = m.get('content') or ''
                if len(s) > max_system_chars:
                    s = s[:max_system_chars] + '\n……（中间过长铁律已精简，直接输出有效内容即可）'
                if keep_dim:
                    s += f'\n当前维度：{keep_dim}。'
                s += '\n\n' + _force_block
                processed.append({'role': 'system', 'content': s})
                continue
            processed.append(m)
        # 保留最后 6 条（如果是重试场景，最后两条是 assistant_old + user_retry_hint，必须带）
        if len(processed) > 8:
            processed = [processed[0]] + processed[-6:]
        return processed
    except Exception:
        return messages


# ============================================================================
# AI 智驾：四Tab（设定/正文/去AI/校审）统一接口
# 整合原 AI副驾 + AI总创作 + 章节AI创作 能力，统一入口
# ============================================================================

# 维度定义：用户可见的9个维度子按钮（设定Tab下）
SMART_DIMENSIONS = [
    # mode 说明（维度生成模式）：
    #   suggest = 方向性选择维度：smart_suggest 生成 3-5 个差异化方案卡供作者多选一
    #             （构思=方向源头 / 文风=口味偏好 / 大纲=结构路线多解，必须给选择）
    #   direct  = 执行性展开维度：上游必填依赖已完善时不再出多方案（伪选择：上游已锁方向，
    #             多方案只会诱导 LLM 各换一套体系，选定后与已定构思打架=拼凑感根源），
    #             直接基于锁定上游生成 + 不满意整体重生成（reroll）；
    #             依赖未完善时保留多方案作为探索模式兜底
    {'key': 'concept',            'label': '构思',       'field': 'concept',            'card': 'SAVE_CONCEPT',      'icon': '💡', 'hint': '一句话讲清故事核：主角是谁、要什么、最大的阻碍', 'mode': 'suggest'},
    {'key': 'key_rules',          'label': '设定',       'field': 'key_rules',          'card': 'SAVE_RULE',         'icon': '⚙️', 'hint': '能力体系/修炼体系/科技树，硬规则（构思已定金手指方向时直接生成）', 'mode': 'direct'},
    {'key': 'worldbuilding',      'label': '世界观',     'field': 'worldbuilding',      'card': 'SAVE_WORLDSETTING', 'icon': '🌍', 'hint': '故事发生的世界，独特规则或设定（生成中会提取世界地图架构到“地图”维度）', 'mode': 'direct'},
    {'key': 'plot_design',        'label': '大纲',       'field': 'plot_design',        'card': 'SAVE_OUTLINE_NODE', 'icon': '📋', 'hint': '主线走向，五幕式总纲（卷数与五幕映射已锁定，方案只在每卷目标组织上差异）', 'mode': 'suggest'},
    {'key': 'character_profiles', 'label': '人物',       'field': 'character_profiles', 'card': 'SAVE_CHARACTER',    'icon': '👤', 'hint': '主角和核心配角的动机、性格、关系网（构思已定主角/反派框架时直接生成）', 'mode': 'direct'},
    {'key': 'timeline',           'label': '剧情',       'field': 'timeline',           'card': 'SAVE_PLOT',         'icon': '📖', 'hint': '关键剧情节点的时间顺序（大纲已定每卷目标时直接生成）', 'mode': 'direct'},
    {'key': 'foreshadowing',      'label': '伏笔',       'field': 'foreshadowing',      'card': 'SAVE_FORESHADOW',   'icon': '🔮', 'hint': '长线伏笔的埋设与回收计划（基于大纲/剧情派生，直接生成）', 'mode': 'direct'},
    {'key': 'locations',          'label': '地图',       'field': 'locations',          'card': 'SAVE_LOCATION',     'icon': '🗺️', 'hint': '故事中的地点、势力分布、世界地图架构（基于世界观派生，直接生成）', 'mode': 'direct'},
    {'key': 'style_guide',        'label': '文风',       'field': 'style_guide',        'card': 'APPLY_STYLE',       'icon': '🎨', 'hint': '叙事风格、语言调性、节奏把控', 'mode': 'suggest'},
]

# 通用聊天：不属于任何维度，自由讨论小说/剧情分析，通过触发关键词填入各维度
SMART_GENERAL_KEY = 'general'

_DIM_KEY_TO_SPEC = {d['key']: d for d in SMART_DIMENSIONS}

# ============================================================================
# 维度依赖图（P1 改进）：某维度生成前，建议/要求先完善哪些前置维度
# required:   未完善时阻断生成（强依赖，违反会导致下游维度质量崩溃）
# recommended: 未完善时提示但允许生成（软依赖，影响一致性）
# 设计原则：
#   - concept 是所有维度的源头（一句话讲清故事核）
#   - worldbuilding/key_rules 是剧情/人物的设定基础
#   - plot_design（五幕总纲）是 timeline（分卷剧情）的前置
#   - timeline（分卷剧情）是 foreshadowing（伏笔回收计划）的前置
# ============================================================================
DIMENSION_DEPENDENCIES = {
    'concept':            {'required': [], 'recommended': []},
    'key_rules':          {'required': ['concept'], 'recommended': []},
    'worldbuilding':      {'required': ['concept'], 'recommended': ['key_rules']},
    'plot_design':        {'required': ['concept'], 'recommended': ['worldbuilding', 'key_rules']},
    'timeline':           {'required': ['plot_design'], 'recommended': ['character_profiles', 'worldbuilding']},
    'character_profiles': {'required': ['concept'], 'recommended': ['worldbuilding']},
    'foreshadowing':      {'required': ['plot_design'], 'recommended': ['timeline']},
    'locations':          {'required': ['worldbuilding'], 'recommended': []},
    'style_guide':        {'required': [], 'recommended': ['concept']},
}


def _is_dim_filled(bb, dim_key: str) -> bool:
    """判断指定维度是否已完善（达到可用阈值）"""
    spec = _DIM_KEY_TO_SPEC.get(dim_key)
    if not spec or not bb:
        return False
    val = (getattr(bb, spec['field'], '') or '').strip()
    if not val:
        return False
    # 各维度的"可用阈值"字数下限（上游分节铁律有明确要求，这里做粗筛）
    # 构思/设定/世界观/地图/伏笔/文风/大纲 这些维度不能再 50 字就放行
    dim_floor = {
        'concept': 1200,
        'key_rules': 1500,
        'worldbuilding': 2000,
        'locations': 1200,
        'foreshadowing': 1000,
        'style_guide': 1000,
        'plot_design': 1500,
    }
    floor = dim_floor.get(dim_key)
    if floor:
        return len(val) >= floor
    # timeline 是 JSON 数组：至少 1 卷且每卷有 main_plot（不做字数下限直接用结构校验）
    if dim_key == 'timeline':
        try:
            vols = json.loads(val)
            return isinstance(vols, list) and len(vols) > 0 and \
                   all(isinstance(v, dict) and (v.get('main_plot') or '').strip() for v in vols)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    # character_profiles 可能是 JSON 数组或纯文本：至少有 1 个人物（JSON 需 1 个对象；纯文本需 >= 200 字，因为现在要求每人字段写满30字×至少4字段）
    if dim_key == 'character_profiles':
        if val.startswith('['):
            try:
                chars = json.loads(val)
                return isinstance(chars, list) and len(chars) > 0 and \
                       any(len(str(c.get('background') or c.get('description') or c.get('identity') or '')) >= 50 for c in chars if isinstance(c, dict))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return len(val) >= 200
    # 其他维度：至少 50 字（兜底）
    return len(val) >= 50


def check_dim_readiness(bb, dim_key: str) -> dict:
    """检查指定维度的前置依赖完善度

    返回:
    {
        'ready': bool,
        'missing_required': ['character_profiles'],
        'missing_recommended': ['worldbuilding'],
        'warning': '生成剧情前建议先完善：人物设定'
    }
    """
    deps = DIMENSION_DEPENDENCIES.get(dim_key, {'required': [], 'recommended': []})
    missing_req = [k for k in deps['required'] if not _is_dim_filled(bb, k)]
    missing_rec = [k for k in deps['recommended'] if not _is_dim_filled(bb, k)]

    warning = ''
    if missing_req:
        labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in missing_req)
        warning = f'生成“{_DIM_KEY_TO_SPEC[dim_key]["label"]}”前必须先完善：{labels}（未完善会导致内容质量严重下降）'
    elif missing_rec:
        labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in missing_rec)
        warning = f'建议先完善：{labels}（未完善可能导致内容不一致）'

    return {
        'ready': len(missing_req) == 0,
        'missing_required': missing_req,
        'missing_recommended': missing_rec,
        'warning': warning,
    }



def _build_dim_context(book, bb, dim_key, with_self=True):
    """构建指定维度的上下文：其他已填维度作为参考 + 当前维度已有内容。
    完整注入各维度内容，不截断（避免信息缺失导致设定错乱）。
    """
    target = _DIM_KEY_TO_SPEC.get(dim_key)
    if not target:
        return '', ''
    parts = []
    for d in SMART_DIMENSIONS:
        if d['key'] == dim_key:
            continue
        if bb:
            v = (getattr(bb, d['field'], '') or '').strip()
            if v:
                # 人物维度：character_profiles 存的是 JSON 数组，注入前转成自然语言，避免 AI 模仿 JSON 格式
                if d['key'] == 'character_profiles' and v.startswith('['):
                    v = _character_profiles_to_text(v)
                parts.append(f'【{d["label"]}】\n{v}')
    ctx = '\n\n'.join(parts)
    self_content = ''
    if bb and with_self:
        self_content = (getattr(bb, target['field'], '') or '').strip()
        if self_content:
            # 人物维度自身已有内容也转自然语言
            if dim_key == 'character_profiles' and self_content.startswith('['):
                self_content = _character_profiles_to_text(self_content)
    return ctx, self_content


# 通用聊天：自动根据用户输入定位相关章节原文/维度内容并注入上下文
# 让 AI 不再需要回复"请把资料发我"
# ============================================================================

# 关键词映射 -> 维度 key（含同义词，便于用户自由表达时命中）
_DIM_KEYWORD_MAP = [
    ('concept',            ['构思', '核心构思', '故事核', '核心冲突', '卖点', '一句话', '故事梗概']),
    ('key_rules',          ['设定', '核心规则', '规则', '能力体系', '修炼体系', '科技树', '等级', '境界', '功法', '体系']),
    ('worldbuilding',      ['世界观', '世界设定', '地理', '大陆', '国家', '城邦', '势力', '历史', '世界背景']),
    ('plot_design',        ['大纲', '剧情大纲', '主线', '支线', '五幕', '三幕', '起承转合', '剧情线', '整体大纲']),
    ('timeline',           ['剧情', '时间线', '时间', '年代', '剧情时间', '顺序', '先后', '事件顺序']),
    ('character_profiles', ['人物', '角色', '主角', '配角', '反派', '性格', '外貌', '背景故事', '人物档案', '人物设定', '角色设定']),
    ('foreshadowing',      ['伏笔', '铺垫', '预示', '伏笔回收', '回收伏笔', '埋设', '暗线']),
    ('locations',          ['地图', '地点', '场景', '势力分布', '世界地图', '地理位置', '地名']),
    ('style_guide',        ['文风', '叙事风格', '风格', '调性', '语言风格', '写作风格', '文笔']),
]


def _detect_mentions(user_text, book_id, bb):
    """从用户输入中识别提及的章节（号/标题关键词）和维度。
    返回：{'chapters': [Chapter 对象列表，按命中顺序去重], 'dims': [dim_key 列表，按命中顺序去重]}
    """
    from app import Chapter, parse_chapter_number
    user_text = user_text or ''
    # 1) 章节识别：章号
    hit_chapters = []
    hit_ids = set()
    try:
        all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        def _ch_num(c):
            n = parse_chapter_number(c.title or '')
            return (0, n) if n is not None else (1, c.order_index)
        all_chs_sorted = sorted(all_chs, key=_ch_num)
        # 章号正则（支持第N章/第N回/Chapter N）
        nums_found = []
        suffix = '章节回卷部篇话集幕折更段讲课夜日年季场'
        for m in re.finditer(r'第\s*([0-9零一二三四五六七八九十百千万亿两〇]+)\s*([' + suffix + r'])', user_text):
            n = _cn_to_int(m.group(1))
            if n is not None and n not in nums_found:
                nums_found.append(n)
        for m in re.finditer(r'(?:chapter|ch|episode|ep)\.?\s*(\d+)', user_text, re.IGNORECASE):
            n = int(m.group(1))
            if n not in nums_found:
                nums_found.append(n)
        for n in nums_found:
            for c in all_chs_sorted:
                cn = parse_chapter_number(c.title or '')
                if cn == n:
                    if c.id not in hit_ids:
                        hit_chapters.append(c)
                        hit_ids.add(c.id)
                    break
        # 章节标题关键词（除章号外的其余片段，若匹配某章标题中包含则命中，最多1章）
        # 先去掉已命中章号的子串，避免重复匹配
        remaining = re.sub(r'第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*[' + suffix + r']', '', user_text)
        remaining = re.sub(r'(?:chapter|ch|episode|ep)\.?\s*\d+', '', remaining, flags=re.IGNORECASE)
        # 提取"XXX章"中章号后面的章节名关键词，长度>=2
        name_match = re.search(r'章\s*([\u4e00-\u9fffA-Za-z0-9]{2,})', user_text)
        if name_match:
            kw = name_match.group(1)
            for c in all_chs_sorted:
                if c.id in hit_ids:
                    continue
                t = (c.title or '').strip()
                # 仅命中不是章号部分的文字
                t_without_num = re.sub(r'^第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*章\s*', '', t)
                if kw and (kw in t or kw in t_without_num):
                    hit_chapters.append(c)
                    hit_ids.add(c.id)
                    break
    except Exception:
        pass

    # 2) 维度识别：关键词命中
    dims = []
    dim_keys_seen = set()
    for dim_key, kws in _DIM_KEYWORD_MAP:
        for kw in kws:
            if kw and kw in user_text:
                if dim_key not in dim_keys_seen:
                    dims.append(dim_key)
                    dim_keys_seen.add(dim_key)
                break
    # 如果提到"设定"但没提"核心规则"单独词，可能用户泛指，保留一次
    # （已在关键词中直接映射为 key_rules，保持一致即可）

    return {'chapters': hit_chapters[:5], 'dims': dims[:5]}


def _cn_to_int(s):
    """中文数字转 int（轻量版，覆盖聊天场景常见范围）。"""
    if not s:
        return None
    if re.fullmatch(r'\d+', s):
        return int(s)
    digit_map = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
    total, section, num = 0, 0, 0
    for ch in s:
        v = digit_map.get(ch)
        if v is None:
            return None
        if v >= 10:
            section = (num or 1) * v
            total += section
            num = 0
            if v >= 10000:
                total = section
                section = 0
        else:
            num = v
    return total + num


def _build_auto_context_block(user_text, book_id, bb):
    """根据用户提及自动构建引用块（章节原文 + 维度内容摘要）。
    返回：(block_str, info_dict) — info_dict 供前端回显命中的章节/维度名。
    """
    mentions = _detect_mentions(user_text, book_id, bb)
    if not mentions['chapters'] and not mentions['dims']:
        return '', {'chapters': [], 'dims': []}

    lines = []
    info_chapters = []
    info_dims = []

    for ch in mentions['chapters']:
        title = ch.title or f'第{ch.order_index}章'
        raw = (ch.content or '').strip()
        # 正文过长（>1500字）截断中段保留首尾，关键信息不丢
        if len(raw) > 1500:
            head = raw[:800]
            tail = raw[-700:]
            snippet = head + '\n…（中间已省略，约' + str(max(0, len(raw) - 1500)) + '字）…\n' + tail
        else:
            snippet = raw or '（章节尚无正文）'
        wc = ch.word_count or len(raw)
        lines.append(f'【引用·章节原文】{title}（{wc}字，已自动从章节表载入，无需作者再发）')
        lines.append(snippet)
        info_chapters.append({'id': ch.id, 'title': title})

    for dim_key in mentions['dims']:
        spec = _DIM_KEY_TO_SPEC.get(dim_key)
        if not spec:
            continue
        label = spec['label']
        raw = ''
        if bb:
            raw = (getattr(bb, spec['field'], '') or '').strip()
            if dim_key == 'character_profiles' and raw.startswith('['):
                raw = _character_profiles_to_text(raw)
        if raw:
            snippet = raw if len(raw) <= 2500 else (raw[:1800] + '\n…（中间省略）…\n' + raw[-700:])
            lines.append(f'【引用·维度内容】{label}（已从设定库载入，无需作者再发）')
            lines.append(snippet)
        else:
            lines.append(f'【引用·维度内容】{label}（此维度目前还是空白）')
        info_dims.append({'key': dim_key, 'label': label})

    block = '\n\n'.join(lines)
    return block, {'chapters': info_chapters, 'dims': info_dims}


def _character_profiles_to_text(json_str):
    """把 character_profiles JSON 数组转为自然语言文本，避免 AI 模仿 JSON 格式输出。
    输入：[{"name":"主角名","identity":"...","personality":"..."}, ...]
    输出：
      姓名：主角名
      身份：...
      性格：...
      （空行分隔下一个角色）
    """
    try:
        arr = json.loads(json_str)
        if not isinstance(arr, list):
            return json_str
        blocks = []
        for c in arr:
            if not isinstance(c, dict):
                continue
            lines = []
            field_labels = [
                ('name', '姓名'), ('role', '角色'), ('identity', '身份'),
                ('personality', '性格'), ('motivation', '动机'),
                ('background', '背景'), ('relationships', '关系'),
                ('abilities', '能力'), ('cultivation_talent', '修炼天赋'),
                ('realm', '境界'), ('items', '物品'),
            ]
            for f, label in field_labels:
                val = (c.get(f) or '').strip()
                if val:
                    lines.append(f'{label}：{val}')
            if lines:
                blocks.append('\n'.join(lines))
        return '\n\n'.join(blocks) if blocks else json_str
    except Exception:
        return json_str


@chat_collab_bp.route('/api/ai/smart/dimensions', methods=['GET'])
def smart_dimensions():
    """返回 AI 智驾支持的维度列表（供前端渲染子按钮）。"""
    return jsonify({'dimensions': SMART_DIMENSIONS})


@chat_collab_bp.route('/api/ai/smart/backfill-eventlog', methods=['POST'])
def backfill_eventlog():
    """P1-3: 全文重算事件日志（后台批处理接口）。
       · 同步执行，章节多（>20）时自动每 10 章 yield 一条进度 SSE，避免 Render 网关超时。
       · use_llm: 'auto'（默认，关键章用LLM/普通章正则） / 'always'（全部 LLM，成本高）/ 'never'（全部正则）。
       · start_chapter / end_chapter：可选，只重算某个范围。
       对于 <20 章 且 use_llm!='always' 的情况：直接 JSON 返回总结果；否则 SSE 流式返回进度事件。
    """
    from app import BookBible, Chapter, Character, db

    data = request.json or {}
    book_id = data.get('book_id')
    use_llm = (data.get('use_llm') or 'auto').lower()
    start_ch = data.get('start_chapter')
    end_ch = data.get('end_chapter')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    if use_llm not in ('auto', 'always', 'never'):
        return jsonify({'error': 'use_llm 必须是 auto / always / never'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'BookBible 不存在'}), 404

    chapters_q = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index)
    if start_ch is not None:
        chapters_q = chapters_q.filter(Chapter.order_index >= int(start_ch))
    if end_ch is not None:
        chapters_q = chapters_q.filter(Chapter.order_index <= int(end_ch))
    chapters = chapters_q.all()

    total = len(chapters)
    if total == 0:
        return jsonify({'status': 'ok', 'total': 0, 'added': 0, 'note': '没有章节需要处理'})

    # 决定 LLM 配置（若没配，则 use_llm 降级为 never）
    try:
        from llm_gateway import LLMGateway, get_llm_config
        _base, _api, _model = get_llm_config()
        _gw = LLMGateway(_base, _api, _model) if _base and _api and _model else None
    except Exception:
        _gw = None
        _base = _api = _model = ''
    if use_llm in ('always', 'auto') and not _gw:
        use_llm = 'never'

    known_actors = [c.name for c in Character.query.filter_by(book_id=book_id).all() if c.name]
    known_locations = []
    try:
        _locs = json.loads(bb.locations or '[]')
        if isinstance(_locs, list):
            known_locations = [str(x) for x in _locs if x]
    except Exception:
        pass

    # 少于 20 章且 use_llm != always → 直接 JSON 返回
    use_stream = (total >= 20 or use_llm == 'always')

    def _run_llm_decision_for(idx, ch) -> bool:
        if use_llm == 'always':
            return True
        if use_llm == 'never':
            return False
        # 'auto'：关键章 + 凭证齐全
        from event_log_manager import is_key_chapter
        return bool(is_key_chapter(ch, total_chapters=total)['is_key'])

    def _process_all():
        added_total = 0
        llm_count = 0
        rule_count = 0
        per_ch = []
        for i, ch in enumerate(chapters):
            try:
                from event_log_manager import append_chapter_events
                use_l = _run_llm_decision_for(i, ch)
                if use_l:
                    llm_count += 1
                else:
                    rule_count += 1
                r = append_chapter_events(
                    bb, ch, ch.content or '',
                    known_actors=known_actors,
                    known_locations=known_locations,
                    use_llm=use_l,
                    gw=_gw, base_url=_base, api_key=_api, model=_model,
                )
                added_total += int(r.get('events_added') or 0)
                per_ch.append({'order': ch.order_index, 'title': ch.title,
                               'added': r.get('events_added', 0),
                               'use_llm': use_l})
            except Exception as _e:
                per_ch.append({'order': ch.order_index, 'title': ch.title, 'error': str(_e)})
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {
            'status': 'ok',
            'total': total,
            'added_total': added_total,
            'llm_count': llm_count,
            'rule_count': rule_count,
            'chapters': per_ch,
        }

    if not use_stream:
        summary = _process_all()
        return jsonify(summary)

    # 流式（SSE）：每 10 章推一次进度，最后推 total
    import time as _t
    from event_log_manager import append_chapter_events

    def _sse_line(payload: Dict) -> str:
        return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'

    def gen():
        yield _sse_line({'type': 'start', 'total': total, 'use_llm': use_llm})
        added_total = 0
        llm_count = 0
        rule_count = 0
        try:
            for i, ch in enumerate(chapters, start=1):
                try:
                    use_l = _run_llm_decision_for(i, ch)
                    if use_l:
                        llm_count += 1
                    else:
                        rule_count += 1
                    r = append_chapter_events(
                        bb, ch, ch.content or '',
                        known_actors=known_actors,
                        known_locations=known_locations,
                        use_llm=use_l,
                        gw=_gw, base_url=_base, api_key=_api, model=_model,
                    )
                    added_total += int(r.get('events_added') or 0)
                except Exception as _e:
                    yield _sse_line({'type': 'warn', 'chapter': ch.order_index, 'title': ch.title, 'error': str(_e)})
                if i % 10 == 0 or i == total:
                    yield _sse_line({'type': 'progress',
                               'done': i, 'total': total,
                               'added_so_far': added_total,
                               'llm_so_far': llm_count, 'rule_so_far': rule_count})
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            yield _sse_line({'type': 'done',
                       'total': total,
                       'added_total': added_total,
                       'llm_count': llm_count,
                       'rule_count': rule_count})
        except GeneratorExit:
            pass
        except Exception as _e:
            yield _sse_line({'type': 'error', 'error': str(_e)})
    return Response(gen(), mimetype='text/event-stream; charset=utf-8',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@chat_collab_bp.route('/api/ai/smart/optimization-report', methods=['GET'])
def optimization_report():
    """M4: 返回系统学习到的失败模式与 prompt 优化建议（含使用说明 + 已采纳补丁列表）。"""
    from app import BookBible
    book_id = request.args.get('book_id')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'ready': False, 'failure_count': 0, 'suggestions': [],
                        'how_to_use': {'step1': '先选择一本书', 'step2': '', 'step3': '', 'step4': ''},
                        'applied_patches': [], 'applied_patch_count': 0})
    try:
        from meta_optimizer import get_optimization_report
        return jsonify(get_optimization_report(bb))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@chat_collab_bp.route('/api/ai/smart/adopt-optimization-suggestion', methods=['POST'])
def adopt_optimization_suggestion():
    """M4b: 采纳一条优化建议 → 写入 prompt_patches_json（并忽略该 bucket）。

    body:
      book_id: str
      bucket_key: str         # category::dim_key
      category: str
      dim_key: str (optional)
      patch_text: str         # 用户可能在前端改过 proposed_patch，所以直接传最终文本
    """
    from app import BookBible, db
    data = request.json or {}
    book_id = data.get('book_id')
    bucket_key = (data.get('bucket_key') or '').strip()
    category = (data.get('category') or 'content').strip()
    dim_key = (data.get('dim_key') or '').strip()
    patch_text = (data.get('patch_text') or '').strip()
    if not book_id or not bucket_key or not patch_text:
        return jsonify({'error': '缺少 book_id / bucket_key / patch_text'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'BookBible 不存在'}), 404
    try:
        from meta_optimizer import add_prompt_patch, build_active_patch_text
        patch_obj = add_prompt_patch(bb, category=category, dim_key=dim_key,
                                     patch_text=patch_text, bucket_key=bucket_key)
        db.session.commit()
        return jsonify({
            'ok': True,
            'patch': patch_obj,
            'active_patch_preview': build_active_patch_text(bb)[:500],
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@chat_collab_bp.route('/api/ai/smart/dismiss-optimization-suggestion', methods=['POST'])
def dismiss_optimization_suggestion():
    """M4c: 忽略一条建议 → 写入 ignored_failure_buckets_json，不再出现在建议列表。

    body: { book_id, bucket_key }
    """
    from app import BookBible, db
    data = request.json or {}
    book_id = data.get('book_id')
    bucket_key = (data.get('bucket_key') or '').strip()
    if not book_id or not bucket_key:
        return jsonify({'error': '缺少 book_id / bucket_key'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'BookBible 不存在'}), 404
    try:
        from meta_optimizer import add_ignored_bucket
        add_ignored_bucket(bb, bucket_key)
        db.session.commit()
        return jsonify({'ok': True, 'bucket_key': bucket_key})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@chat_collab_bp.route('/api/ai/smart/preview-impact', methods=['POST'])
def preview_impact():
    """M2: 动作影响预览。当前支持 rename_entity / edit_dim。
    升级：preview 阶段提前跑所有 auto 任务（check_dim_consistency 等），
    将实际冲突结果写入每个 task.result，前端可直接展示红/黄警告。"""
    from app import BookBible
    from smart_planner import SmartPlanner, TaskRunner
    data = request.json or {}
    book_id = data.get('book_id')
    action = data.get('action')
    if not book_id or not action:
        return jsonify({'error': '缺少 book_id 或 action'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    planner = SmartPlanner(book_id, bb)
    if action == 'rename_entity':
        graph = planner.build_plan(
            'rename_entity',
            old_name=data.get('old_name', ''),
            new_name=data.get('new_name', ''),
            entity_type=data.get('entity_type', 'character'))
    elif action == 'edit_dim':
        dim_key = data.get('dim_key', '')
        # 用户在预览时可能已提供想要替换的新内容（未入库版本），透传给 check 任务的 new_text
        new_text = data.get('new_text')
        graph = planner.build_plan('edit_dim', dim_key=dim_key, new_text=new_text)
    else:
        return jsonify({'error': '不支持的 action'}), 400

    # ----- preview 阶段提前执行所有 auto 任务（不写入 DB） -----
    runner = TaskRunner(book_id, preview_mode=True)
    try:
        # 仅执行 auto=True 的任务，不处理 manual（那些是需要作者动手的）
        runner.run_all_auto(graph)
    except Exception as _e:
        # preview 失败不应该中断主流程，记录 note 即可
        import traceback as _tb
        for t in graph.topo_order():
            if t.status == 'pending':
                t.status = 'skipped'
                t.result = t.result or {}
                t.result.setdefault('preview_error', str(_e))

    result_dict = graph.to_dict()

    # 汇总：冲突统计 → summary 补充说明 / warnings 追加
    total_critical = 0
    total_warning = 0
    conflict_dims = []
    for t in result_dict.get('tasks', []):
        r = t.get('result') or {}
        if isinstance(r, dict):
            total_critical += int(r.get('critical') or 0)
            total_warning += int(r.get('warning') or 0)
            if r.get('status') in ('conflict', 'warn') and r.get('target_label'):
                conflict_dims.append(
                    f"{r['target_label']}（{r.get('critical') or 0}严重 / {r.get('warning') or 0}警告）")

    if total_critical or total_warning:
        extra_note = f"扫描到 {total_critical} 处严重冲突 + {total_warning} 处疑似矛盾：" + '；'.join(conflict_dims)
        result_dict['summary'] = (result_dict.get('summary') or '') + '\n⚠️ ' + extra_note
        warnings = list(result_dict.get('warnings') or [])
        if total_critical:
            warnings.insert(0, f'存在 {total_critical} 处严重冲突，建议先解决冲突再应用此改动。')
        warnings.append(extra_note)
        result_dict['warnings'] = warnings
    return jsonify(result_dict)


# ----------------------------------------------------------------------------
# 设定Tab：通用聊天（自由讨论，关键词触发填入维度）
# ----------------------------------------------------------------------------

# 关键词到维度的映射：用户消息中含关键词时，AI 回复可产对应维度的卡片
_GENERAL_KEYWORD_MAP = {
    'concept': ['构思', '故事核', '主线思路', '核心冲突'],
    'key_rules': ['设定', '体系', '规则', '修炼', '能力', '科技树'],
    'worldbuilding': ['世界观', '世界设定', '世界规则'],
    'plot_design': ['大纲', '主线', '剧情走向', '起承转合'],
    'timeline': ['剧情', '时间线', '事件顺序', '剧情节点'],
    'character_profiles': ['人物', '角色', '主角', '配角', '关系'],
    'foreshadowing': ['伏笔', '埋线', '回收'],
    'locations': ['地图', '地点', '势力分布', '地理位置'],
    'style_guide': ['文风', '风格', '语言调性', '叙事'],
}


def _detect_dim_from_text(text):
    """从用户文本中检测涉及的维度关键词，返回 [(dim_key, matched_words)]。"""
    if not text:
        return []
    hits = []
    for dim_key, kws in _GENERAL_KEYWORD_MAP.items():
        matched = [kw for kw in kws if kw in text]
        if matched:
            hits.append((dim_key, matched))
    return hits


@chat_collab_bp.route('/api/ai/smart/general', methods=['POST'])
def smart_general():
    """AI智驾·设定·通用聊天：自由讨论小说/剧情，流式回复，关键词触发产维度卡片。
    已升级：复用 chat_smart 级别的定位铁律 + 章节目录 + 自动上下文注入，
    严禁回复"请把资料发给我"类话术。

    body: { book_id, message, history?, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error / meta(auto_context)
    """
    from app import db, AISession, Book, BookBible, Chapter, parse_chapter_number
    from llm_gateway import LLMGateway, get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    message = (data.get('message') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids') or []
    session_id = data.get('session_id')

    if not book_id or not message:
        return jsonify({'error': '缺少 book_id 或 message'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ===== 【卷数/章数意图·落地前置】通用聊天也可能直接说"改成25卷/每卷60章" =====
    try:
        _auto_sync_params_from_user_message(book, bb, message)
        # 同步后可能新建 bb → 重新取
        bb = BookBible.query.filter_by(book_id=book_id).first()
        # Book ↔ Bible 双表同步（任一侧有值都合并到另一侧，防止后续 _get_total_volumes 取不到）
        from app import _sync_book_meta_to_bible
        if bb is None:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)
        _sync_book_meta_to_bible(book, bb)
        db.session.commit()
        bb = BookBible.query.filter_by(book_id=book_id).first()
    except Exception:
        pass

    # ===== 最近章节 + 下一章号（与 chat_smart 口径一致）=====
    recent_chapters = []
    next_chapter_num = None
    try:
        ch_info = _get_latest_chapter_info(book_id)
        next_chapter_num = ch_info['next_num']
        recent = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        def _recent_key(c):
            n = parse_chapter_number(c.title or '')
            return (0, n) if n is not None else (1, c.order_index)
        recent = sorted(recent, key=_recent_key)[-5:]
        recent_chapters = [{'title': c.title, 'word_count': c.word_count or 0,
                            'order_index': c.order_index} for c in recent]
    except Exception:
        pass

    # ===== 关键词命中（卡片产出用）=====
    detected = _detect_dim_from_text(message)

    # ===== 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）=====
    conception_rules = build_conception_rules(skill_pack_ids, mode='agent')

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting', title='通用聊天')
    session_id = session.id

    # ===== 复用 chat_smart 的 system prompt + TOC + 定位铁律（核心）=====
    toc_block = _build_toc_block(book_id)
    base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)

    # 通用聊天专属追加：构思专属规则 + 关键词命中卡片产出提示 + 增强索要资料禁令（第二保险）
    extra_parts = []
    if conception_rules:
        extra_parts.append(f'\n【构思阶段·平台内置规则+技能包方法论】\n{conception_rules}')
    dim_hint = ''
    if detected:
        dim_labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k, _ in detected)
        dim_hint = f'\n\n【关键词触发】用户讨论涉及维度：{dim_labels}。若你的回复中产出了可落地的设定内容，请用卡片标记输出（每个维度一张）：\n[[CARD:卡片类型|标题|内容]]\n卡片类型对照：SAVE_CONCEPT=构思, SAVE_RULE=设定, SAVE_WORLDSETTING=世界观, SAVE_OUTLINE_NODE=大纲, SAVE_PLOT=剧情, SAVE_CHARACTER=人物, SAVE_FORESHADOW=伏笔, SAVE_LOCATION=地图, APPLY_STYLE=文风。无则不输出卡片。'
        if any(k == 'character_profiles' for k, _ in detected):
            dim_hint += '\n\n【人物卡片内容格式·铁律】绝对禁止 JSON 符号 [ ] { } " : 和英文字段名。卡片内容必须用纯中文，按“姓名：xxx\\n身份：xxx\\n性格：xxx\\n动机：xxx\\n背景：xxx\\n关系：xxx\\n能力：xxx”分行输出，每字段至少30字。'
    # 叠加一条更强的禁令（第二保险，避免模型偶尔无视 base 的铁律）
    extra_parts.append("""
【禁止索要资料·二次强制（如与上面铁律冲突，以本条目为准）】
如果用户的原话里包含以下任何表达，你必须直接按要求产出方案/修改建议/分析，严禁再说要资料：
  "帮我改/修改/调整/修订/润色/优化 + 大纲/设定/人物/世界观/剧情/伏笔/文风/第X章"
  "给我写/生成/出 + 大纲/设定/剧情/人物"
正确做法：
  - 用户说"修改/调整大纲/剧情/人物…"且对应维度为空 → 直接从零给方案（分点/分幕/分卷），不要反问要大纲原文
  - 用户说"修改/调整 第X章" → 直接给修改方案或产出 SAVE_CHAPTER 卡片（系统已自动注入该章原文），不要说"你还没给我第X章内容"
  - 只有当缺少非常具体的修改目标时（如"第5章改一下"又不说改什么），只问"你想侧重改剧情/对白/节奏/人物哪方面？"，不要要资料
  - 任何场景下都禁止出现这些句子或同义改写：请把大纲/设定/人物/章节资料发给我 / 你需要先提供 / 先把XXX发我 / 我需要你提供 / 期待您的大纲
""".strip())
    extra_parts.append(dim_hint)
    extra_parts.append(PLAIN_TEXT_LAYOUT_RULES)
    sys_prompt = base_system + '\n\n' + '\n\n'.join(p for p in extra_parts if p)

    # ===== 自动上下文注入：章节原文/维度内容引用块前置 =====
    auto_ctx_block, auto_ctx_info = _build_auto_context_block(message, book_id, bb)
    enriched_user_message = message
    if auto_ctx_block:
        enriched_user_message = (
            '（以下为系统根据作者输入自动从当前书库载入的引用资料，用于辅助回答；作者原话为最后的"【作者原话】"段。\n'
            '回答时直接基于这些资料讨论/修改，严禁再让作者"把资料发给我"；若引用中的某维度为空，直接说明为空并从零给方案。）\n\n'
            f'{auto_ctx_block}\n\n'
            '——————————————————\n'
            '【作者原话】\n'
            f'{message}'
        )

    # ===== 组装 LLM messages（含会话滑窗历史）=====
    history = load_session_messages(session)
    messages = build_context_messages(sys_prompt, history, enriched_user_message)

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        full = []
        try:
            # SSE 首帧 meta：命中章节/维度（前端提示"已定位并注入"）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield sse({'type': 'meta', 'kind': 'auto_context', 'info': auto_ctx_info})

            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=4096):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                full.append(chunk)
                yield sse({'type': 'delta', 'content': chunk})
            content = ''.join(full).strip()
            # 平台级纯文本清理（先清再解析卡片，避免卡片内残留 Markdown）
            content = _clean_text_to_plain(content)
            # 解析卡片标记
            cards = _parse_card_markers(content)
            clean_content = _clean_text_to_plain(_strip_card_markers(content))
            # 人物卡片兜底：若内容仍为 JSON 数组，转成自然语言
            for card in cards:
                if card.get('type') == 'SAVE_CHARACTER':
                    c = (card.get('content') or '').strip()
                    if c.startswith('[') or c.startswith('{'):
                        card['content'] = _character_profiles_to_text(c) if c.startswith('[') else _character_profiles_to_text('[' + c + ']')
                else:
                    # 统一纯文本清理卡片内容/标题
                    card['content'] = _clean_text_to_plain(card.get('content', ''))
                    if card.get('title'):
                        card['title'] = _clean_text_to_plain(card['title'])
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            # 历史里保存作者原话（不保存注入引用块，避免多轮重复上下文）
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_content,
                            'cards': [{**c, 'status': 'pending'} for c in cards] if cards else None})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _parse_card_markers(text):
    """解析 [[CARD:TYPE|title|content]] 标记为卡片列表。"""
    cards = []
    if not text:
        return cards
    pattern = re.compile(r'\[\[CARD:([A-Z_]+)\|([^\|]*)\|([^\]]+)\]\]', re.S)
    for m in pattern.finditer(text):
        ctype, title, content = m.group(1), m.group(2).strip(), m.group(3).strip()
        cards.append({
            'id': str(uuid.uuid4())[:8],
            'type': ctype,
            'title': title or _CARD_TARGET.get(ctype, ctype),
            'content': content,
            'target': _CARD_TARGET.get(ctype, ctype),
        })
    return cards


def _strip_card_markers(text):
    """移除文本中的卡片标记。"""
    if not text:
        return text
    return re.sub(r'\[\[CARD:[A-Z_]+\|[^\|]*\|[^\]]+\]\]', '', text).strip()


# ----------------------------------------------------------------------------
# 设定Tab：人机协作流（提需求 → 多选意见 → 选中 → 生成 → 可改重生成 → 填入维度）
# ----------------------------------------------------------------------------

@chat_collab_bp.route('/api/ai/smart/suggest', methods=['POST'])
def smart_suggest():
    """AI智驾·设定：用户提需求 → AI给 3-5 个多选意见。

    body: { book_id, dimension, requirement, skill_pack_ids? }
    返回: { suggestions: [{id, title, preview}], dimension, dimension_label, requirement }
    """
    from app import Book, BookBible
    from llm_gateway import get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    dim_key = data.get('dimension')
    requirement = (data.get('requirement') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids') or []
    # 用户在智驾窗口直接贴了自己的完整内容（>300字且维度空时前端会传）：
    # 把"用户方案"放在 AI 方案最前面，供作者选择"按我的直接落地"。
    user_paste = (data.get('user_paste') or '').strip()

    if not book_id or dim_key not in _DIM_KEY_TO_SPEC:
        return jsonify({'error': '缺少 book_id 或 dimension 无效'}), 400

    spec = _DIM_KEY_TO_SPEC[dim_key]
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404

    # ===== 【卷数/章数意图·落地前置】用户在需求框里说"改成25卷/每卷60章"也生效 =====
    # （用户经常在"描述你对大纲的需求"输入框里直接说『给我出一个25卷玄幻』）
    params_sync_notes = None
    try:
        params_sync_notes = _auto_sync_params_from_user_message(book, None, requirement or '')
    except Exception:
        params_sync_notes = None
    # 同步后重新取 bb（可能刚刚新增）
    bb = BookBible.query.filter_by(book_id=book_id).first()
    # 确保作品基本信息页（Book）的卷数同步到 Bible：
    # 若用户在 Book 侧改为 25 卷但 Bible 仍是默认 10，必须同步，否则 AI 读到的仍是 10 卷
    try:
        from app import _sync_book_meta_to_bible
        if bb is None:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)
        _sync_book_meta_to_bible(book, bb)
        db.session.commit()
        bb = BookBible.query.filter_by(book_id=book_id).first()
    except Exception:
        pass
    ctx, self_content = _build_dim_context(book, bb, dim_key)

    # ===== 【direct 模式·执行性展开维度】上游必填依赖已完善时不再出多方案 =====
    # 上游（构思的金手指/世界观卖点/主角反派框架、大纲的每卷目标）已把方向锁死，
    # 此时多方案是伪选择：LLM 会在不同方案里各换一套体系方向，选定后与已定上游打架
    # = 拼凑感根源之一。→ 直接返回固定卡片（不调 LLM，零成本零延迟），
    # 前端点卡片走 smart_generate 直生；不满意整体重生成（reroll，换角度不换方向）。
    # 依赖未完善时保留多方案作为探索模式兜底；用户贴了自己的完整内容时走原流程。
    if spec.get('mode') == 'direct' and not user_paste:
        _dep_req = DIMENSION_DEPENDENCIES.get(dim_key, {}).get('required', [])
        _direct_ready = True
        try:
            _direct_ready = check_dim_readiness(bb, dim_key).get('ready', True)
        except Exception:
            _direct_ready = True  # 就绪检查异常不阻断，宁可直生也不让作者卡住
        if _direct_ready:
            _dep_labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in _dep_req if k in _DIM_KEY_TO_SPEC) or '上游设定'
            _direct_hints = {
                'key_rules': '力量体系/等级阶梯/经济数值严格按构思第六节金手指方向展开，禁止另起体系',
                'worldbuilding': '地理/势力/历史严格按构思第九节世界观卖点钩子展开，禁止另起世界观',
                'character_profiles': '主角严格按构思第七节魅力公式、反派按第八节框架展开，禁止换人设方向',
                'timeline': '各卷剧情严格按大纲每卷目标/冲突/卷尾钩子展开，禁止偏离五幕框架',
                'foreshadowing': '伏笔埋设/回收按大纲与剧情节点派生，禁止凭空新开主线级伏笔',
                'locations': '地点严格按世界观地理分块与势力分布派生，禁止另起地名体系',
            }
            _direct_meta = {'direct_mode': True}
            if params_sync_notes:
                _direct_meta['params_sync'] = params_sync_notes
            return jsonify({
                'suggestions': [{
                    'id': 'sug_1',
                    'title': f'基于已定{_dep_labels}直接生成',
                    'preview': (f'本维度为执行性展开，方向已由{_dep_labels}锁定：'
                                + _direct_hints.get(dim_key, '基于已定上游方向直接展开')
                                + '。生成后不满意可整体重新生成（换展开角度，方向不变）；需调整局部请在生成后对内容提修改意见。'),
                }],
                'dimension': dim_key,
                'dimension_label': spec['label'],
                'requirement': requirement,
                'meta': _direct_meta,
            })

    # 注入核心创作参数铁律（卷数/题材/风格），方案简介里禁止再出现"十卷/五卷/5-8卷"这种默认值
    # 【用户截图的根源】以前用的 _build_core_params_block 太弱，AI仍然按网文常识写"十卷"
    # 现在强制用 _core_params_iron_block（同 chat_smart 那条铁律），且对 preview 简介做专门约束
    core_params = ''
    # tv_for_suggest 不再写死初始 10，避免 DB 未设定时被"默认十卷"污染。
    # 仅在用户 requirement / bb 字段 / book 字段 里真正解析到 N 卷，才注入铁律。
    tv_for_suggest = 0
    try:
        from app import _get_total_volumes
        tv_for_suggest = _get_total_volumes(bb, book) or 0
    except Exception:
        tv_for_suggest = 0
    # 二次兜底：如果 requirement 用户需求文本里明确出现了「N卷」描述，
    # 哪怕 DB 里还没写（_auto_sync_params_from_user_message 还没 commit）也按 N 来提要求，
    # 这样生成的卡片预览就跟用户说的一致，不会出现"用户说 18 卷，卡片写十卷"这种问题。
    try:
        if (tv_for_suggest == 0 or tv_for_suggest is None) and requirement:
            m = _RE_TV.search(requirement or '')
            if m:
                cand = int(m.group(1))
                if 1 <= cand <= 500:
                    tv_for_suggest = cand
    except Exception:
        pass
    try:
        # 优先铁律版；铁律版取不到再降级旧版兜底
        core_params = _core_params_iron_block(bb, book)
    except Exception:
        core_params = ''
    if not core_params:
        try:
            from app import _build_core_params_block
            core_params = _build_core_params_block(bb, book) or ''
        except Exception:
            pass

    # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）
    skill_note = build_conception_rules(skill_pack_ids, mode='single')

    # 方案级铁律（所有维度通用，直接在 suggestions[].preview 产出层拦截"十卷"）
    suggest_iron_rule = ''
    if tv_for_suggest and tv_for_suggest >= 1:
        suggest_iron_rule = f"""
【方案卡片预览·卷数铁律（直接作用于你输出的 JSON suggestions[].preview）】
- 本书已设定总卷数为 {tv_for_suggest} 卷，你输出的**每一个**方案简介 preview 中，若需要提到全书分卷规模，
  必须直接写成“{tv_for_suggest} 卷”（阿拉伯数字）或更具体的“{tv_for_suggest}卷×50章”。
- 严禁在 preview 中出现“十卷 / 五卷 / 八卷 / 六卷 / 十二卷 / 十余卷 / 5-8 卷 / 通常 5-8 卷 / 5到8卷”这类默认值或中文数字描述，
  哪怕你觉得更通顺也不行 —— 用户设定多少就必须写多少。
- 严禁把卷数偷偷压缩成"5幕对应5卷"来写简介，必须真实体现 {tv_for_suggest} 卷的体量。
- 如果你违反以上任何一条，你输出的方案卡片就不合格。"""

    # 大纲/剧情/构思这 3 个维度专属：要求 preview 主动把卷数写进简介（用户截图的方案卡片就是"十卷按五幕完成"这种）
    dim_needs_volume_in_preview = dim_key in ('plot_design', 'timeline', 'concept', 'dynamic_volumes')
    preview_volume_req = ''
    if dim_needs_volume_in_preview and tv_for_suggest and tv_for_suggest >= 1:
        preview_volume_req = f'\n- 本任务为“{spec.get("label", dim_key)}”维度，你输出的每条 preview 简介**必须**显式出现“{tv_for_suggest}卷”的字样，用来直接告诉作者这方案是按他设定的 {tv_for_suggest} 卷规划的；不写"十卷""五卷"等其他数字。'

    # 大纲维度：五幕模型说明（给出 tv 卷下的精确映射示例，禁止 AI 把多幕压缩到第25卷）
    outline_extra = ''
    if dim_key == 'plot_design':
        try:
            from app import _get_total_volumes, _cultivation_dimension_hint
            tv = _get_total_volumes(bb, book) or 0
            if tv >= 2:
                # 作者已指定卷数 → 给出精确的五幕卷号映射
                act1_end = max(1, tv*5//100)
                act2_end = tv*25//100
                act3_end = tv*50//100
                act4_end = tv*75//100
                five_act_example = f'立身第1~{act1_end}卷、立足第{act1_end+1}~{act2_end}卷、立势第{act2_end+1}~{act3_end}卷、立威第{act3_end+1}~{act4_end}卷、立命第{act4_end+1}~{tv}卷'
                outline_extra = f"""\n\n【大纲维度专属要求】请生成“五幕式总纲”方向的差异化方案。
全书严格 {tv} 卷，每卷约50章12万字。
五幕模型按{tv}卷比例的精确卷号映射如下（必须严格遵守，不得擅自改动）：
- 立身(前5%)：第1~{act1_end}卷
- 立足(5%-25%)：第{act1_end+1}~{act2_end}卷
- 立势(25%-50%)：第{act2_end+1}~{act3_end}卷
- 立威(50%-75%)：第{act3_end+1}~{act4_end}卷
- 立命(75%-100%)：第{act4_end+1}~{tv}卷

【必须照抄的示例】当提到五幕对应的卷号范围时，请严格按以下格式书写：
"{five_act_example}"
严禁出现"第2至{tv}卷""第7至{tv}卷""第X至{tv}卷"这种把多幕压缩到同一卷的错误写法；每一幕必须对应独立的卷号区间。
只输出多方案供作者选择，每个方案 preview 简介必须说明对应的五幕怎么分配到 {tv} 卷。"""
            else:
                # 作者未指定卷数 → 先提出"建议按 N 卷规划"的候选，不写死 10
                outline_extra = """\n\n【大纲维度专属要求】请生成“五幕式总纲”方向的差异化方案。
作者尚未指定总卷数，因此你每条方案**必须**在 preview 简介里先明确写出一个你建议的分卷规模（例如"建议按 20 卷规划，五幕对应..."），禁止擅自默认"十卷/五卷/十二卷/5-8卷"等固定值；
为每条方案给出对应卷数下的五幕卷号映射（立身/立足/立势/立威/立命，五幕从 1 卷 连续递增 到 方案建议卷数），每幕必须对应独立的卷号区间，不得把多幕压缩成同一区间。"""
            try:
                outline_extra += _cultivation_dimension_hint('plot_design', book, bb)
            except Exception:
                pass
        except Exception:
            pass

    # 防遗忘检查报告回注：让方案生成也感知已诊断出的一致性违规/待回收伏笔/叙事债务
    af_alerts_suggest = ''
    try:
        from app import _collect_anti_forget_alerts
        _af = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
        if _af:
            af_alerts_suggest = f'\n\n【防遗忘检查诊断】（最近检查发现的问题，生成方案时必须主动规避/修正）\n{_af}'
    except Exception:
        pass

    # 预拼接块（Python 3.11 禁止 f-string 表达式内含反斜杠，故先算好再引用）
    _self_content_block = ("【当前维度已有内容（可在其基础上补充完善）】\n" + self_content) if self_content else ""
    _skill_note_block = ("【技能包指引】\n" + skill_note) if skill_note else ""

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》的“{spec['label']}”维度生成 3-5 个差异化的创意方案供作者选择。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

{core_params}

【已有设定参考】
{ctx or "（暂无）"}

{_self_content_block}

【作者需求】
{requirement or f"请帮我生成{spec['label']}的设定"}
{outline_extra}{af_alerts_suggest}{suggest_iron_rule}{preview_volume_req}

{_skill_note_block}

【重要·方案卡格式】请生成 3-5 个不同切入角度的方案。严格按以下 JSON 格式输出（不要任何其他内容、不要 Markdown 代码块，不要解释性文字，不要思考过程，不要规则复述，不要英语）：
{{
  "suggestions": [
    {{"title": "方案标题（10字内，中文，说明该方案核心卖点）", "preview": "方案简介（120-220字，全中文，直接对读者讲清：故事核/核心设定差异/爽点钩子，绝不提本prompt里的任何规则/格式/自检要求）"}}
  ]
}}

【P0 禁令·违者作废】
1. 严禁在 title/preview 里出现任何英语（如 key requirements / theme / each preview must / I'm looking at 等）、严禁中英文混杂；
2. 严禁把本 prompt 里的规则、自检要求、格式说明、"30卷"字样的约束语句当方案内容复述；
3. 严禁 preview 用短于 80 字的占位句子凑数（如"方案二：方案2"），每一条 preview 都必须是完整的中文创意简介；
4. 每条方案必须是"差异化创意"——不能是5条把同一个创意换个同义词重写，要有题材/主角身份/金手指形态/切入视角/核心冲突形态上的明确差异。

【最终自检（输出前必过）】
1. 若本任务属于大纲/剧情/构思维度，每条 preview 必须显式包含"{tv_for_suggest if (tv_for_suggest and tv_for_suggest>=1) else '__'}卷"字样，不得写"十卷""五卷"等默认数字；
2. 所有 preview 检查一遍：有没有英语？有没有复述本 prompt 里的规则/自检/格式说明？字数够 120-220？中文通顺？
3. JSON 合法：无多余逗号，suggestions 长度 3-5，数组元素只含 title 与 preview。"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请生成{spec["label"]}的多选方案'}]

    from app import _call_llm
    content, err = _call_llm(messages, max_tokens=2000, temperature=0.85, task_type='creation')
    if err:
        return jsonify({'error': f'生成方案失败：{err}'}), 500

    suggestions = []
    _raw = content or ''
    try:
        m = re.search(r'\{[\s\S]*\}', _raw)
        if m:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                # 支持 suggestions / result / data 等多种外层字段
                for _k in ('suggestions', 'result', 'data', 'items', 'list'):
                    v = parsed.get(_k)
                    if isinstance(v, list):
                        suggestions = v
                        break
    except Exception:
        pass

    # ===== 【P0兜底·用户截图根因】：模型把prompt规则原文/英语/占位句子当方案吐了，必须全打掉 =====
    _BAD_FINGERPRINTS = [
        # 用户截图里实锤出现过的指纹
        'each preview must', 'key requirements', "i'm looking at",
        '方案一：方案1', '方案二：方案2', '方案三：方案3', '方案四：方案4', '方案五：方案5',
        # 规则复述类
        'json格式', '输出格式', '自检', '默认数字', 'suggestions', 'preview must',
        # 中英混类（连续2个以上英语单词夹在中文里）
    ]
    def _is_garbage_preview(txt: str) -> bool:
        if not txt: return True
        low = (txt or '').lower()
        for fp in _BAD_FINGERPRINTS:
            if fp in low: return True
        # 英语单词（字母+空格+字母连续出现≥10字符且非纯URL）=垃圾
        if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', txt): return True
        # 纯占位标题+正文极短=垃圾（如标题"方案1"正文才20字）
        if len(txt.strip()) < 60: return True
        return False

    def _normalize_suggestions(raw_sugs):
        out = []
        for s in (raw_sugs or []):
            if isinstance(s, str):
                out.append({'title': '', 'preview': s.strip()})
            elif isinstance(s, dict):
                title = str(s.get('title') or s.get('name') or s.get('方案名') or '').strip()
                preview = str(s.get('preview') or s.get('content') or s.get('简介') or s.get('description') or s.get('desc') or '').strip()
                if preview:
                    out.append({'title': title, 'preview': preview})
                elif title and len(title) >= 80:
                    # 模型把内容塞进title了，迁移过来
                    out.append({'title': '', 'preview': title})
        return out

    suggestions = _normalize_suggestions(suggestions)
    suggestions = [s for s in suggestions if not _is_garbage_preview(s.get('preview', ''))]

    # 二级兜底：从整段原始文本按"方案N："分段取3-5段（中文段为主，过滤英文/规则段）
    if not suggestions:
        cn_splits = re.split(r'(?:^|\n)\s*(?:方案\s*[一二三四五六1-5][：:\s\.、])', _raw)
        cleaned = []
        for chunk in cn_splits[1:6]:
            chunk = chunk.strip()
            if not chunk: continue
            # 取chunk中第一段落（去掉```json 与 code fence）
            chunk = re.sub(r'```[\s\S]*?```', '', chunk)
            # 去明显是prompt说明/英文的句子
            lines = [l.strip() for l in chunk.split('\n') if l.strip()]
            kept_lines = []
            for l in lines:
                low = l.lower()
                if any(fp in low for fp in _BAD_FINGERPRINTS):
                    continue
                if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', l):
                    continue
                kept_lines.append(l)
            merged = ' '.join(kept_lines).strip()
            if len(merged) >= 80:
                cleaned.append({'title': f'方案{len(cleaned)+1}', 'preview': merged})
        suggestions = cleaned

    # 三级兜底：原始文本无JSON、也没"方案N："分段 → 按中文句号/问号断成连续长句，每 3-5 个长句合并为 1 条 preview
    if not suggestions:
        _clean_text = re.sub(r'```[\s\S]*?```', '', _raw)
        _clean_text = re.sub(r'<[^>]+>', '', _clean_text)
        # 去掉 prompt 类句子（带引号 Each preview / Key requirements / JSON合法 等）
        _lines = [l.strip() for l in _clean_text.split('\n') if l.strip()]
        good = []
        for l in _lines:
            low = l.lower()
            if any(fp in low for fp in _BAD_FINGERPRINTS):
                continue
            if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', l):
                continue
            good.append(l)
        merged = ''.join(good)
        # 按中文句号/问号/叹号断句
        sents = re.split(r'(?<=[。！？!?；;])', merged)
        sents = [s.strip() for s in sents if len(s.strip()) >= 10]
        bucket = []
        cur_len = 0
        cur_parts = []
        for s in sents:
            cur_parts.append(s)
            cur_len += len(s)
            if cur_len >= 140 and len(cur_parts) >= 2:
                bucket.append(''.join(cur_parts))
                cur_len = 0
                cur_parts = []
                if len(bucket) >= 5: break
        if cur_parts and len(bucket) < 5:
            bucket.append(''.join(cur_parts))
        suggestions = [{'title': f'方案{i+1}', 'preview': p} for i, p in enumerate(bucket) if len(p) >= 80]

    # 四级兜底：按行硬截前5段非空非英文
    if not suggestions:
        lines = [l.strip() for l in (_raw or '').split('\n') if l.strip() and not l.strip().startswith('```')]
        for i, line in enumerate(lines[:5]):
            # 去掉前导序号
            clean = re.sub(r'^[\d一二三四五1-5\.、\)\s]+', '', line)
            # 英文/规则指纹直接跳过
            low = clean.lower()
            if any(fp in low for fp in _BAD_FINGERPRINTS) or re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', clean):
                continue
            if clean and len(clean) >= 60:
                suggestions.append({'title': f'方案{i + 1}', 'preview': clean[:260]})

    if not suggestions:
        # 只有用户方案时，允许 suggestions 仅有 1 条（用户自己的），不强制要求 3-5 条
        if not user_paste:
            return jsonify({'error': 'AI 未返回有效方案，请重试或调整需求'}), 500

    # === 后端兜底：只纠正明显作为“全书总卷数”的违规描述，绝不碰任何卷号/卷号区间 ===
    #     惨痛教训：任何试图全局替换"第X卷"或"X-Y卷"的规则都会误伤，导致"第13-18卷"→"第125卷"。
    #     现在策略极度保守：
    #       1) 先把所有卷号（含第X卷、第X-Y卷、卷X、第X卷后紧随1-8字中文）完整 stash 占位；
    #       2) 只对 stash 之外的独立"总卷数"短语做替换；
    #       3) 还原 stash，卷号原封不动返回。
    def _enforce_volume_in_preview(preview: str, tv: int) -> str:
        if not preview or not tv or tv < 1:
            return preview
        tvs = f'{tv}卷'

        volume_id_holders = []

        def _stash(m):
            volume_id_holders.append(m.group(0))
            return f'\x00VID{len(volume_id_holders)-1}\x00'

        # 0) 卷号保护：第X-Y卷、第X卷、卷X 以及后面紧跟的少量中文（立身/立足/核心目标等）一起 stash
        cn_digit = r'[一二两三四五六七八九十百零]'
        suffix_follow = r'(?:\s*[\u4e00-\u9fff]{0,8})?'
        preview = re.sub(rf'第\s*\d{{1,4}}\s*[-~～到至]\s*\d{{1,4}}\s*卷{suffix_follow}', _stash, preview)
        preview = re.sub(rf'第\s*\d{{1,4}}\s*卷{suffix_follow}', _stash, preview)
        preview = re.sub(rf'第\s*{cn_digit}{{1,6}}\s*卷{suffix_follow}', _stash, preview)
        preview = re.sub(rf'(?<!\d)卷\s*\d{{1,4}}(?!\s*卷){suffix_follow}', _stash, preview)

        # 1) 替换中文数字总卷数（如"十卷、五卷、十二卷"），要求：
        #    - 前面是"全书/按/共/约/规划/规模/体量/为/是/有"等总卷数上下文，或句首/标点/空格
        #    - 前面不能是"第/之/其/卷/VID占位"
        cn_total = r'(?:[一二两三四五六七八九十百零]{1,5}|十余|十几|数十|十二|十五|二十|三十|五十|一百)'
        total_ctx = r'(?:全书|按|共|约|大概|规划|规模|体量|设定|写|分|划分|安排|设计|为|是|有|写成|出|设定为)'

        def _replace_total_cn(m):
            # m.group(1) 是前面的总卷数上下文/标点/句首，必须原样保留
            prefix = m.group(1)
            return f'{prefix}{tvs}'
        # 中文数字总卷数：前面是总卷数上下文、句首、或标点/空格；后面不能是卷号修饰
        preview = re.sub(rf'((?:{total_ctx}\s*|^|[，。；！？、\s])){cn_total}\s*卷(?!\s*(?:末|首|上|中|下|内|外|前|后|间|之|分|名|号|标|页|段|节|章))',
                         _replace_total_cn, preview)
        # 兜底：句首直接出现的"十卷按/十卷分别/十卷串联/十二卷..."
        preview = re.sub(rf'(?<![第之其卷\d\x00]){cn_total}\s*卷(?=\s*(?:按|分别|串联|完成|写尽|涵盖|覆盖|铺陈|讲|写|推进|安排|规划|走|写完|撑起|构筑|呈现|讲完|打通|飞升|史诗|规模|体量|全书))',
                         tvs, preview)

        # 2) 替换阿拉伯数字总卷数（如"全书12卷""按30卷"），要求前面是明确总卷数上下文
        #    注意：tv 本身不替换；独立句首/标点后的数字卷也要替换
        def _replace_total_num(m):
            prefix = m.group(1)
            try:
                n = int(m.group(2))
            except Exception:
                return m.group(0)
            return f'{prefix}{tvs}' if n != tv else m.group(0)
        preview = re.sub(rf'((?:{total_ctx}\s*|^|[，。；！？、\s]))(?!{tv}\b)(\d{{1,4}})\s*卷(?!\s*(?:末|首|上|中|下|章|节|段|页|号|名))',
                         _replace_total_num, preview)

        # 3) 还原所有卷号
        def _unstash(m):
            try:
                return volume_id_holders[int(m.group(1))]
            except Exception:
                return m.group(0)
        preview = re.sub(r'\x00VID(\d+)\x00', _unstash, preview)

        # 4) 大纲/剧情/构思维度：若 preview 里还没出现 tv卷，只在句首安全补一次
        if dim_needs_volume_in_preview and tvs not in preview and f'{tv} 卷' not in preview:
            def _prefix_prepend(m):
                prefix_word = m.group(1) or ''
                return f'全书{tvs}，' if not prefix_word else f'{prefix_word}{tvs}，'
            preview = re.sub(r'^(以|全书|故事|小说|本书|该作|本作)?', _prefix_prepend, preview)

        return preview

    for i, s in enumerate(suggestions):
        s.setdefault('title', f'方案{i + 1}')
        s.setdefault('preview', '')
        if tv_for_suggest and tv_for_suggest >= 1:
            s['preview'] = _enforce_volume_in_preview(s.get('preview', '') or '', tv_for_suggest)
        s['id'] = f'sug_{i + 1}'

    if not suggestions:
        # 只有用户方案时，允许 suggestions 仅有 1 条（用户自己的），不强制要求 3-5 条
        if not user_paste:
            return jsonify({'error': 'AI 未返回有效方案，请重试或调整需求'}), 500

    # 把用户贴的完整内容作为"我的方案"放在 AI 方案最前面
    if user_paste:
        _title = re.sub(r'\s+', ' ', user_paste[:30]).strip()
        if len(_title) > 18:
            _title = _title[:18] + '…'
        _preview = user_paste[:160] + ('…' if len(user_paste) > 160 else '')
        suggestions.insert(0, {
            'id': 'user_0',
            'title': f'我的方案：{_title or "用户自定义"}',
            'preview': _preview,
            '_from_user': True,
        })
    # 重新编号 id（用户方案保持 user_0，AI 方案从 sug_1 开始）
    _ai_idx = 0
    for s in suggestions:
        if s.get('_from_user'):
            continue
        _ai_idx += 1
        s['id'] = f'sug_{_ai_idx}'

    # 返回值里带上参数同步备注（若用户在需求里说"改成25卷"等），前端可在智驾里显示小字提示
    meta = {}
    if params_sync_notes:
        meta['params_sync'] = params_sync_notes
    if tv_for_suggest and tv_for_suggest >= 1:
        meta['total_volumes'] = tv_for_suggest

    return jsonify({
        'suggestions': suggestions,
        'dimension': dim_key,
        'dimension_label': spec['label'],
        'requirement': requirement,
        'meta': meta,
    })


@chat_collab_bp.route('/api/ai/smart/generate', methods=['POST'])
def smart_generate():
    """AI智驾·设定：基于选中意见生成最终内容（流式，产卡片）。

    body: { book_id, dimension, suggestion, requirement?, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error
    """
    from app import db, AISession, Book, BookBible
    from llm_gateway import LLMGateway, get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    dim_key = data.get('dimension')
    suggestion = (data.get('suggestion') or '').strip()
    requirement = (data.get('requirement') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids') or []
    session_id = data.get('session_id')
    # 用户选中了"我的方案"并要直接落地：跳过 LLM，原样 delta 输出 suggestion 并产卡片，保证内容不被 AI 改写
    from_user_paste = bool(data.get('from_user_paste'))
    # 【direct 模式】整体重新生成（reroll）：换展开角度，方向仍锁定上游不变
    reroll = bool(data.get('reroll'))

    # reroll 允许 suggestion 为空：direct 模式的方向来自 DB 已定上游，不依赖方案卡片文本
    if not book_id or dim_key not in _DIM_KEY_TO_SPEC or (not suggestion and not reroll):
        return jsonify({'error': '参数无效：需要 book_id/dimension/suggestion'}), 400

    spec = _DIM_KEY_TO_SPEC[dim_key]
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    ctx, self_content = _build_dim_context(book, bb, dim_key)

    # =====【卷数/章数·真正写入 DB·截图的矛盾根源修复】=====
    # 场景：用户在"构思"维度，选了一张预览写着"全书按25卷设计"的方案卡片
    # → 点"按此方案直接生成" → smart_generate 被调用，suggestion = 那 25 卷方案全文
    # → 但之前没有把"25卷"真的写入 DB（bb.total_volumes / book.total_volumes）
    # → _get_total_volumes 取不到就回退默认 10
    # → _core_params_iron_block 后面告诉 LLM"全书10卷"
    # → LLM 落地卡片输出"全书定为十卷"，跟用户选的25卷方案互相矛盾
    #
    # 修复：在注入铁律 / 调用 LLM 之前，先把 suggestion（即用户选中的方案全文，
    # 含 title/preview/full_content 拼合）和 requirement 用户意见，一起塞进
    # _auto_sync_params_from_user_message → 正则解析 → 真正写入 DB，
    # 这样后面 _core_params_iron_block 读到的就是正确卷数，不再 10 卷。
    try:
        _auto_sync_params_from_user_message(book, bb, (suggestion or '') + '\n' + (requirement or ''))
        # 同步完可能刚刚新建 bb，必须再取一次
        bb = BookBible.query.filter_by(book_id=book_id).first()
        # 顺带把 Book 表侧的 meta 也同步到 Bible，防止一侧改了另一侧没改
        from app import _sync_book_meta_to_bible
        if bb is None:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)
        _sync_book_meta_to_bible(book, bb)
        db.session.commit()
        bb = BookBible.query.filter_by(book_id=book_id).first()
    except Exception:
        pass

    # 注入核心创作参数铁律（卷数/题材/风格），让大纲/剧情等维度严格按全书卷数规划
    core_params = _core_params_iron_block(bb, book)

    # ===== 【direct 模式·上游方向锁定】执行性展开维度（设定/世界观/人物/剧情/伏笔/地图）=====
    # 上游必填依赖已完善时：注入上游维度全文 + 方向锁定铁律，禁止另起方向与已定上游冲突。
    # 这是"东拼西凑"的对症修复：下游生成不再漂移，构思的 direction 层权威落到生成时。
    # reroll=True（整体重新生成）：换一个展开角度，但方向仍锁定不变。
    direct_lock_note = ''
    if spec.get('mode') == 'direct':
        try:
            _dep_req = DIMENSION_DEPENDENCIES.get(dim_key, {}).get('required', [])
            _filled_deps = [k for k in _dep_req if _is_dim_filled(bb, k)]
            if _filled_deps:
                _dep_full_parts = []
                for _dk in _filled_deps:
                    _dspec = _DIM_KEY_TO_SPEC.get(_dk, {})
                    _dval = (getattr(bb, _dspec.get('field', ''), '') or '').strip() if bb else ''
                    if _dval:
                        if _dk == 'character_profiles' and _dval.startswith('['):
                            _dval = _character_profiles_to_text(_dval)
                        _dep_full_parts.append(f'【已定{_dspec.get("label", _dk)}·方向锁定原文（最高权威，必须严格遵循）】\n{_dval}')
                if _dep_full_parts:
                    _roll_note = ('\n【整体重新生成·reroll】上一次生成的内容作者不满意，请换一个展开角度重新组织本维度'
                                  '（不同的组织结构/切入顺序/细节侧重），但方向仍以上述锁定原文为准，禁止漂移。'
                                  if reroll else '')
                    direct_lock_note = ('\n\n' + '\n\n'.join(_dep_full_parts)
                                        + '\n\n【方向锁定铁律·违规=作废】本维度是执行性展开，上面的已定上游就是方向源头：'
                                          '所有体系/人物/事件必须在其框架内展开细化，禁止另起炉灶、禁止引入与上游冲突的体系/人设/走向；'
                                          '若展开中发现上游有空缺，按上游已有逻辑自然补全，不得反向推翻上游。'
                                        + _roll_note)
        except Exception:
            direct_lock_note = ''

    # 构思阶段·专属规则：通用核心+构思格式约束+master技能包（屏蔽文风/去AI规则）
    # 大纲/剧情维度的专属附加要求在后面组装后，再二次注入 extra_master_note
    extra_master_note_parts = []
    conception_rules = build_conception_rules(skill_pack_ids, mode='agent')

    # 大纲维度额外注入五幕模型说明，确保按卷数生成五幕式结构
    outline_extra = ''
    if dim_key == 'plot_design':
        try:
            from app import _get_total_volumes
            tv = _get_total_volumes(bb, book) or 0
            if tv >= 2:
                outline_extra = f'\n\n【大纲维度专属要求】请生成“五幕式总纲”，全书严格 {tv} 卷，每卷约50章12万字。五幕模型：立身(1-5%)/立足(5-25%)/立势(25-50%)/立威(50-75%)/立命(75-100%)。为每卷输出：卷号卷名、所属幕、本卷核心目标、主要冲突、关键转折点(2-3个)、卷尾高潮与悬念。只输出总纲文本，不输出详细情节节点。'
            else:
                outline_extra = '\n\n【大纲维度专属要求】请生成“五幕式总纲”方向的方案。作者尚未指定总卷数，因此方案内先给出你建议的分卷规模（N卷，N≥2，禁止擅自默认十卷/五卷/十二卷/5-8卷等固定值），再按建议的 N 卷输出五幕式内容：立身/立足/立势/立威/立命对应到连续卷号区间，并为每卷输出：卷号卷名、所属幕、本卷核心目标、主要冲突、关键转折点(2-3个)、卷尾高潮与悬念。只输出总纲文本，不输出详细情节节点。'
            try:
                from app import _cultivation_dimension_hint
                outline_extra += _cultivation_dimension_hint('plot_design', book, bb)
            except Exception:
                pass
        except Exception:
            pass

    # 剧情维度额外注入全部卷剧情约束，确保按全部卷创作
    # 【P0修复】timeline 维度必须输出按卷 JSON 数组（与 ai_master_create 一致），
    # 否则落地时无法按卷 upsert，会被识别成一个整体剧情大纲
    timeline_extra = ''
    if dim_key == 'timeline':
        try:
            from app import _get_total_volumes, _get_chapters_per_volume
            tv = _get_total_volumes(bb, book) or 0
            cpv = _get_chapters_per_volume(bb, book)
            if tv >= 1:
                total_chapters = tv * cpv
                # 注入已有卷剧情作为连贯约束
                existing_volumes = ''
                if bb and bb.timeline:
                    try:
                        vols = json.loads(bb.timeline)
                        if isinstance(vols, list) and vols:
                            vol_lines = []
                            for v in vols:
                                vi = v.get('volume_index') or v.get('volume_id') or '?'
                                vn = v.get('volume', f'第{vi}卷')
                                mp = (v.get('main_plot') or '')[:200]
                                vol_lines.append(f'第{vi}卷《{vn}》：{mp}')
                            existing_volumes = '\n'.join(vol_lines)
                    except Exception:
                        pass
                timeline_extra = f'\n\n【剧情维度专属要求】全书严格 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章。请基于五幕式总纲生成全部 {tv} 卷的剧情，各卷剧情连贯、卷间衔接（ending_hook与下一卷开头承接）。\n【大纲承接铁律】若已提供大纲（plot_design），各卷 main_events 的事件排序必须落在大纲该卷核心目标/主要冲突框架内，ending_hook 必须具体承接大纲该卷"卷尾高潮与悬念"（大纲定钩子方向，剧情写具体事件）；大纲未覆盖的空缺按其逻辑自然补全，禁止另起走向。'
                if existing_volumes:
                    timeline_extra += f'\n\n【已有卷剧情（须保持连贯，可在其基础上完善）】\n{existing_volumes}'
                # 核心密度约束：每卷 summary(总概要) + main_events(8-12个主要剧情事件，默认10)
                # 10个主要事件 × 平均5章 = 刚好支撑 50章 × 约2400字/章 ≈ 12万字正文
                _density_hint = ''
                if cpv and cpv > 0:
                    # 按5章/事件做倒推：期望事件数 = cpv/5
                    expected_events = max(8, min(12, int(round(cpv / 5))))
                    _density_hint = f'（按每卷约 {cpv} 章计算，本卷主要剧情事件建议正好 {expected_events} 个；每个事件平均约 {int(round(cpv / expected_events))} 章正文，1个事件可扩成5-10个节点）'
                timeline_extra += f'''

【分卷铁律·必读】**全书共 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章**。卷序号从 1 开始连续递增到 {tv}。卷名格式"第N卷 副标题"。必须覆盖全部 {tv} 卷，不得多不得少。

【卷级 6 要素铁律（每卷必须在顶层字段标注）】
  · characters：本卷核心出场人物（主角+关键配角，20-40字，如"主角A、女主角B、兄弟配角C、敌对首领D"）
  · timeline_anchor：本卷时间锚点（距开篇多久+关键日期，如"开篇第 3-8 月，仲夏至深秋"）
  · location：本卷主要地点（20-40字，按先后次序，如"出生小镇→城郊黑市→主城外围"）
  · realm_change：本卷境界变化区间（主角起→止，如"初级一段→中级七段·凝神巅峰"）
  · age_change：本卷年龄变化（主角起→止，如"16岁→16岁8个月"）

【两层结构铁律（首次生成 = 卷级6要素+总概要 + 主要剧情事件，不要写 nodes[]）】
  第一层：每卷必须有 卷级6要素 + summary + main_events[]
    · summary：本卷总体剧情概要（覆盖整卷的总故事走向，150-250字）
    · main_events：本卷 **8-12 个主要剧情事件（默认 10 个）{_density_hint}**
    · 10 个主要剧情事件 × 平均 5 章 × 2400字/章 = 本卷约 12 万字正文
    · **【本次修改·关键】main_event 不套章节（不要 chapters 字段）！** 章节分配由后续「节点设计」阶段精确到每章。
    · 每一个 main_event 必须给出"预计支撑章数"用于密度自检（不写章节区间，只写一个 estimated_chapters 数字，10 个事件的 estimated_chapters 加起来必须等于 {cpv}）。
    · 每一个 main_event 结构如下（6 要素齐全，缺一不可）：
        {{
          "index": 1,
          "title": "事件标题（动宾结构，如"城郊黑市夺令牌"）",
          "estimated_chapters": 5,                        // 本事件预计可支撑的章数，合计必须={cpv}
          "summary": "事件概要（80-160字，具体可落地写约5章内容的剧情推进：起→承→转→合→钩子）",
          "characters": "本事件核心人物（2-5人，按出场权重排序）",
          "events": "事件核心推进（20-40字：谁在什么场景做了什么，造成什么关键后果）",
          "time": "本事件时间锚（相对卷级的位置，如"卷首前10天" / "卷中·仲夏祭当天"）",
          "location": "本事件发生地点（精确到具体场景，如"出生小镇·西区工坊·旧仓库"）",
          "realm_change": "本事件结束时主角的境界变化描述（如"突破初级三段，右手法纹点亮" / "境界不动，根基扎实"）",
          "age_change": "本事件结束时主角的年龄/时程变化（如"16岁1个月零5天" / "距开篇3周"）",
          "bury": "（第X卷节点阶段再精确到章，事件层只写"本事件中段埋下：XXX；预计第Y卷回收"。没埋就空串。）",
          "payoff": "（第X卷节点阶段再精确到章，事件层只写"本事件结尾回收：前文/前卷埋下的XXX；效果：XXX"。没收就空串。）"
        }}
    · 10 个主要剧情事件的 estimated_chapters 相加必须恰好等于 {cpv}（缺/多 1 章都不行）；每个 estimated_chapters 建议 4-6，少数可以 3 或 7，但总计严格={cpv}。
  第二层：nodes[]（详细情节节点）**首次生成一律留空数组 []**，由用户在剧情维度点击每卷「节点设计」按钮后，把每个 main_event 再拆成 5-10 个节点事件生成（节点阶段再补 chapters + 精确到章的 bury/payoff）。首次生成严禁把 nodes[] 写满，严禁越俎代庖替用户做节点设计。

【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；第N卷最后一个 main_event 的结尾悬念必须能被第N+1卷第一个 main_event 承接；伏笔 payoff 能跨卷指向第N±K卷 main_event。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹在 markdown 代码块中，不要任何解释性文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "characters": "本卷核心人物（20-40字）",
    "timeline_anchor": "本卷时间锚（距开篇X月-XX月，关键节气/节日）",
    "location": "本卷主要地点路线（20-40字）",
    "realm_change": "本卷境界起→止，如"初级一段→中级七段·凝神巅峰"",
    "age_change": "本卷年龄起→止，如"16岁→16岁8个月"",
    "summary": "本卷总体剧情概要（150-250字，覆盖整卷总走向）",
    "main_plot": "本卷主线剧情（卷内主线推进路径，100-160字）",
    "core_conflict": "本卷核心冲突（对手/阵营/目标冲突）",
    "ending_hook": "本卷卷尾钩子（动态悬念/冲突/转折，承接下一卷开头）",
    "main_events": [
      {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"事件概要（可落地写约5章的具体推进）","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}},
      {{"index":2,"title":"事件2","estimated_chapters":5,"summary":"...","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}}
    ],
    "nodes": []
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。nodes 必须是空数组，首次不要写节点内容！main_events 禁止出现 chapters 字段！'''
            else:
                # 作者未指定总卷数（tv=0）：让 LLM 先给出建议 N 卷，再按 N 卷输出 JSON
                # 禁止任何默认十卷/五卷/十二卷/5-8卷的数字；JSON 结构与 tv 明确时完全一致
                timeline_extra = f'\n\n【剧情维度专属要求】作者尚未指定总卷数。请你先自行确定一个合理的分卷规模 N（N≥2，禁止擅自默认十卷/五卷/十二卷/十余卷/5-8卷等固定值），再按 N 卷生成完整剧情，每卷剧情须支撑约 {cpv} 章容量，卷间衔接（ending_hook 与下一卷开头承接）。'
                # JSON 数组格式铁律（同上，卷数改成"N卷/第N卷"占位规则）
                _density_hint = ''
                if cpv and cpv > 0:
                    expected_events = max(8, min(12, int(round(cpv / 5))))
                    _density_hint = f'（按每卷约 {cpv} 章计算，每卷主要剧情事件建议正好 {expected_events} 个；每个事件平均约 {int(round(cpv / expected_events))} 章正文，1个事件可扩成5-10个节点）'
                timeline_extra += f'''

【分卷铁律·必读】方案建议 N 卷、每卷约 {cpv} 章、全书约 N×{cpv} 章（N 就是你方案里确定的卷数，禁止擅自写死 10）。卷序号从 1 开始连续递增到 N，卷名格式"第N卷 副标题"。必须覆盖全部 N 卷，不得多不得少。

【卷级 6 要素铁律（每卷必须在顶层字段标注）】
  · characters：本卷核心出场人物（主角+关键配角，20-40字）
  · timeline_anchor：本卷时间锚点（距开篇多久+关键日期/节气）
  · location：本卷主要地点路线（20-40字，按先后）
  · realm_change：本卷境界变化区间（主角起→止）
  · age_change：本卷年龄变化（主角起→止）

【两层结构铁律（首次生成 = 卷级6要素+总概要 + 主要剧情事件，不要写 nodes[]）】
  第一层：每卷必须有 卷级6要素 + summary + main_events[]
    · summary：本卷总体剧情概要（覆盖整卷的总故事走向，150-250字）
    · main_events：本卷 **8-12 个主要剧情事件（默认 10 个）{_density_hint}**
    · 10 个主要剧情事件 × 平均 5 章 × 2400字/章 = 本卷约 12 万字正文
    · **【本次修改·关键】main_event 不套章节（不要 chapters 字段）！** 章节分配由「节点设计」阶段再精确到每章。
    · 每个 main_event 必须给出 estimated_chapters（预计支撑章数），N 卷下每卷 main_events[*].estimated_chapters 加起来必须恰好等于 {cpv}。
    · 每个 main_event 结构如下（6 要素齐全，缺一不可）：
        {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"事件概要（80-160字，可落地写约5章推进）","characters":"核心人物2-5人","events":"谁在何场景做了什么→关键后果（20-40字）","time":"相对卷级时间锚（卷首X天/卷中·祭典当天）","location":"精确场景地点","realm_change":"结束时境界变化描述","age_change":"结束时年龄/时程","bury":"本事件X段埋下：XXX；预计第Y卷回收","payoff":"本事件X段回收：前文/前卷埋下的XXX；效果…"}}
    · 合计 estimated_chapters 刚好 {cpv} 章，密度自检不要错。
  第二层：nodes[]（详细情节节点）**首次生成一律留空数组 []**，由用户在剧情维度点击每卷「节点设计」按钮后，把每个 main_event 再拆成 5-10 个节点事件生成（节点阶段补 chapters + 精确到章的 bury/payoff）。首次严禁写 nodes 内容。

【卷间衔接铁律】第K卷 ending_hook 必须与第K+1卷开头严格衔接；第K卷最后一个 main_event 的结尾悬念必须能被第K+1卷第一个 main_event 承接；伏笔 payoff 能跨卷指向第K±M卷 main_event。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹代码块，不要任何解释文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "characters": "本卷核心人物（20-40字）",
    "timeline_anchor": "本卷时间锚",
    "location": "本卷主要地点路线",
    "realm_change": "主角境界 起→止",
    "age_change": "主角年龄 起→止",
    "summary": "本卷总体剧情概要（150-250字）",
    "main_plot": "本卷主线推进路径（100-160字）",
    "core_conflict": "本卷核心冲突",
    "ending_hook": "卷尾钩子承接下卷",
    "main_events": [
      {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"...","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}}
    ],
    "nodes": []
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。nodes 必须是空数组，首次不要写节点内容！main_events 禁止出现 chapters 字段！'''
            # 修炼体系小说：节点须含修炼进展/境界区间/年龄区间/时间线锚点
            try:
                from app import _cultivation_dimension_hint
                timeline_extra += _cultivation_dimension_hint('timeline', book, bb)
            except Exception:
                pass
        except Exception:
            pass

    # 人物维度额外约束：禁止输出 JSON/代码符号，必须用自然语言按字段分行
    character_extra = ''
    if dim_key == 'character_profiles':
        character_extra = """

【人物维度输出格式·绝对铁律·违反即作废】
1. 绝对禁止输出以下任何符号：[ ] { } " " : , 以及英文字段名 name identity personality motivation background relationships abilities items。
2. 绝对禁止输出 JSON 数组或对象，绝对禁止输出代码块。
3. 错误示例（严禁这样输出）：
   [{"name":"主角名","identity":"...","personality":"..."}]
4. 正确格式（必须这样输出，纯中文，每字段一行）：
   姓名：主角名
   身份：边军遗孤，大宗弃徒
   性格：外表沉静寡言，行事果断
   动机：查清蒙冤真相，建立立足之地
   背景：底层劳工出身
   关系：与同营弟兄为生死之交
   能力：古法修行，近身搏杀
   物品：贴身佩刀
5. 多个角色之间用空行分隔，每个角色都按上述格式。
6. 每个字段内容充实具体，至少30字。"""
        # 修炼体系小说：额外要求输出修炼天赋/境界字段
        try:
            from app import _cultivation_dimension_hint
            character_extra += _cultivation_dimension_hint('character_profiles', book, bb)
        except Exception:
            pass

    # 防遗忘检查报告回注：让设定生成也感知已诊断出的问题并主动规避/修正
    af_alerts_gen = ''
    try:
        from app import _collect_anti_forget_alerts
        _af = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
        if _af:
            af_alerts_gen = f'\n\n【防遗忘检查诊断】（最近检查发现的问题，本次生成必须保持一致、主动规避，不可重犯）\n{_af}'
    except Exception:
        pass

    # 构思维度专属：从"一句话故事核"扩充为可支撑百万字的完整故事蓝图（8-10个一级分节）
    concept_extra = ''
    if dim_key == 'concept':
        concept_extra = """
【构思维度·完整蓝图铁律·必须写满10分节·最少1200字·禁止两句话应付】
本书的"构思"不是一句话梗概，而是整本书全部后续维度（世界观、规则、人物、大纲、剧情、伏笔）的源头蓝图；如果这里写得空、两句话结束，下游所有维度的内容都会跟着空洞、敷衍、撑不起来（12万字×N卷的体量需要源头蓝图就有足够密度）。

输出必须按以下 10 个分节依次输出，分节标题要完整保留、不得跳过任何一节。一节最少 80-150 字，10 节合计不少于 1200 字。每一节都要写"具体到能拍板"的内容，不能出现"待设定""后续再定""根据需要补充"这类空话。

一、【一句话故事核（Logline）】
写法：主角(身份+核心处境) + 要什么(显性目标) / 怕什么(隐性恐惧) + 最大阻碍(对手/规则/自己) + 赌上什么(代价与风险)。
错误示范："废柴少年一路逆袭。"（太空）
正确示范："被夺走核心天赋的大宗嫡子沦为底层劳力，为了复仇、为了证明一条不必靠天赋的正道，他必须在五年内以身为炉重启成长之路，赌上全家性命与自己是否还会再次被背叛，最终把夺走自己一切的人从高台上拽下来。"

二、【三幕/五幕主题曲线（贯穿全书的价值主张）】
本书的核心主题是什么？主角的"起点信念"是什么？中期会因为什么事件动摇、对主题产生"反主题"的怀疑？高潮处他如何用最终选择把主题钉死？请给出 起点 → 反诘 → 抉择 → 终局 的4段主题变化，每段 60-120 字。

三、【核心冲突三角（主角 vs 对手 vs 世界规则）】
分别写清楚：
1) 主角（Protagonist）的"外部欲望/内部缺失/执念来源"；
2) 对手（Antagonist / 反派阵营 / Boss 体系）：对手的合理动机（不只是坏，要"他为何这么做、他的世界逻辑是什么、他的创伤/恐惧/执念"）、与主角的镜像关系、两人之间的零和博弈点；
3) 世界规则（System / 铁律 / 天道 / 律法 / 阶层）：世界的规则如何系统性地压迫主角？打破这个规则的代价是什么？——三个维度必须各自独立且彼此咬合。

四、【目标分层（短期/中期/长期/终极）】
给主角设计 4 层目标，每层一个具体可操作的阶段性终点（不是空洞的变强）：
- 短期（第1卷内）：具体可落地的胜负条件（如：逃出某困境、通过某考核、拿到某通行许可、赚到启动资金）；
- 中期（前30%，立足阶段）：建立一个据点/团队/身份/势力雏形（如：跻身前列、加入精英队伍、拥有自家生意）；
- 长期（中后段，立势-立威阶段）：从个人强 → 有自己的班底/地盘/话语权/制度权；
- 终极：与核心冲突三角对决的终局条件（推翻XX、改写XX规则、把XX从高台上拽下、守护住X）；
每一层目标都要写"如果失败了，主角/重要之人会失去什么具体东西"，避免空洞。

五、【核心爽感机制（爽点谱系 + 读者期待曲线）】
请按题材从以下爽感类型中选 3-5 种作为主爽点，并写出每种爽点：首次出现的卷/时机、升级迭代的节奏、爽点公式（谁在谁面前做了什么→观众/世界的即时反应→主角/对手的后续后果），不能全写"装逼打脸"：
  · 废柴逆袭 / 扮猪吃虎 / 打脸逆袭 / 升级跃迁 / 大境界突破
  · 智斗反杀 / 借刀杀人 / 权谋布局 / 以弱胜强
  · 资源暴富 / 赚差价 / 经济霸权 / 产业链垄断（商战/种田/经营资源文）
  · 团队建立 / 兄弟情义 / 红颜情愫 / 师徒传承
  · 势力崛起 / 建国立朝 / 势力扩张 / 战争碾压
  · 血脉觉醒 / 金手指解锁 / 神秘传承揭秘
  · 世界观解谜 / 真相揭秘 / 前文伏笔集中回收
  · 虐渣复仇 / 以牙还牙 / 恶有恶报 / 因果闭环
要写出每一类爽点的"触发→爆发→余波"三拍结构，以及第1卷、第3卷、第5卷、卷末各至少安排哪一次代表性爽点，便于下游剧情维度照此铺排。

六、【金手指/外挂设计（能力 + 约束 + 代价）】
金手指不是越强越好，而是"越贴合主角身份+约束越清晰+代价越具体"越好。请写出：
1) 类型：系统/灵宠/灵魂寄宿/传承/重生经验/血脉/天赋异禀/祖传异宝/空间/契约/职业技能；
2) 核心能力清单：至少 3 条不同方向的能力（主战力 + 辅助 + 资源/情报/成长）；
3) 能力分级：初期解锁什么、中期升级什么、后期才能开什么、满级形态是什么；
4) 硬约束/冷却/资源消耗：每次使用需要什么条件？哪些情况下直接失效？有没有冷却/代价/反噬？（金手指不是想怎么开就怎么开，越有边界，冲突越好看）；
5) 与主角执念的贴合度：金手指如何刚好服务于主角的短期/中期/长期目标？为什么偏偏是他得到了这个金手指，而不是路人？
6) 终极风险：金手指本身会不会变成最终 Boss？会不会有更高级的使用者盯上它？它的来历之谜是否可以作为中后期主线谜团？

七、【主角魅力公式：身份 × 反差 × 创伤执念】
为本书主角写"专属记忆符号"三选一或三选二：
- 记忆符号（外貌/口头禅/小动作）：常年佩戴的旧物、眉眼间的疤痕、习惯性摩挲的随身物件、紧张时的小动作、胜负前的标志性语言；
- 三重反差（至少一层落地成明确设定）：
  · 外在身份 vs 内里实力/权谋（扮猪吃虎）
  · 初始处境 vs 后期状态（起点弱→终局强）
  · 对外态度 vs 对内底线（嘴硬护短、佛系底线、外冷内柔）
- 核心创伤（具体、戳心、贯穿全书）：必须写"哪一年/哪一天/什么具体事件、谁做了什么、主角因此失去了什么、他身体里留下什么具体的印记（伤疤/烙印/残疾/噩梦/对某个词过敏）"，不要写"身世凄惨"空话；
- 核心执念：从创伤衍生出来的、贯穿全书的"我要拿回什么/我要证明什么/我要保护什么/我要赎罪什么/我绝不允许再发生什么"；必须强烈、可行动、可被对手精准利用制造冲突。

八、【对手/反派魅力（反派合理不是纯坏）】
写出至少 2 个层级的对手：
1) 前期对手（第1-2卷）：与主角处于同一阶层/同一地域，理由合理（资源、面子、立场、家族复仇、夺舍、背叛、信仰冲突、上位者命令），写清楚他的"赢面"在哪，不能是纯粹送经验的白痴；
2) 中期对手（第3-5卷）：格局拉高，从个人冲突 → 势力/派系/组织/国家/种族/阶级层面的系统性对手，动机要与他的身份/家族/信仰/创伤绑定；
3) 终局Boss（全书最后）：他所坚持的"世界真理/秩序"是什么？他为什么认为自己做的是对的？他与主角在主题上的根本分歧是什么？最终两人决战的"理念决战层面的胜负"比"战力胜负"更重要。
每个对手都要写：他的"合理诉求 / 他的创伤 / 他的赢面 / 他的软肋 / 他与主角的镜像点"。

九、【世界观卖点钩子（世界的"独特之处"）】
本书的世界与同题材其他书比，最独特的 3-5 个设定卖点是什么？（例如：凡人修仙传的"灵根决定一切但资源寿元逼死人"、诡秘之主的"序列途径+扮演法+特性守恒"、全球高武的"地窟入侵+财富换气血"、道诡异仙的"病与道互相映射"）。每个卖点写清楚：独特之处是什么 + 它会催生出什么独有剧情/冲突/爽点 + 第1卷首次在哪种情境下亮相。

十、【全书情感底色 + 读者定位 + 文风力向】
- 情感底色：是热血少年向/稳扎稳打凡人流/黑暗复仇流/轻松欢乐流/治愈治愈向/权谋智斗流/群像史诗流？
- 读者画像：主要读者在什么情绪下最爽？（下班解压、通勤放松、代入逆袭、解谜、看战争碾压、看情感、看种田经营）
- 文风力向建议：语言调性（文言感/口语化/冷硬/幽默/细腻/史诗）、节奏密度（每N章一个小高潮、每卷1-2个大高潮）、战斗/日常/成长/情感/经营/战争的大致比例。
- 最后写一句"如果下游维度的设定内容有冲突，这一节所定的情感底色优先。"
"""

    # 核心规则维度专属：从"一句话等级"扩充成体系化能力/修炼/科技/经济规则库
    key_rules_extra = ''
    if dim_key == 'key_rules':
        key_rules_extra = """
【设定（核心规则）维度·完整体系铁律·最少1500字·分11节输出·禁止几句话就完】
本维度是"世界的物理/社会法则"，下游所有剧情、战力、人物、势力、经济都要在这个规则体系内自洽。禁止只写"境界一到九段"就结束。必须按以下 11 节依次输出，一节不跳过，合计不少于 1500 字，每一节都要有具体数字、例子。

一、【力量总体系分类（总览表）】
先给一张分类清单：本书的力量/能力属于以下哪一大类（允许混合，最多选 2 主类 + 1 辅类）：
  · 修炼体系（东方玄幻/仙侠/武侠/武道）
  · 魔法/巫术体系（西方奇幻/骑士/魔兽）
  · 序列/职业/扮演体系（如诡秘）
  · 科技树/基因/机甲/星舰体系（科幻/机甲/末世/星际）
  · 异能/规则系（都市异能/规则怪谈/超能力）
  · 国术/军武/谍报体系（近现代军事/谍战）
  · 经营/种田/职业经营（经商、基建、种田、网游、经营流）
  · 文运/国运/言灵（文道、以诗杀敌、言出法随）
写出各分类在本书中"谁掌握"（种族/国家/势力/门派/职业/阶层）、它们之间的克制关系（如：科技vs修炼谁强谁弱、什么条件下能跨类对抗）。

二、【等级阶梯表（境界/阶位/军衔/职称）】
写出完整的等级阶梯，按"凡人→登堂→入室→大成→登峰→破界→成神/至尊/不朽..."的逻辑分层，每一大层都要写：
1) 等级命名（如：淬体→通脉→凝气→化液→固晶→金身→神念→法相→洞天...，或：列兵→下士→上士→少尉→少校→少将→上将→元帅）；
2) 每级之间的战力差距（量化：如：一级可打10个普通人、二级可打100个；或：练气和筑基战力差 10 倍，筑基可跨一级但不能跨两级）；
3) 每级之间的"突破门槛"：资源、条件、感悟、试炼、风险、失败后果；
4) 每级对应的社会地位：初阶/中阶/高阶/大师/宗师/圣/神 → 对应在组织/国家/种族里的职称与权力；
5) 寿元上限：每阶寿元多少？衰老速率？意外死亡的常见方式？
6) 战力天花板：本卷末主角到哪？全书完结主角到哪？最强者到哪？——明确写清楚，避免后期战力崩。

三、【核心修炼/提升路径（两种以上路线的差异）】
不要所有人走同一条路，至少给 2 种主要提升路径 + 1 种偏门路线：
- 主路线A（大众路线）：门派/正统/学院/国家体系 → 优点/缺点/社会认可度；
- 主路线B（民间路线）：家族传承/野路子/黑市传承/异族功法 → 优点/缺点/风险；
- 偏门路线C：禁术/魔修/外道/夺舍/机械飞升/信仰成神 → 代价/反噬/被追杀的理由；
每条路径要写"它的天花板在哪、适合什么人、与其他路线的克制关系"。

四、【功法/技能树（分类分级 + 稀有度 + 配搭建议）】
把功法/技能/魔法/科技模块按"战斗系/辅助系/经营生产系/生活系/秘术禁术"分类，再按等级分级（凡/灵/宝/法/道/圣/神，或 E/D/C/B/A/S/SS）：
- 每大类至少举 3-5 个代表性技能，说明其效果、学习门槛、资源消耗、实战中最强的点和最弱的点；
- 常见的"技能搭配组合"（如：近战法修 + 瞬发低阶盾 + 身法），禁止万能主角什么都会；
- 稀有技能的获取方式（秘境/传承/血脉/功勋兑换/黑市/奇遇/金手指），并写明获取风险。

五、【资源与货币体系（完整经济系统，这是支撑种田/经营/修炼类文密度的关键）】
必须写完整，别让主角钱和资源像天上掉下来的：
1) 通用货币：名称、单位、进制（如：灵石，1上=100中=10000下；或：帝国信用币/联邦积分）；
2) 辅助货币/等价物：丹药、矿石、符箓、法器、兵器、布匹、粮食、贡献点、功勋点、军功、门派令牌；
3) 常见物品的价格参考表（至少 10 项）：如炼气期丹药 1000 下灵以下、筑基期 10 万下灵以下、金丹 1000 万下灵以下；一台机甲/一阶妖兽核/一块玄铁/一把制式武器值多少？
4) 资源产地：哪些地区产哪些核心资源？这些地方被谁掌控？资源争夺是不是主线冲突之一？
5) 贫富差距与修炼成本：突破一阶平均要花多少资源？底层/中产/顶层各自能承担到哪一阶？为什么大多数人卡在某一阶？

六、【装备/法宝/载具系统】
装备/机甲/飞船/法器/灵器/法宝/道装/护甲/兵器：
1) 分级与命名：凡器/灵器/宝器/法器/道器/仙器/神器（或 E→SSS），每阶对应战力加成；
2) 获取方式：自制/铁匠/锻造师/炼金术士 → 材料清单 + 打造门槛；
3) 绑定/认主方式：滴血/神识绑定/契约/基因锁/权限码；
4) 损耗与修复：会不会爆？会不会碎？如何修复？修复代价；
5) 代表性装备：列举 3-5 件核心装备（主角的本命刀、中期的舟、后期的阵盘），分别对应什么阶段的战力升级。

七、【炼丹/炼器/阵法/符箓/御兽/驯虫/符文/编程/制造 等生产/副职业】
这些副职业是填充正文密度、支撑经营/种田/爽点的关键，不是可有可无：
1) 选择本书至少 2-3 种副职业，写清楚它们的等级阶梯；
2) 各副职业对主职战力的加成方式（如：炼丹出丹药给修炼加速 + 给队友补血；炼器给自己量身做装备；阵法可布置组织防线/战场困敌；御兽直接多一个战力）；
3) 副职业的"升级门槛"：材料/经验/传承/配方/图纸，为什么稀缺？
4) 写出 3-5 个代表性物品（如：筑基丹、九转金丹、迷踪阵、天雷符、三级机甲引擎），分别说明配方、效用、市场价格。

八、【修炼/能力的硬约束 + 反噬代价】
为了让剧情有冲突，能力必须有边界：
- 强行越级/禁术/连轴战斗的反噬是什么？（经脉受损、寿元衰减、精神错乱、走火入魔、机械臂过载、基因崩坏、被系统惩罚）
- 有没有"心魔/外魔/天魔阻道"？什么时候出现？怎么过？失败的后果？
- 能力/境界能不能"掉落"？掉落之后如何恢复？有没有"二次破境更上一层"的可能？
- 金手指/核心能力的"冷却时间/资源消耗/使用条件/触发场景"，至少写 3 条边界条件。

九、【种族/职业/阵营能力克制表】
【维度边界】本节只写"能力与克制"（战斗/对抗怎么算）；种族的文化信仰、栖息地、外貌寿命、种族关系史归"世界观"维度种族大观节，本节不重复展开。
列出本书的主要种族/职业/阵营（人族 / 妖族 / 魔族 / 灵族 / 机械族 / 异能者 / 修士 / 星舰军官 / 江湖门派 / 教会 / 联邦...）：
- 每一方的核心能力方向、战斗优势、战斗劣势（只写能力面）；
- 种族/阵营之间的能力克制关系（谁克谁、什么条件下能跨级对抗）；
- 跨种族/跨阵营的能力禁忌（夺舍？吞噬？血脉污染？借用异族之力会怎样？）。

十、【世界硬规则与禁忌（铁律·剧情冲突的发动机）】
【维度边界】本节只管"力量使用与超凡行为的禁忌"（修炼/异能/科技使用层面的铁律）；社会治理类律法（刑法/审判/政体/税收）归"世界观"维度政治与律法节，本节不重复。
列出至少 8 条"世界铁律"，人物如果违反就会被追杀/死亡/天谴/降级/反噬/被剥夺权力：
- 如：夺舍 = 魔道 / 修炼者不能对凡人大开杀戒 / 热武器在XX地失效 / 飞升或成神要献祭XX / 系统拒绝作弊 / 非凡者在公众面前暴露异能会被官方清洗 / 谁敢动XX遗迹谁就被诅咒 / 高级修士不可随意干预凡间；
每条铁律还要写：执法者是谁？执法手段？是否有灰色地带/暗规则可以钻空子？主角会不会在剧情中主动/被动违反这些铁律？铁律本身是不是最终 Boss 用来维持秩序的工具？

十一、【体系天花板总览（战力/科技上限）】
【维度边界】本节只写"力量体系的层级与天花板"（各级战力/科技上限）；文明的政体规模、社会形态、平均生活水平归"世界观"维度势力/阶级节，本节不重复。
如果是科幻/机甲/星际/末世：写清科技树（能源、武器、护盾、跃迁、AI、生物技术、外骨骼、星舰等级、反物质）、当前主角所处的科技层级与全书天花板差多少？
如果是玄幻/仙侠/奇幻：写清修炼文明的实力层级（王朝→大宗→圣地→皇朝→仙朝→神朝），每一级对应的平均实力上限、可调动战力规模；
最终给出一句"下游任何剧情/战力/装备/经营描写如果和本节铁律冲突，以本节体系为准，必须自动修正。"
"""

    # 世界观维度专属：从"简单大陆介绍"扩充到15节级完整世界百科
    worldbuilding_extra = ''
    if dim_key == 'worldbuilding':
        worldbuilding_extra = """
【世界观维度·完整世界百科铁律·最少2000字·分15节输出·禁止两句话就完】
本维度是"故事发生的世界全景"，下游剧情的地理移动、势力站队、国家战争、势力冲突、种族矛盾、资源战争、风土人情都从这里来。不能只写"主角所在的大陆有东/西/南/北四大域"就结束。按以下 15 节依次输出，一节不跳过，合计不少于 2000 字。

一、【世界总览（宇宙/大陆/位面/世界树结构）】
从最高维度开始讲：世界是几层结构？（如一界/三界/三十三天/多元宇宙/源海+源核+界域/单一大陆+N个秘境/星球数/联邦星域）。主角所在的"主舞台"叫什么？整体面积相当于几个地球？海拔？气候带？大陆上有几大板块？（如一块超级大陆 + 若干附属岛链 + 禁忌海）。世界的"边界"在哪里？越界会怎么样？——把一张世界地图的大致轮廓用文字描述出来。

二、【世界起源/创世纪元史（至少3段创世神话 + 真实历史版本）】
- 民间神话版（老百姓信什么？各个种族的创世神是谁？）；
- 学术界/学院/圣廷/图书馆"正史版"；
- 隐藏真相版（作为中后期主线谜团，可以不揭谜底，但要留下"有问题的缝隙"）；
至少包含 3 个大纪元：远古纪元（太初/开天/第一文明）→ 中古纪元（王朝/神朝/大战/大断层）→ 近古纪元（秩序重建/大航海/灵气复苏/工业革命）→ 当代纪元（主线开局前 100 年发生了什么关键事件）。每个纪元至少 1 件"改变世界格局的大事"。

三、【地理分块（主大陆/次大陆/群岛/禁地/秘境/天堑）】
把主舞台按地理拆成至少 6 个"大区"，写出：名字、气候、地形特色（山脉/平原/森林/沙漠/沼泽/火山/冰原）、资源特产、常住人口、主要势力控制者、区域内部最大的矛盾、对主线剧情的作用；
必须包含至少 4 类特殊区域：
1) 开局出生区域（主角起家/被欺负/翻身的起点）；
2) 中期主要舞台（大宗/学院/都城/大型岛屿，在此发展势力）；
3) 禁地/绝地/秘境（危险但有机缘，中后期主线突破区）；
4) 跨区域天堑（如：无法穿越的禁忌海/需要传送阵才能过的大峡谷/空间裂缝带/辐射污染区——是剧情推进时天然的关卡）。

四、【气候与天象体系】
这个世界有没有"灵气潮汐"、"季节紊乱"、"极夜极昼"、"九星连珠"、"血月"、"圣日"、"天劫"、"灵雨/红雨/陨石雨"等特殊天象？
- 它们发生的周期？对修炼/战争/经济/农作物/社会心理有什么具体影响？
- 哪些天象会被视为吉兆/凶兆？宗教仪式是如何利用它的？——这会催生出大量可落地的剧情事件（如：每十年一次的潮汐大典 = 学院大比 + 资源拍卖会 + 敌对势力偷袭的绝佳时机）。

五、【主要势力总表（至少8个）】
至少列 8 个势力，按"大宗/王朝/帝国/家族/商会/教会/地下组织/异族联盟/学院派/军方"等不同性质分类，每一方写：
- 势力名称、性质、级别（王朝/大宗三级）、领袖、核心人物2-3人；
- 地盘（哪几个大区）、核心人口/兵员/成员规模；
- 核心战力（首领/国师/老祖什么境界、王牌军团/阵法/秘宝）；
- 经济来源（收税/矿产/商铺/炼丹/走私/奴隶/功德香火/官方拨款）；
- 理念/立派宗旨/意识形态/对外关系（和谁是盟友、和谁世仇、与主角利益的交集点）；
- 剧情位置：第1卷出现哪些？第2-4卷卷入哪些？终局谁是友谁是敌？

六、【社会制度与阶级分层】
这个世界的"人分几等"？写出完整的阶级金字塔：
- 顶层：皇帝/教皇/宗主/圣地传人/神裔/贵族/世袭大家族 → 拥有哪些权力？（生杀？立法？免税？垄断资源？）
- 中层：修士/军官/官员/富商/职业者 → 上升通道是什么？（科举？组织考核？军功？捐官？商道？）
- 底层：凡人/劳工/佃农/流民/佣兵/学徒 → 被盘剥的方式？翻身的概率？
- 禁忌群体：魔修/异族混血/偷渡者/贱籍/黑户 → 他们如何生存？主角是否曾属于这一类？
写出各阶层之间的流动是开是闭？有没有严格的种姓/血脉/出身限制？阶级矛盾是不是主线冲突的发动机？

七、【政治与律法（谁制定规则、谁执法、谁钻空子）】
【维度边界】本节写社会治理面（政体/法律来源/执法审判/灰色地带）；力量使用层面的超凡禁忌（夺舍=魔道、暴露异能被清洗等）归"设定"维度世界硬规则节，本节不重复。
- 国体：君主专制/贵族共和/组织议会/联邦民主/神权/军政府？
- 法律的来源：神谕/皇帝敕令/组织戒律/商会章程/旧例判例；
- 执法者：禁军/锦衣卫/刑堂/审判庭/治安司/赏金猎人；
- 审判程序：公开审判/私刑/决斗审判/神判；
- 灰色地带：黑市/地下钱庄/灰色法条/买官卖官/贵族豁免权——主角前期怎么在灰区活命、怎么利用灰区翻身、后期要不要打破这些灰区规则？

八、【经济与贸易系统（生产/运输/商路/黑市）】
【维度边界】本节写产业/商路/贸易/黑市与货币发行权；货币的具体面值、换算进制、物品价格表归"设定"维度资源与货币体系节，本节不重复列价格。
1) 主要产业：农业/矿业/制造业/修炼业/服务业/信息业；
2) 核心商路：哪几条大路/运河/航线/传送阵是经济命脉？分别掌握在谁手里？谁能卡住谁的脖子？
3) 主要港口/坊市/交易都市：至少 3 个，写清它们的特色、税收制度、地下势力；
4) 黑市：哪里有？做什么交易？（禁药、禁器、人口、情报、违禁功法、异族货物）；执法力度如何？
5) 通货膨胀/货币发行权：谁能铸币？谁能印钞？会不会恶性通胀？主角是否会在中期通过经营/商战掌控货币权？

九、【种族大观（至少5个智慧种族）】
【维度边界】本节写种族的文化面（栖息地/信仰/习俗/种族关系/社会地位）；种族的战斗能力方向与克制关系归"设定"维度能力克制表节，本节不重复列战力数值。
列出 5 个以上智慧种族（人/妖/魔/灵/龙/矮/精灵/鲛人/石族/亡灵/机械族/异兽族…），分别写：
- 外貌、寿命、繁衍方式、栖息地、核心优势、核心短板；
- 文化信仰、禁忌、主流价值观；
- 与其他种族的关系（被谁奴役？与谁通商？与谁世仇？）；
- 混血种群（半妖/半魔/半精灵/人机混合体）的社会地位与歧视链；
至少选 1 个种族作为"主角阵营的盟友"，1 个作为"全书主对手族群"，1 个作为"灰色中间势力，时敌时友"。

十、【宗教、信仰、神话体系】
世界上的主要宗教/信仰（至少 2 个正统 + 1 个邪教/秘教）：
- 神祇、神系、教义、主神、正神、邪神；
- 教会结构：教皇/大主教/神父/修士/骑士团/裁判所；
- 信仰的力量：信徒祷告能否产生神力？神是否会显圣？神会不会死？神与修炼体系的关系？
- 宗教与世俗政权的关系（教权>皇权？还是对立？）；
- 邪教/秘密教派存在的土壤是什么？它的教义虽然扭曲但有没有合理的底层吸引力？主角会不会被误判为邪教徒？

十一、【语言、文字、度量衡】
- 通用语、各族语言、古语/神语/加密语（龙语、精灵语、神纹、古篆、机械编码）是否存在？谁掌握？懂古语是否等于掌握传承/开启秘境的钥匙？
- 文字：方块字/拼音/符文/立体文字？识字率？底层人是否普遍文盲？主角是否因为识字/会古语而产生优势？
- 度量衡：长度（丈/尺/米/里/光年）、重量（斤/两/石/吨）、时间（时辰/刻/日/月/年/纪元/星历）、面积（亩/顷/平方公里）、容量（斗/升/桶）；
- 历法：节日/节气/圣日/祭日/战争纪念日——剧情中大型事件（考核、婚礼、大典、宣战、刺杀）常常放在节日，直接可用。

十二、【风俗、礼仪、服饰、饮食、建筑】
- 出生礼、成人礼、婚礼、葬礼、祭祀大典：具体流程是怎样的？哪些环节可以被对手利用制造冲突？
- 阶层服饰规制：什么颜色/纹样/材质/佩饰是某阶级专属？僭越会怎样？
- 饮食：主食、肉食、饮品、酒文化、饮茶、宴席座次；不同地域/种族口味差异；
- 建筑风格：山门洞府/皇宫/市井民居/店铺/书院/地下世界/异族营地——描写具体场景时要能对号入座。

十三、【军事与战力体系（组织/兵种/阵法/战争规模）】
如果有战争/国战/势力战：
- 军事组织：禁军/边军/卫所/大宗护山队/骑士团/雇佣军/星舰舰队；
- 兵种：步兵/骑兵/弓箭/重甲/法师/术师/飞骑/舰炮手/机甲兵/异兵种；
- 阵法/战阵/舰队编队：列阵的增益、破阵的代价、历史名战案例；
- 战争规模：万人级/十万人级/百万人级/星域级？一场大战消耗多少资源（粮草/弹药/灵石/丹药/人命）？战后如何恢复？——支撑中后期卷的战争线密度。

十四、【交通与通讯（跑图/传信的速度与成本）】
这会影响剧情节奏：
- 步行/骑马/飞骑/马车/宝船/飞舟/传送阵/虫洞/跃迁引擎：各自的速度、成本、门槛、谁能使用？
- 传信：飞鸽/信鸟/传音玉简/传讯符/灵网/无线电/量子通讯/星链：速度、距离、保密等级、是否可以截获/伪造？
- 写出"从A地到B地"的典型行程时间与代价：主角能不能追得上一场大战？紧急情报什么时候到？——剧情节奏的硬约束。

十五、【世界的未解之谜/禁忌之地/上古遗留】
这些是中后期的剧情燃料、世界观解谜、伏笔谜底、终极BOSS来源。至少列出 5 个：
- 每个谜团写：已知传说、主流猜测、真实真相留空（但要暗示一个方向）、与主线的关联（主角会在第几卷因为什么事件接触到它）、揭开它会带来什么改变（颠覆秩序？获得传承？引入更大的外患？）；
最后写一句："下游剧情、伏笔、人物背景中如果涉及谜团，均以本节为总源头，不得互相矛盾。"
"""

    # 地图维度专属：结构化地图条目（按地点清单式输出，每个地点至少120字）
    locations_extra = ''
    if dim_key == 'locations':
        locations_extra = """
【地图维度·结构化地点铁律·最少1200字·每个地点一条·禁止只写地点名】
输出按"地点清单"结构，编号从 1 开始连续递增，至少列出 12 个关键地点，每条地点至少 120 字，合计不少于 1200 字。每个地点必须包含以下 8 项（缺少任何一项视为敷衍）：
1) 名称：正式名 + 俗称/古名 + 所属行政区/大区；
2) 地理：经纬度大貌（东/西/南/北/中）、地形、气候、物产、天险；
3) 归属势力：宗主国/统治组织/占领军/割据家族/地下势力；
4) 核心建筑/地标：至少 3 处具体地标（城门/广场/大殿/学院/工坊区/港口/传送阵/禁地）；
5) 人口与阶层：人口规模、核心阶层、歧视链情况；
6) 经济命脉：主要产业、商会、税收、黑市；
7) 剧情作用：哪几卷作为主舞台？第一次出场时发生什么核心事件？
8) 隐藏信息/伏笔：此地埋着什么未解之谜/旧战场/上古遗址/禁忌？会不会在中后段再作为主战场回归？
至少包含以下类别各 1-2 处：
  · 主角出生/幼年居住地
  · 第1卷开局受苦/翻身地（苦役营/小镇/贫民窟/边城）
  · 中期大宗/学院/都城/主基地
  · 大型商业城市/坊市/港口
  · 禁地/秘境/古战场/遗迹
  · 边关/战区/要塞
  · 敌国首都/敌对阵营核心地盘
  · 中立区/三不管地带/灰色地带
"""

    # 伏笔维度专属：结构化伏笔（埋设章/回收章/权重/依赖/埋收方式）
    foreshadowing_extra = ''
    if dim_key == 'foreshadowing':
        foreshadowing_extra = """
【伏笔维度·结构化长短线铁律·最少1000字·每条伏笔1张卡片·禁止只有几句话】
输出必须是按"长线伏笔 + 中线伏笔 + 短线伏笔"三层结构，总伏笔数不少于 15 条，合计不少于 1000 字。每条伏笔必须按以下 10 项写满（少一项视为敷衍）：
1) 编号：F-N（长线F1-F5 / 中线M1-M7 / 短线S1-S5...）
2) 类别：身世型 / 宝物型 / 能力型 / 势力型 / 人物型 / 世界真凶型 / 规则型 / 情感型 / 因果复仇型；
3) 伏笔内容（一句话讲清埋什么）；
4) 埋设方式：对话口误/旧物细节/背景消息/路人闲聊/异象/梦境/半截信/残缺功法/某NPC反常行为；
5) 埋设位置：建议卷+建议章号（或"第1卷.E2"即第1卷第2个主要剧情事件）；
6) 回收位置：建议卷+建议章号（长线必须跨2卷以上，中线跨1卷内若干事件，短线30章内收）；
7) 权重 1-10：权重10=终局核心、1=气氛点缀；
8) 依赖项：必须先回收哪几条伏笔才能回收这一条？（例如：F5 依赖 M2、S3 先收）；
9) 收伏笔时的"爆点"：回收时要给读者什么强反馈？打脸/真相大白/战力暴涨/势力翻盘/情感大反转/世界秩序颠覆？；
10) 防遗忘备注：当正文写到埋设章附近时，要自然地"提一嘴"，避免读者忘记（AI写作时会自动检测，不准漏埋）。
必须保证：长线≥4，中线≥6，短线≥5；并至少有 2 条跨卷大伏笔（贯穿全书 F 级）终局才回收。
"""

    # 大纲维度专属：原 outline_extra 基础上再加"五幕·每幕指标清单"防一句化
    if dim_key == 'plot_design':
        # 这里不再覆盖 outline_extra，而是往它后面追加分节清单
        outline_extra += """
【大纲分节清单·每卷必须写满 6 项 + 跨卷承接 + 爽点排布·最少1500字】
除了五幕对应和核心目标外，**每一卷**你必须再额外把以下 6 项写出来，缺一项视为敷衍：
1) 本卷爽点排布（至少4个小爽点+1个大高潮爽点），分别对应剧情哪个阶段；
2) 本卷人物方向（新登场的重要人物类型与出场目的，1句话方向即可，如"引入中期对手××及其势力"——具体人物名单与塑造归"人物"维度和"剧情"维度 characters，大纲不展开）；
3) 本卷地点动线方向（起点→主要舞台→卷尾所在，1句话方向即可——具体地点清单与地理细节归"剧情"维度 location 和"地图"维度，大纲不展开）；
4) 本卷主角的"修炼/事业/财富/关系/势力"五项进展指标，每项分别从X到Y；
5) 本卷伏笔主题方向（本卷侧重埋什么类型的伏笔，如"主角身世线+上古遗迹线"，1-2句话方向即可——具体埋设条目/回收位置归"剧情"维度 bury/payoff 和"伏笔"维度，大纲不展开）；
6) 本卷结尾处主角"得到了什么 / 失去了什么 / 主动承担的新任务"是什么，确保能把下一卷拉起来；
【维度边界·防与剧情维度重复】大纲是目标层：写"每卷要完成什么、爽点怎么配、钩子往哪指"；人物名单/地点清单/伏笔条目/事件排序是执行层，归"剧情"维度（其卷级 characters/location 与事件 bury/payoff 会按卷展开），大纲写到方向为止，禁止逐条展开。
另外，整体大纲结尾要补一张【跨卷连贯性总览】表格化文字（文本即可）：
第1卷尾钩子 ←接→ 第2卷开头契机
第2卷尾钩子 ←接→ 第3卷开头契机
……
保证每一卷的 ending 都不是空洞悬念，而是能被下一卷第一幕直接承接的具体事件/危机/任务。
"""

    # 文风维度专属：从"叙事风格几句话"扩充到8分节风格手册
    style_extra = ''
    if dim_key == 'style_guide':
        style_extra = """
【文风指南维度·8分节风格手册·最少1000字·禁止只写"语言流畅有张力"】
你必须按以下 8 节输出风格手册，一节不少，合计不少于 1000 字，下游正文生成时会逐条对照：
1) 总体调性（热血/稳扎稳打/冷硬/幽默/细腻/史诗/治愈/暗黑复仇/轻小说欢乐）；
2) 叙事节奏：每N章一个小高潮、每卷几个中高潮、卷尾大高潮比例，日常/打斗/修炼/经营/对话/情感/权谋/战争/解谜的篇幅占比（加起来=100%）；
3) 描写比例：外貌、服饰、场景、心理、动作、对话各自的篇幅倾向（多/中/少）；
4) 战斗描写：白描还是华丽？先出招式名还是先写后果？一招之内要写几层细节（攻击→格挡→借力→反击→余波→围观反应→双方心理→战后代价）；
5) 对话风格：书面化？口语化？短句多还是长句多？有没有专用尊称/敬语/异族语调/古白话/翻译腔？
6) 视角规则：严格第三人称有限视角？能否上帝视角？换POV时的切换规则（章节开头？空行分隔？）？能否切换到反派视角？
7) 爽点触发写法：打脸/升级/赚钱/获得宝物/团队胜利——这些爽点统一按"酝酿→压→爆→反馈→余波"五拍来写，每拍多少比例？
8) 高压线：绝对禁止出现的词、句式、桥段（如过度玛丽苏、反复"倒吸凉气"、全员降智、女主花瓶、解释性大段独白、战力乱跳、章节无钩子断章、现代脏话出现在古代背景、作者跳出来吐槽等），列出至少 10 条。
"""

    # 【P0修复】timeline 维度输出按卷 JSON 数组，末尾不附加"300-800字纯文本"规则
    # 否则 AI 会输出纯文本大纲，落地时无法按卷 upsert。
    # 但剧情维度 main_plot/ending_hook/nodes.summary 等字段是自然语言文本，
    # 仍必须遵守叙事工艺铁律，注入 JSON 兼容的专用版。
    if dim_key == 'timeline':
        _tail_rule = f'\n\n{TIMELINE_NARRATIVE_RULES}'
    else:
        _tail_rule = f'\n\n请直接输出该维度的完整设定内容（300-800字），不要寒暄，不要解释，不要加 Markdown 标题。\n\n{PLAIN_TEXT_LAYOUT_RULES}'

    # 把维度专属附加规则合并到 conception_rules 末尾，一并注入
    if outline_extra:
        conception_rules += '\n\n' + outline_extra.strip()
    if timeline_extra:
        conception_rules += '\n\n' + timeline_extra.strip()
    if concept_extra:
        conception_rules += '\n\n' + concept_extra.strip()
    if key_rules_extra:
        conception_rules += '\n\n' + key_rules_extra.strip()
    if worldbuilding_extra:
        conception_rules += '\n\n' + worldbuilding_extra.strip()
    if character_extra:
        conception_rules += '\n\n' + character_extra.strip()
    if locations_extra:
        conception_rules += '\n\n' + locations_extra.strip()
    if foreshadowing_extra:
        conception_rules += '\n\n' + foreshadowing_extra.strip()
    if style_extra:
        conception_rules += '\n\n' + style_extra.strip()
    if af_alerts_gen:
        conception_rules += '\n\n' + af_alerts_gen.strip()
    # 【direct 模式】上游方向锁定块置于规则块最前（权威最高，先于分节清单读到）
    if direct_lock_note:
        conception_rules = direct_lock_note + '\n\n' + conception_rules

    # 预拼接块（Python 3.11 禁止 f-string 表达式内含反斜杠，故先算好再引用）
    _self_content_block = ("【当前维度已有内容（可在此基础上完善，不要简单重复）】\n" + self_content) if self_content else ""
    _skill_note_block = ("【构思阶段·平台内置规则 + 技能包方法论 + 本维度专属要求】\n" + conception_rules) if conception_rules else ""

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》生成“{spec['label']}”维度的完整设定内容。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

{core_params}

【已有设定参考】
{ctx or "（暂无）"}

{_self_content_block}

【作者需求】
{requirement or "无"}

【选中方案】
{suggestion or "（整体重新生成：不基于旧方案，直接按上方方向锁定原文与规则重新展开）"}

{_skill_note_block}
{_tail_rule}"""

    # 用户采纳的"系统学习与优化建议"补丁：维度生成时必须同样遵守
    if bb:
        try:
            from meta_optimizer import build_active_patch_text
            _pp = build_active_patch_text(bb)
            if _pp:
                sys_prompt += '\n\n' + _pp
        except Exception:
            pass

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting',
                                              title=f'{spec["label"]}生成')
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    # 世界观维度：生成后额外产出地图卡片，便于提取到“地图”维度
    if dim_key == 'worldbuilding':
        sys_prompt += '\n\n另外，若世界观中包含地理/势力分布信息，请在正文之后追加一张“地图”卡片，格式：\n[[CARD:SAVE_LOCATION|世界地图架构|在此输出主要地理区域、势力分布、关键地点的简要架构]]'

    # 设定维度：文风已并入设定，额外产出一张“文风”卡片，便于落地到 style_guide
    if dim_key == 'key_rules':
        sys_prompt += '\n\n另外，请基于本书题材与设定，提炼出适配的文风指南（叙事风格、语言调性、节奏把控），在正文之后追加一张“文风”卡片，格式：\n[[CARD:APPLY_STYLE|文风指南|在此输出叙事风格、语言调性、节奏把控等文风约束]]'

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请生成{spec["label"]}的完整内容'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        full = []
        try:
            # 【P1改进】生成前依赖检查：前置维度未完善时下发提示（不阻断）
            try:
                readiness = check_dim_readiness(bb, dim_key)
                if readiness.get('warning'):
                    yield sse({'type': 'meta', 'kind': 'dependency_warning', 'info': readiness})
            except Exception:
                pass

            # 用户方案直接落地：不调用 LLM，原样流式 delta，保证用户原文一字不改
            if from_user_paste:
                clean_content = suggestion
                if dim_key == 'timeline':
                    # 保留 JSON 结构，仅剥 markdown 代码块包裹
                    fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', clean_content)
                    if fence:
                        clean_content = fence.group(1).strip()
                else:
                    clean_content = _clean_text_to_plain(clean_content)
                    if dim_key == 'character_profiles' and clean_content.lstrip().startswith('['):
                        clean_content = _character_profiles_to_text(clean_content)
                # 流式 delta：按 80 字/块输出，保持前端打字效果一致
                _chunk_sz = 80
                for _i in range(0, len(clean_content), _chunk_sz):
                    yield sse({'type': 'delta', 'content': clean_content[_i:_i + _chunk_sz]})
                # 世界观/核心设定 维度：用户原文中如果没有卡片，也不 AI 衍生（用户贴啥就是啥）
                extra_cards = _parse_card_markers(clean_content)
                if extra_cards:
                    if dim_key == 'timeline':
                        body = _strip_card_markers(clean_content)
                    else:
                        body = _clean_text_to_plain(_strip_card_markers(clean_content))
                else:
                    body = clean_content
                card = {
                    'id': str(uuid.uuid4())[:8],
                    'type': spec['card'],
                    'title': f'{spec["label"]}（用户方案直接落地）',
                    'content': body,
                    'target': _CARD_TARGET.get(spec['card'], spec['label']),
                }
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
                for ec in extra_cards:
                    ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                    if ec.get('title'):
                        ec['title'] = _clean_text_to_plain(ec['title'])
                    yield sse({'type': 'card', 'card': ec, 'session_id': session_id})
                history = load_session_messages(session)
                history.append({'role': 'user', 'content': f'落地用户{spec["label"]}方案'})
                history.append({'role': 'assistant', 'content': body,
                                'cards': [{**c, 'status': 'pending'} for c in [card] + extra_cards]})
                _safe_save_session_messages(session, history)
                yield sse({'type': 'done', 'session_id': session_id})
                return

            # 按维度给足 token（旧值 2000 会把设定/世界观截成半截，见 _DIM_MAX_TOKENS 注释）
            max_tok = _dim_max_tokens(dim_key)

            # 【P0改进】LLM 调用 + 生成后自检重试
            from .post_gen_validator import PostGenValidator
            try:
                from app import _get_total_volumes, _get_chapters_per_volume
                _tv = _get_total_volumes(bb, book)
                _cpv = _get_chapters_per_volume(bb, book)
            except Exception:
                _tv, _cpv = 1, 50
            validator = PostGenValidator(_tv, _cpv, max_retries=1)

            content = ''
            cur_messages = messages
            max_attempts = 4  # 首次 + 最多 3 次重试
            _EMPTY_FALLBACK_LEN = 30  # 兜底阈值（人物/文风/伏笔这类短内容维度也能触发）
            _last_stream_err = ''
            _last_fc_truncated = False  # 上次失败是否为截断类（思考耗尽/输出被切）→ 重试直接顶满 max_tokens
            _sections_retried = False   # 缺节自动补写是否已触发过（只补一次，防空转）
            for _attempt in range(max_attempts):
                yield SSE_HEARTBEAT_COMMENT  # SSE 保活：防 Render 30s idle timeout
                if _attempt >= 1:
                    # 【聊天截断修复】重试发 attempt_reset 让前端清空上一轮半截内容（旧实现两轮 delta 拼一条消息→像被截断）
                    yield sse({'type': 'meta', 'kind': 'attempt_reset',
                               'info': {'attempt': _attempt + 1, 'max_attempts': max_attempts,
                                        'reason': _last_stream_err[:160] if _last_stream_err else ''}})
                full = []
                _temp = 0.7 + min(_attempt * 0.08, 0.2)  # 初始 0.7 起步，重试微增
                if _attempt == 0:
                    _max_tok = max_tok
                elif _last_fc_truncated:
                    _max_tok = 27000  # 截断类失败（思考耗尽 token）重试直接顶满，渐进 1.5x 不够思考消耗
                else:
                    _max_tok = min(int(max_tok * (1.5 if _attempt == 1 else 2)), 27000)
                # 第 2 次起进"精简模式"：截断过长 system/铁律，防 prompt 溢出 → 模型拒答吐空
                _msgs_for_this_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key) if _attempt >= 1 else cur_messages
                try:
                    # 【聊天终止修复】单次流失败只记录原因并降级重试，不再炸掉整条 SSE（旧实现直接
                    # 进外层 except → error 帧 → 前端 removeEmptyAi 消息戛然而止）
                    for chunk in gw_stream_with_hb(gw, _msgs_for_this_call, temperature=_temp, max_tokens=_max_tok):
                        if chunk is HEARTBEAT:
                            yield SSE_HEARTBEAT_COMMENT
                            continue
                        full.append(chunk)
                        yield sse({'type': 'delta', 'content': chunk})
                except GeneratorExit:
                    # 客户端断开：同步抢救本轮已流出的部分内容再退出（禁止 yield）
                    _save_partial_on_disconnect(session, f'智驾生成·{spec["label"]}',
                                                requirement or suggestion[:60], ''.join(full))
                    raise
                except Exception as se:
                    _last_stream_err = str(se)[:300]
                    # 【智驾生成错误修复】确定性失败（key 无效/额度耗尽/用户取消）重试无意义，
                    # 直接跳出循环走下方空内容 error 帧立即报错（旧实现 401 也傻等 4 轮超时）
                    from llm_gateway import FailureClass
                    _fc = getattr(se, 'failure_class', None)
                    _last_fc_truncated = _fc == FailureClass.FORMAT_ERROR  # 思考耗尽/输出被切 → 重试顶满 max_tokens
                    if _fc in (FailureClass.AUTHENTICATION, FailureClass.QUOTA, FailureClass.CANCELLED):
                        break
                    continue
                raw_joined = ''.join(full)
                # EMPTY_OUTPUT 兜底1：先全局剥离 think 标签（R1 系列模型最多的问题）
                raw_no_think = _strip_think_tags(raw_joined)
                cleaned = raw_no_think.strip()
                if dim_key == 'timeline':
                    # timeline：仅 fence 清理 + JSON 规整
                    fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
                    if fence:
                        cleaned = fence.group(1).strip()
                    try:
                        parsed = json.loads(cleaned)
                        if isinstance(parsed, dict):
                            for k in ['volumes', 'data', 'result', 'items', 'list']:
                                if isinstance(parsed.get(k), list):
                                    parsed = parsed[k]
                                    break
                        if isinstance(parsed, list):
                            cleaned = json.dumps(parsed, ensure_ascii=False, indent=2)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                else:
                    cleaned = _clean_text_to_plain(cleaned)
                    # 人物维度：JSON 数组转自然语言
                    if dim_key == 'character_profiles' and cleaned.lstrip().startswith('['):
                        cleaned = _character_profiles_to_text(cleaned)
                # EMPTY_OUTPUT 兜底2：清理后仍空但 raw 有字数 → 用原始仅去 fence/html 的版本（宁脏勿空）
                if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                    fallback = raw_no_think.strip()
                    m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                    if m:
                        fallback = m.group(1).strip()
                    fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                    fallback = re.sub(r'</?p>', '\n', fallback)
                    if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                        cleaned = fallback.strip()
                # EMPTY_OUTPUT 兜底3：客套/拒答检测（"好的没问题"这种也当空）
                if cleaned and _is_refusal_or_fluff(cleaned):
                    cleaned = ''
                content = cleaned
                issues = validator.validate(dim_key, content, raw_length_hint=len((raw_no_think or '').strip()))
                _log_validation_issues(bb, dim_key, issues)
                # 【增强·缺节自动补写】warn 级缺节（分节命中 <60%，常因截断/跳节）也触发一次
                # 带缺失清单的重试（旧实现只重试 error 级 → 半成品直接交付，用户看到"生成了一部分"）
                _sec_retry = (not _sections_retried and _attempt < max_attempts - 1
                              and bool(validator.sections_missing_issues(issues)))
                if _sec_retry:
                    _sections_retried = True  # 缺节补写只触发一次，避免空转烧 token
                if (not validator.should_retry(issues) and not _sec_retry) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                # 需要重试：带错误反馈重新生成（error 级优先，其次缺节补写清单）
                retry_hint = validator.build_retry_hint(issues) or validator.build_sections_retry_hint(issues)
                yield sse({'type': 'meta', 'kind': 'validation_retry',
                          'info': {'attempt': _attempt + 1,
                                   'max_attempts': max_attempts,
                                   'issues': validator.to_meta(issues)}})
                cur_messages = messages + [
                    {'role': 'assistant', 'content': content or raw_no_think},
                    {'role': 'user', 'content': retry_hint}
                ]
            else:
                validation_meta = []
            # 【空回复兜底】多轮尝试后仍无内容：显式 error 帧（替代静默 done+空卡片）
            if not content or not content.strip():
                _err = _last_stream_err or 'LLM 多次尝试后仍返回空内容，请检查模型配置/额度后重试'
                yield sse({'type': 'error', 'error': f'生成失败：{_err}'})
                return
            extra_cards = _parse_card_markers(content)  # 世界观会额外产出地图卡片
            if extra_cards:
                clean_content = _clean_text_to_plain(_strip_card_markers(content)) if dim_key != 'timeline' else _strip_card_markers(content)
            else:
                clean_content = content
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': spec['card'],
                'title': f'{spec["label"]}（AI智驾生成）',
                'content': clean_content,
                'target': _CARD_TARGET.get(spec['card'], spec['label']),
            }
            card_meta = {'validation': validation_meta} if validation_meta else None  # 自检结果随卡片下发
            yield sse({'type': 'card', 'card': card, 'session_id': session_id, 'meta': card_meta})
            for ec in extra_cards:
                ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                if ec.get('title'):
                    ec['title'] = _clean_text_to_plain(ec['title'])
                yield sse({'type': 'card', 'card': ec, 'session_id': session_id})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'生成{spec["label"]}：{requirement or suggestion[:50]}'})
            history.append({'role': 'assistant', 'content': clean_content,
                            'cards': [{**c, 'status': 'pending'} for c in [card] + extra_cards]})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


@chat_collab_bp.route('/api/ai/smart/dim-edit', methods=['POST'])
def smart_dim_edit():
    """AI智驾·设定：单独维度AI修改（流式，产卡片）。

    body: { book_id, dimension, current_content, edit_request, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error
    """
    from app import db, AISession, Book, BookBible
    from llm_gateway import LLMGateway, get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    dim_key = data.get('dimension')
    current_content = (data.get('current_content') or '').strip()
    edit_request = (data.get('edit_request') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids') or []
    session_id = data.get('session_id')

    if not book_id or dim_key not in _DIM_KEY_TO_SPEC or not edit_request:
        return jsonify({'error': '参数无效：需要 book_id/dimension/edit_request'}), 400

    spec = _DIM_KEY_TO_SPEC[dim_key]
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # 如果未传 current_content，从 bb 读取
    if not current_content and bb:
        current_content = (getattr(bb, spec['field'], '') or '').strip()

    # 人物维度：current_content 是 JSON 数组时转自然语言，避免 AI 模仿 JSON 格式
    if dim_key == 'character_profiles' and current_content.startswith('['):
        current_content = _character_profiles_to_text(current_content)

    ctx, _ = _build_dim_context(book, bb, dim_key, with_self=False)

    # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）
    skill_note = build_conception_rules(skill_pack_ids, mode='agent')

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting',
                                              title=f'{spec["label"]}修改')
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    # 人物维度额外约束：禁止输出 JSON/代码符号，必须用自然语言按字段分行
    character_extra = ''
    if dim_key == 'character_profiles':
        character_extra = '\n\n【人物维度输出格式·绝对铁律】绝对禁止 JSON 符号 [ ] { } " : 和英文字段名。必须用纯中文，按“姓名：xxx\\n身份：xxx\\n性格：xxx\\n动机：xxx\\n背景：xxx\\n关系：xxx\\n能力：xxx”分行输出，每字段至少30字。'

    # 【P0修复】timeline 维度保持按卷 JSON 数组格式，不附加纯文本规则
    timeline_edit_extra = ''
    if dim_key == 'timeline':
        timeline_edit_extra = '\n\n【剧情维度修改铁律】当前维度原文是按卷的 JSON 数组（每卷含 volume_index/volume/main_plot/core_conflict/ending_hook/nodes 等字段）。修改后必须保持相同的 JSON 数组格式输出，不要输出纯文本或 Markdown。只调整修改意见涉及的卷或字段，其余卷保持原样。直接输出 JSON 数组，不要包裹代码块，不要解释。'

    # 预拼接块（Python 3.11 禁止 f-string 表达式内含反斜杠，故先算好再引用）
    _skill_note_block = ("【技能包指引】\n" + skill_note) if skill_note else ""

    sys_prompt = f"""你是资深网文创作智驾。请根据作者的修改意见，修订《{book.title or "未命名"}》的“{spec['label']}”维度内容。

【其他维度参考】
{ctx or "（暂无）"}

【当前维度原文】
{current_content or "（暂无）"}

【作者修改意见】
{edit_request}

{_skill_note_block}
{character_extra}{timeline_edit_extra}

请直接输出修订后的完整内容（保留原文中合理的部分，按修改意见调整），不要寒暄，不要解释，不要加 Markdown 标题。

【修改铁律】
1. 这是对“已有内容”的局部修订，不是重新创作；必须保留原文的整体结构、核心设定、人物/地点/势力名称及关键事件；
2. 仅针对修改意见中明确提到的点进行调整，未提及的部分尽量保持原样；
3. 如果修改意见与原文冲突，优先执行修改意见，但需在同一框架内微调，禁止另起炉灶生成全新世界观/大纲/人物；
4. 输出必须是可直接覆盖原维度的完整修订正文。

{TIMELINE_NARRATIVE_RULES if dim_key == 'timeline' else PLAIN_TEXT_LAYOUT_RULES}"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请修订{spec["label"]}内容'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        from .post_gen_validator import PostGenValidator
        try:
            from app import _get_total_volumes, _get_chapters_per_volume
            _tv = _get_total_volumes(bb, book)
            _cpv = _get_chapters_per_volume(bb, book)
        except Exception:
            _tv, _cpv = 1, 50
        validator = PostGenValidator(_tv, _cpv, max_retries=1)

        content = ''
        cur_messages = messages
        validation_meta = []
        try:
            max_tok = _dim_max_tokens(dim_key)  # 旧值 2000 会截断设定/世界观，见 _DIM_MAX_TOKENS 注释
            max_attempts = 4
            _EMPTY_FALLBACK_LEN = 30
            _sections_retried = False  # 缺节自动补写只触发一次（同 smart/generate）
            for _attempt in range(max_attempts):
                # === SSE 保活：每次 LLM 调用前（含重试）先发 1 帧心跳，占住连接防 Render 30s idle timeout ===
                yield SSE_HEARTBEAT_COMMENT
                if _attempt >= 1:
                    yield sse({'type': 'delta', 'content': f'\n（第{_attempt + 1}次尝试…）\n'})
                full = []
                _temp = 0.7 + min(_attempt * 0.08, 0.2)
                _max_tok = max_tok
                if _attempt == 1:
                    _max_tok = min(int(max_tok * 1.5), 27000)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), 27000)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                    if chunk is HEARTBEAT:
                        yield SSE_HEARTBEAT_COMMENT
                        continue
                    full.append(chunk)
                    yield sse({'type': 'delta', 'content': chunk})
                raw_joined = ''.join(full)
                # Step1：剥离 think 标签
                raw_no_think = _strip_think_tags(raw_joined)
                cleaned = raw_no_think.strip()
                # timeline 维度：不清理 JSON 结构
                if dim_key == 'timeline':
                    fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
                    if fence:
                        cleaned = fence.group(1).strip()
                    try:
                        parsed = json.loads(cleaned)
                        if isinstance(parsed, dict):
                            for k in ['volumes', 'data', 'result', 'items', 'list']:
                                if isinstance(parsed.get(k), list):
                                    parsed = parsed[k]
                                    break
                        if isinstance(parsed, list):
                            cleaned = json.dumps(parsed, ensure_ascii=False, indent=2)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                else:
                    cleaned = _clean_text_to_plain(cleaned)
                    # 人物维度兜底：若 AI 仍输出 JSON 数组，转成自然语言
                    if dim_key == 'character_profiles' and cleaned.lstrip().startswith('['):
                        cleaned = _character_profiles_to_text(cleaned)
                # EMPTY_OUTPUT 兜底：清理后空但 raw(去think后) ≥30 字 → 保守清理
                if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                    fallback = raw_no_think.strip()
                    m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                    if m:
                        fallback = m.group(1).strip()
                    fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                    fallback = re.sub(r'</?p>', '\n', fallback)
                    if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                        cleaned = fallback.strip()
                # 客套/拒答检测
                if cleaned and _is_refusal_or_fluff(cleaned):
                    cleaned = ''
                content = cleaned
                # 自检
                issues = validator.validate(dim_key, content, raw_length_hint=len((raw_no_think or '').strip()))
                _log_validation_issues(bb, dim_key, issues)
                # 【增强·缺节自动补写】同 smart/generate：warn 级缺节也带清单重试一次（防截断/跳节半成品交付）
                _sec_retry = (not _sections_retried and _attempt < max_attempts - 1
                              and bool(validator.sections_missing_issues(issues)))
                if _sec_retry:
                    _sections_retried = True
                if (not validator.should_retry(issues) and not _sec_retry) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                retry_hint = validator.build_retry_hint(issues) or validator.build_sections_retry_hint(issues)
                yield sse({'type': 'meta', 'kind': 'validation_retry',
                          'info': {'dim': dim_key, 'attempt': _attempt + 1,
                                   'max_attempts': max_attempts,
                                   'issues': validator.to_meta(issues)}})
                cur_messages = messages + [
                    {'role': 'assistant', 'content': content or raw_no_think},
                    {'role': 'user', 'content': retry_hint}
                ]
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': spec['card'],
                'title': f'{spec["label"]}（AI智驾修订）',
                'content': content,
                'target': _CARD_TARGET.get(spec['card'], spec['label']),
            }
            card_meta = {'validation': validation_meta} if validation_meta else None
            yield sse({'type': 'card', 'card': card, 'session_id': session_id, 'meta': card_meta})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'修订{spec["label"]}：{edit_request}'})
            history.append({'role': 'assistant', 'content': content,
                            'cards': [{**card, 'status': 'pending'}]})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


@chat_collab_bp.route('/api/ai/smart/batch', methods=['POST'])
def smart_batch():
    """AI智驾·设定·批量：一次性生成多个维度（流式，每维度产一张卡）。

    body: { book_id, dimensions: [dim_key, ...], requirement?, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error
    """
    from app import db, AISession, Book, BookBible
    from llm_gateway import LLMGateway, get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    dims = data.get('dimensions') or []
    requirement = (data.get('requirement') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids') or []
    session_id = data.get('session_id')

    if not book_id or not dims:
        return jsonify({'error': '参数无效：需要 book_id/dimensions'}), 400

    # 过滤合法维度
    dims = [d for d in dims if d in _DIM_KEY_TO_SPEC]
    if not dims:
        return jsonify({'error': '无有效维度'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）
    skill_note = build_conception_rules(skill_pack_ids, mode='agent')

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting',
                                              title=f'批量生成{len(dims)}维度')
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        generated = {}
        try:
            # 【P0改进】初始化自检器（批量生成共用）
            from .post_gen_validator import PostGenValidator
            try:
                from app import _get_total_volumes, _get_chapters_per_volume
                _tv = _get_total_volumes(bb, book)
                _cpv = _get_chapters_per_volume(bb, book)
            except Exception:
                _tv, _cpv = 1, 50
            validator = PostGenValidator(_tv, _cpv, max_retries=1)

            for dim_key in dims:
                spec = _DIM_KEY_TO_SPEC[dim_key]
                label = spec['label']
                yield sse({'type': 'delta', 'content': f'\n\n正在生成【{label}】…\n\n'})

                # 【P1改进】依赖检查：前置维度未完善时下发提示
                try:
                    # 批量场景：已批量生成的维度也算"已完善"
                    tmp_bb = bb
                    # 把 generated 临时合并到 bb 副本用于依赖检查
                    if generated:
                        from app import BookBible as _BB
                        tmp_bb = _BB(book_id=book_id)
                        for k_field in ['concept','key_rules','worldbuilding','plot_design','timeline','character_profiles','foreshadowing','locations','style_guide']:
                            v = (getattr(bb, k_field, '') or '') if bb else ''
                            if k_field in generated:
                                v = generated[k_field]
                            setattr(tmp_bb, k_field, v)
                    readiness = check_dim_readiness(tmp_bb, dim_key)
                    if readiness.get('warning'):
                        yield sse({'type': 'meta', 'kind': 'dependency_warning', 'info': readiness})
                except Exception:
                    pass

                ctx_parts = []
                for k, v in generated.items():
                    ctx_parts.append(f'【{_DIM_KEY_TO_SPEC[k]["label"]}】\n{v[:500]}')
                ctx_block = '\n\n'.join(ctx_parts) if ctx_parts else '（暂无）'

                existing = ''
                if bb:
                    existing = (getattr(bb, spec['field'], '') or '').strip()

                # 注入核心创作参数铁律（批量维度也要遵守总卷数/题材/风格，尤其是大纲/剧情维度）
                core_iron = _core_params_iron_block(bb, book)
                # 【P0修复】timeline 维度输出按卷 JSON 数组，跳过纯文本规则与清理
                _is_tl = (dim_key == 'timeline')
                sys_prompt = (
                    f'你是资深网文创作智驾。请为《{book.title or "未命名"}》生成“{label}”设定。'
                    f'\n\n{core_iron}'
                    f'\n\n【已生成维度参考】\n{ctx_block}'
                    f'\n\n【作者补充要求】\n{requirement or "无"}'
                    f'{(chr(10) + chr(10) + "【技能包指引】" + chr(10) + skill_note) if skill_note else ""}'
                )
                if _is_tl:
                    sys_prompt += (
                        '\n\n【剧情维度输出铁律】必须输出按卷的 JSON 数组（不要代码块包裹），'
                        '每卷含 volume_id/volume/volume_index/act/main_plot/core_conflict/ending_hook/nodes 字段。'
                        '直接输出 JSON 数组，不要解释。'
                        f'\n\n{TIMELINE_NARRATIVE_RULES}'
                    )
                else:
                    sys_prompt += (
                        '\n\n请直接输出该维度的设定内容（300-600字），不要寒暄，不要解释。'
                        f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
                    )
                if existing:
                    sys_prompt += f'\n\n【已有内容（可补充完善，不要简单重复）】\n{existing[:400]}'

                messages = [{'role': 'system', 'content': sys_prompt},
                            {'role': 'user', 'content': f'请生成{label}'}]
                content = ''
                cur_messages = messages
                validation_meta = []
                try:
                    max_tok = _dim_max_tokens(dim_key)  # 旧值 1500 连校验器字数下限都装不下（必截断）
                    # 自检重试循环（首次 + 最多 3 次重试，think 剥离 + 客套检测 + 低阈值兜底）
                    max_attempts = 4
                    _EMPTY_FALLBACK_LEN = 30
                    _sections_retried = False  # 缺节自动补写只触发一次（同 smart/generate）
                    for _attempt in range(max_attempts):
                        # === SSE 保活：每次 LLM 调用前（含重试）先发 1 帧心跳，占住连接防 Render 30s idle timeout ===
                        yield SSE_HEARTBEAT_COMMENT
                        if _attempt >= 1:
                            yield sse({'type': 'delta', 'content': f'\n（第{_attempt + 1}次尝试…）\n'})
                        raw_chunks = []
                        # 初始温度 0.7（原 0.8 偏高），重试时微增
                        _temp = 0.7 + min(_attempt * 0.08, 0.2)
                        _max_tok = max_tok
                        if _attempt == 1:
                            _max_tok = min(int(max_tok * 1.5), 27000)
                        elif _attempt >= 2:
                            _max_tok = min(int(max_tok * 2), 27000)
                        _msgs_call = cur_messages
                        if _attempt >= 1:
                            _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                        for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                            if chunk is HEARTBEAT:
                                yield SSE_HEARTBEAT_COMMENT
                                continue
                            raw_chunks.append(chunk)
                            yield sse({'type': 'delta', 'content': chunk})
                        raw_joined = ''.join(raw_chunks)
                        # Step1：剥离 think 标签（R1 系列模型最大坑）
                        raw_no_think = _strip_think_tags(raw_joined)
                        cleaned = raw_no_think.strip()
                        # 清理/规范化
                        if _is_tl:
                            fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
                            if fence:
                                cleaned = fence.group(1).strip()
                            try:
                                parsed = json.loads(cleaned)
                                if isinstance(parsed, dict):
                                    for k in ['volumes', 'data', 'result', 'items', 'list']:
                                        if isinstance(parsed.get(k), list):
                                            parsed = parsed[k]
                                            break
                                if isinstance(parsed, list):
                                    cleaned = json.dumps(parsed, ensure_ascii=False, indent=2)
                            except (json.JSONDecodeError, ValueError, TypeError):
                                pass
                        else:
                            cleaned = _clean_text_to_plain(cleaned)
                        # EMPTY_OUTPUT 兜底：清理后空但 raw(去think后) ≥30 字 → 保守清理仅去 fence/html
                        if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                            fallback = raw_no_think.strip()
                            fence_m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                            if fence_m:
                                fallback = fence_m.group(1).strip()
                            fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                            fallback = re.sub(r'</?p>', '\n', fallback)
                            if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                                cleaned = fallback.strip()
                        # 客套/拒答检测
                        if cleaned and _is_refusal_or_fluff(cleaned):
                            cleaned = ''
                        content = cleaned
                        # 自检（用去think后长度做提示）
                        issues = validator.validate(dim_key, content, raw_length_hint=len((raw_no_think or '').strip()))
                        _log_validation_issues(bb, dim_key, issues)
                        # 【增强·缺节自动补写】同 smart/generate：warn 级缺节也带清单重试一次（防截断/跳节半成品交付）
                        _sec_retry = (not _sections_retried and _attempt < max_attempts - 1
                                      and bool(validator.sections_missing_issues(issues)))
                        if _sec_retry:
                            _sections_retried = True
                        if (not validator.should_retry(issues) and not _sec_retry) or _attempt >= max_attempts - 1:
                            validation_meta = validator.to_meta(issues)
                            break
                        retry_hint = validator.build_retry_hint(issues) or validator.build_sections_retry_hint(issues)
                        yield sse({'type': 'meta', 'kind': 'validation_retry',
                                  'info': {'dim': dim_key, 'attempt': _attempt + 1,
                                           'max_attempts': max_attempts,
                                           'issues': validator.to_meta(issues)}})
                        cur_messages = messages + [
                            {'role': 'assistant', 'content': content or raw_no_think},
                            {'role': 'user', 'content': retry_hint}
                        ]
                except Exception as e:
                    yield sse({'type': 'error', 'error': f'{label}生成失败：{e}'})
                    continue

                generated[dim_key] = content
                card = {
                    'id': str(uuid.uuid4())[:8],
                    'type': spec['card'],
                    'title': f'{label}（AI智驾生成）',
                    'content': content,
                    'target': _CARD_TARGET.get(spec['card'], label),
                }
                card_meta = {'validation': validation_meta} if validation_meta else None
                yield sse({'type': 'card', 'card': card, 'session_id': session_id, 'meta': card_meta})

            # 持久化
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'批量生成{len(dims)}维度：{requirement or "默认"}'})
            cards = []
            for dim_key in dims:
                c = generated.get(dim_key)
                if c:
                    spec = _DIM_KEY_TO_SPEC[dim_key]
                    cards.append({
                        'id': str(uuid.uuid4())[:8],
                        'type': spec['card'],
                        'title': f'{spec["label"]}（AI智驾生成）',
                        'content': c,
                        'target': _CARD_TARGET.get(spec['card'], spec['label']),
                        'status': 'pending',
                    })
            history.append({'role': 'assistant', 'content': f'已生成 {len(cards)} 个维度', 'cards': cards})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


# ----------------------------------------------------------------------------
# 正文Tab：融合章节AI创作（自动定位最新章节，续写/润色）
# 复用 /api/ai/chat/smart/action 的 continue/polish 动作，前端自动传入 target_chapter_num
# ----------------------------------------------------------------------------

@chat_collab_bp.route('/api/ai/smart/latest-chapter', methods=['GET'])
def smart_latest_chapter():
    """获取最新章节信息（供正文Tab自动定位）。

    返回: { latest: {id, title, order_index, word_count, status}|null, next_chapter_num }
    章节号统一口径：优先 parse_chapter_number(title)，与写作/修改/去AI一致。
    """
    info = _get_latest_chapter_info(request.args.get('book_id') or '')
    latest = info['latest_chapter']
    if latest:
        return jsonify({
            'latest': {
                'id': latest.id,
                'title': latest.title,
                'order_index': latest.order_index,
                'word_count': latest.word_count or 0,
                'status': latest.status,
            },
            'next_chapter_num': info['next_num'],
        })
    return jsonify({'latest': None, 'next_chapter_num': 1})


# ----------------------------------------------------------------------------
# 去AITab：拉取去AI味技能包 + 选章节去AI味
# ----------------------------------------------------------------------------

@chat_collab_bp.route('/api/ai/smart/deai-packs', methods=['GET'])
def smart_deai_packs():
    """拉取去AI味技能包列表（review 类，便于前端默认勾选）。

    返回: { packs: [{id, name, description, icon, priority}] }
    """
    from app import SkillPack
    packs = SkillPack.query.filter_by(category='review').order_by(SkillPack.priority.asc()).all()
    return jsonify({'packs': [
        {'id': p.id, 'name': p.name, 'description': p.description or '',
         'icon': p.icon or '📦', 'priority': p.priority}
        for p in packs
    ]})


@chat_collab_bp.route('/api/ai/smart/deai', methods=['POST'])
def smart_deai():
    """AI智驾·去AI：对指定章节正文去AI味（流式，产 SAVE_CHAPTER 卡）。

    body: { book_id, chapter_id, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error
    """
    from app import db, AISession, Book, BookBible, Chapter
    from llm_gateway import LLMGateway, get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    chapter_id = data.get('chapter_id')
    skill_pack_ids = data.get('skill_pack_ids') or []
    session_id = data.get('session_id')

    if not book_id or not chapter_id:
        return jsonify({'error': '缺少 book_id 或 chapter_id'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    chapter = Chapter.query.get(chapter_id)
    if not chapter or chapter.book_id != book_id:
        return jsonify({'error': '章节不存在'}), 404

    raw_content = (chapter.content or '').strip()
    if not raw_content:
        return jsonify({'error': '该章节无正文，无法去AI味'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ====== 核心创作参数铁律 + 越界硬拦截（去AI味也要守边界） ======
    from app import _get_total_volumes, _get_chapters_per_volume, parse_chapter_number
    tv = _get_total_volumes(bb, book)
    cpv = _get_chapters_per_volume(bb, book)
    max_chapters = tv * cpv
    core_iron = _core_params_iron_block(bb, book)
    # 越界硬拦截：去AI味的章节号也不能超过总章数上限
    ch_num = parse_chapter_number(chapter.title or '')
    if ch_num and ch_num > max_chapters:
        return jsonify({'error': (f'【核心参数越界拦截】全书设定总卷数 {tv} 卷 × 每卷 {cpv} 章 = 总章数上限 {max_chapters} 章，'
                                f'《{chapter.title}》已超出上限。若需要继续，请先到作品基本信息中调大总卷数。')}), 400

    # 去AI/审稿阶段·专属规则（通用核心+行文规范+完整去AI手册+review技能包）
    review_rules = build_review_rules(
        skill_pack_ids, mode='agent',
        prompt_keys_filter=['tomato_deai', 'de_ai_flavor', 'polish', 'consistency_check'],
    )

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_deai',
                                              title=f'去AI味·{chapter.title}')
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    # 设定上下文（用于保持人物口吻一致）
    bible_ctx = ''
    if bb:
        parts = []
        # 全维度完整注入（去AI味也要参考全部设定，避免改写时偏离设定）
        for d in SMART_DIMENSIONS:
            v = (getattr(bb, d['field'], '') or '').strip()
            if v:
                if d['key'] == 'character_profiles' and v.startswith('['):
                    v = _character_profiles_to_text(v)
                parts.append(f'【{d["label"]}】\n{v}')
        bible_ctx = '\n\n'.join(parts)

    # 统一纯正文字数口径（与章节保存 API count_words 一致：中文+中文标点+英文单词+数字串）
    from app import count_words
    orig_wc = count_words(raw_content)
    sys_prompt = f"""你是番茄去AI味审查员。请对以下章节正文做去AI味审校，按规则修改后只输出修改后的正文。

{core_iron}

{review_rules}

【全文设定参考】
{bible_ctx or "（暂无）"}

【硬性约束】
1. 只输出纯正文，不要输出章节标题（标题由系统保留，不要重复输出）。
2. 修改后纯正文字数与原文 {orig_wc} 字相近（±10%），保留原章节的剧情走向和钩子，只改文风不改剧情。
   字数统计口径：中文字符+中文标点（全角标点计入，半角标点不计入，英文按单词、数字按串）。请用全角中文标点。
3. 不要加 Markdown 代码块，不要解释，不要在文末附加字数统计。

{PLAIN_TEXT_LAYOUT_RULES}"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请审校以下章节正文：\n\n{raw_content}'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        from .post_gen_validator import PostGenValidator
        # 去AI味走通用校验（只卡 EMPTY_OUTPUT，不做其他维度校验），用 tv=1, cpv=50 占位即可
        validator = PostGenValidator(1, 50, max_retries=1)
        _EMPTY_FALLBACK_LEN = 30  # 章节正文一般很长，30字兜底足够
        content = ''
        body_content = ''
        cur_messages = messages
        validation_meta = []
        try:
            max_tok = 4096
            max_attempts = 4
            yield sse({'type': 'delta', 'content': f'正在为《{chapter.title}》去AI味…\n\n'})
            for _attempt in range(max_attempts):
                # === SSE 保活：每次 LLM 调用前（含重试）先发 1 帧心跳，占住连接防 Render 30s idle timeout ===
                yield SSE_HEARTBEAT_COMMENT
                if _attempt >= 1:
                    yield sse({'type': 'delta', 'content': f'\n（第{_attempt + 1}次尝试…）\n'})
                full = []
                # 去AI味温度保持偏低：0.5 起步（忠实原文），重试微增到 0.58/0.66，避免高温乱改
                _temp = 0.5 + min(_attempt * 0.08, 0.2)
                _max_tok = max_tok
                if _attempt == 1:
                    _max_tok = min(int(max_tok * 1.5), 27000)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), 27000)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim='chapter_deai')
                for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                    if chunk is HEARTBEAT:
                        yield SSE_HEARTBEAT_COMMENT
                        continue
                    full.append(chunk)
                    yield sse({'type': 'delta', 'content': chunk})
                raw_joined = ''.join(full)
                # Step1：剥离 think 标签（R1 模型常见前置坑）
                raw_no_think = _strip_think_tags(raw_joined)
                cleaned = raw_no_think.strip()
                # 平台级纯文本清理（统一去 * 和 #）
                cleaned = _clean_text_to_plain(cleaned)
                # EMPTY_OUTPUT 兜底：清理后空但 raw(去think后) ≥30 字 → 保守清理仅去 fence/html
                if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                    fallback = raw_no_think.strip()
                    m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                    if m:
                        fallback = m.group(1).strip()
                    fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                    fallback = re.sub(r'</?p>', '\n', fallback)
                    if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                        cleaned = fallback.strip()
                # 客套/拒答检测（去AI味也可能碰到模型道歉"我无法帮你做去AI味"）
                if cleaned and _is_refusal_or_fluff(cleaned):
                    cleaned = ''
                content = cleaned
                # 防御性剥离标题行：保证 card.content 为纯正文
                _, body_content = _strip_chapter_title(content, fallback_title=chapter.title or '')
                # 自检：用 body_content（去掉标题后的正文）做校验，提示长度用去 think 后长度
                issues = validator.validate('chapter_deai', body_content, raw_length_hint=len((raw_no_think or '').strip()))
                _log_validation_issues(bb, 'deai', issues)
                if not validator.should_retry(issues) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                retry_hint = validator.build_retry_hint(issues)
                # 去AI味失败重试提示更具体：不能空答，必须输出正文
                retry_hint += '\n额外要求：必须输出完整的去AI味后正文，不能空答，不能道歉，不能只输出"好的/收到"这类客套话。'
                yield sse({'type': 'meta', 'kind': 'validation_retry',
                          'info': {'attempt': _attempt + 1,
                                   'max_attempts': max_attempts,
                                   'issues': validator.to_meta(issues)}})
                cur_messages = messages + [
                    {'role': 'assistant', 'content': content or raw_no_think},
                    {'role': 'user', 'content': retry_hint}
                ]
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': 'SAVE_CHAPTER',
                'title': chapter.title,
                'content': body_content,
                'target': '章节正文',
            }
            card_meta = {'chapter_id': chapter_id, 'replace': True, 'validation': validation_meta} if validation_meta else {'chapter_id': chapter_id, 'replace': True}
            yield sse({'type': 'card', 'card': card, 'session_id': session_id,
                       'meta': card_meta})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'去AI味：{chapter.title}'})
            history.append({'role': 'assistant', 'content': body_content,
                            'cards': [{**card, 'status': 'pending'}]})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


# ----------------------------------------------------------------------------
# B3：去AI Tab·风格对齐诊断（12维评分 + 范本并排 + 改点建议）
# ----------------------------------------------------------------------------

@chat_collab_bp.route('/api/ai/smart/style-align', methods=['POST'])
def smart_style_align():
    """AI智驾·风格对齐：对选中章节做 12 维风格对齐评分，返回评分、范本、改点建议。

    body: { book_id, chapter_id }
    返回: {
      chapter_title, chapter_num,
      dimensions: [{key, name, score, note}]（12 维，按分数升序，低分在前）,
      avg_score,
      bad_items: [{key, name, score, note, fix_suggestion}]（<60分的维度+具体改法）,
      style_pack: { id, name, content } 或 null（自动匹配 genre 对应的风格包）,
      book_genre,
      summary: '优/良/中/差' 四档文字总评
    }
    """
    from app import db, Book, BookBible, Chapter, parse_chapter_number

    data = request.json or {}
    book_id = data.get('book_id')
    chapter_id = data.get('chapter_id')

    if not book_id or not chapter_id:
        return jsonify({'error': '缺少 book_id 或 chapter_id'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    chapter = Chapter.query.get(chapter_id)
    if not chapter or chapter.book_id != book_id:
        return jsonify({'error': '章节不存在'}), 404

    raw_content = (chapter.content or '').strip()
    if not raw_content:
        return jsonify({'error': '该章节无正文，无法风格对齐诊断'}), 400

    # 1) 12 维风格对齐评分
    try:
        from post_write_validator import validate_chapter
        vr = validate_chapter(raw_content)
        dims_dict = vr.stats.get('style_alignment', {}) or {}
        avg = vr.stats.get('style_alignment_avg', 0) or 0
    except Exception as e:
        return jsonify({'error': f'风格评分失败：{e}'}), 500

    # 2) 按分数升序，低分在前（用户先看问题项）
    dims_list = []
    for k, d in dims_dict.items():
        dims_list.append(dict(key=k, name=d.get('name', k), score=int(d.get('score', 0)), note=d.get('note', '')))
    dims_list.sort(key=lambda x: x['score'])

    # 3) 低分维度 (<60) + 具体改法建议（从 12 维定义的正反例中直接摘改法）
    FIX_MAP = {
        'prompt_tag_mid': '改法：把对白提示语从句首挪到对白中或尾，后面再加一个小动作/小触感收尾。例：把"姜雪攥紧衣角说："你在名单上。""改成""你在名单上。"姜雪攥紧衣角。布料在指缝间发出细碎声响。"',
        'qa_disalign': '改法：避免 1:1 工整问答。用答非所问/半句话/沉默/打断/错位回。例："怕不怕？"→"……"→"问你话呢！"→"我怕你不敢来。"而不是"怕不怕？/怕。"',
        'dial_action_insert': '改法：每 3 句对白里至少插 1 句 POV 的小动作（眼皮一跳/攥紧衣角/指节发白）或小吐槽。A→B→A→B 机械轮换必须打断。',
        'side_story': '改法：删掉"隔壁矿友讨水"这种删了不影响主钩子后续的独立路人支线——哪怕只有 200 字也不行。本章所有场景绕回 ONE 主钩子。',
        'end_hook_link': '改法：结尾只留 1 个钩子，而且必须和本章冲突链的最后一环关联，严禁一次连铺 3 个未暗示新坑。',
        'long_paragraph': '改法：长段（>400 字）臃肿段拆成 2-4 段，按动作/对白/环境变化切开。冲突场景更要密集短段（80%-90% 段落一句话/一个动作各成一段）。',
        'para_uniformity': '改法：段长不要网格机械均匀。递进比较链写长句（3-4 层），动作收尾写 1-4 字短句。长短段交替，CV=0.5-1.0 为健康。',
        'comparison_chain': '改法：人物群像场景写"递进比较链长句→动作短句收尾"。X 比起 Y 像 Z → 比起 W 又差一截 → 比起 Q 差得远 → 最后比 T 小巫见大巫 → 1-4 字动作短句收。',
        'cliche_metaphor': '改法：删/换 8 大 AI 套话比喻词（宛如/犹如/恍若/宛若 + 大海/巨龙/深渊/星河）。每千字比喻≤3 个，而且必须贴合具体场景。',
        'correction_style': '改法：动作判断时加一层自我修正，避免机械直给。把"他脊梁骨发凉，很痛，心里发颤。"改成"不是痛。是一种凉，顺着脊梁往上爬，像谁把冰碴子一根根塞进骨缝里。"',
        'sense_detail': '改法：动作段做感官细节三叠（温度/气味/触感/声音选 2-3 叠）。把"他把残片藏好"改成"残片棱角咬进掌心，他没松手，把湿泥按上去，冷意混着血味从指缝里渗出来。"',
        'goal_closed_chain': '改法：动作链=小目标链，每 200-300 字必须有一个"目标→决策→决策被验证"的小三段闭环。绝对不允许漫无目的的动作流水账。',
    }
    bad_items = []
    for d in dims_list:
        if d['score'] < 60:
            bad_items.append({
                **d,
                'fix_suggestion': FIX_MAP.get(d['key'], '对照：文风黄金对白6式 + 文风黄金长短句4型 + ONE主钩子数字硬约束。'),
            })

    # 4) 匹配风格包内容（范本并排）
    sp_content = _get_enabled_style_pack(book)
    manifest = _load_style_manifest()
    sp_meta = None
    if sp_content:
        for p in (manifest.get('packs') or []):
            try:
                with open(os.path.join(_STYLE_PACK_ROOT, p['file']), 'r', encoding='utf-8') as f:
                    if f.read()[:100] == sp_content[:100]:
                        sp_meta = {'id': p['id'], 'name': p['name']}
                        break
            except Exception:
                pass
    style_pack = None
    if sp_content:
        style_pack = {
            'id': (sp_meta or {}).get('id') or 'auto_matched',
            'name': (sp_meta or {}).get('name') or '自动匹配风格包',
            'content': sp_content,
        }

    # 5) 总评四档
    if avg >= 85:
        grade = '优·风格对齐度良好，建议直接写作或做一次去AI味。'
    elif avg >= 70:
        grade = '良·部分维度待改进，建议对照下方低分项的改法，或点「开始去AI味」自动修复。'
    elif avg >= 55:
        grade = '中·风格偏差较大，建议先按改法手动调整一次，再走「去AI味」辅助。'
    else:
        grade = '差·风格矿道病严重（机械对白/独立支线/流水账动作），建议对照风格包范本，把本章拆解后按 ONE 主钩子 + 对白6式 + 短句4型 重写。'

    genre = (getattr(book, 'genre', None) or '').strip() or '（未设定题材）'
    ch_num = parse_chapter_number(chapter.title or '') or 0

    return jsonify({
        'chapter_title': chapter.title or f'第{ch_num}章',
        'chapter_num': ch_num,
        'dimensions': dims_list,
        'avg_score': round(float(avg), 1),
        'bad_items': bad_items,
        'style_pack': style_pack,
        'book_genre': genre,
        'summary': grade,
    })


# ----------------------------------------------------------------------------
# 校审Tab：防遗忘 + 一致性检查
# ----------------------------------------------------------------------------

@chat_collab_bp.route('/api/ai/smart/review', methods=['POST'])
def smart_review():
    """AI智驾·校审：防遗忘检查 / 一致性检查（按卷）。

    body: { book_id, mode: 'anti_forget'|'consistency', chapter_id?, volume_ids?, skill_pack_ids? }
    - anti_forget: 拉取动态文件报告 + 伏笔资料，按 volume_ids 指定卷检查（空=全书）
    - consistency: 对 chapter_id（或最新章节）所在卷做一致性检查，附伏笔/动态文件上下文
    返回: { mode, report: {...}, summary, health_score? }
    """
    from app import db, Book, BookBible, Chapter, AIConfig
    from llm_gateway import get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    mode = data.get('mode')
    chapter_id = data.get('chapter_id')
    volume_ids = data.get('volume_ids') or []
    skill_pack_ids = data.get('skill_pack_ids') or []

    if not book_id or mode not in ('anti_forget', 'consistency'):
        return jsonify({'error': '参数无效：需要 book_id/mode(anti_forget|consistency)'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定（BookBible 不存在）'}), 400

    cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400

    try:
        base_url, api_key, model = get_llm_config(app_module)
        recognition_model = cfg.get_model_for_task('recognition') if hasattr(cfg, 'get_model_for_task') else model
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400

    # ---------- 防遗忘检查（按卷，拉取动态文件+伏笔）----------
    if mode == 'anti_forget':
        try:
            from app import ai_anti_forget_check as _do_anti_forget
            from flask import current_app
            # 透传当前请求的 Authorization 头，让 @login_required 装饰器能拿到 token
            # 否则 test_request_context 不带认证头，会返回 401 "请先登录"
            auth_header = request.headers.get('Authorization', '')
            with current_app.test_request_context(
                f'/api/books/{book_id}/ai-anti-forget-check',
                method='POST',
                json={'scope': 'reports', 'volume_ids': volume_ids, 'skill_pack_ids': skill_pack_ids},
                headers={'Authorization': auth_header} if auth_header else None,
            ):
                resp = _do_anti_forget(book_id)
                if hasattr(resp, 'get_json'):
                    return jsonify(resp.get_json()), resp.status_code
                return resp
        except Exception as e:
            return jsonify({'error': f'防遗忘检查失败：{e}'}), 500

    # ---------- 一致性检查（按卷，附伏笔+动态文件上下文）----------
    # 若未指定 chapter_id，取最新章节
    if not chapter_id:
        latest = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .order_by(Chapter.order_index.desc()).first()
        if not latest:
            return jsonify({'error': '该书尚无章节，无法一致性检查'}), 400
        chapter_id = latest.id

    chapter = Chapter.query.get(chapter_id)
    if not chapter or chapter.book_id != book_id:
        return jsonify({'error': '章节不存在'}), 404

    # 若未指定 volume_ids，自动识别该章所属卷
    if not volume_ids and chapter.parent_id:
        volume_ids = [chapter.parent_id]

    draft_content = (chapter.content or '').strip()
    if not draft_content:
        return jsonify({'error': '该章节无正文'}), 400

    try:
        from app import _consistency_check, _collect_anti_forget_alerts
        # 拉取伏笔资料 + 动态文件上下文，拼入一致性检查上下文
        extra_context = ''
        try:
            alerts = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=6)
            if alerts:
                extra_context += f'\n\n【近期防遗忘诊断】\n{alerts}'
        except Exception:
            pass
        try:
            from app import DynamicReport
            q = DynamicReport.query.filter_by(book_id=book_id)
            if volume_ids:
                q = q.filter(DynamicReport.volume_id.in_([str(v) for v in volume_ids]))
            recent_reports = q.order_by(DynamicReport.chapter_start.desc()).limit(3).all()
            if recent_reports:
                rep_lines = []
                for r in recent_reports:
                    rep_lines.append(f'- {r.title or "动态报告"}（{r.chapter_start or "?"}-{r.chapter_end or "?"}）')
                extra_context += f'\n\n【动态文件报告】\n' + '\n'.join(rep_lines)
        except Exception:
            pass
        try:
            if getattr(bb, 'foreshadowing', None):
                fs = bb.foreshadowing.strip()
                if fs:
                    extra_context += f'\n\n【伏笔资料】\n{fs[:1500]}'
        except Exception:
            pass

        passed, issues = _consistency_check(
            book_id, bb, draft_content + extra_context, chapter.order_index,
            api_key, base_url, recognition_model,
            max_tokens=1200, chapter_plan=''
        )
        return jsonify({
            'mode': 'consistency',
            'chapter_id': chapter_id,
            'chapter_title': chapter.title,
            'order_index': chapter.order_index,
            'volume_ids': volume_ids,
            'passed': passed,
            'issues': issues,
            'summary': '✅ 一致性检查通过' if passed else f'⚠️ 发现问题：{issues}',
        })
    except Exception as e:
        return jsonify({'error': f'一致性检查失败：{e}'}), 500


@chat_collab_bp.route('/api/ai/smart/volumes', methods=['GET'])
def smart_volumes():
    """列出书的所有分卷（供校审Tab按卷选择）。

    返回: { volumes: [{id, title, order_index, chapter_count}] }
    """
    from app import Chapter
    book_id = request.args.get('book_id')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    vols = Chapter.query.filter_by(book_id=book_id, is_volume=True) \
        .order_by(Chapter.order_index.asc()).all()
    result = []
    for v in vols:
        cnt = Chapter.query.filter_by(book_id=book_id, parent_id=v.id, is_volume=False).count()
        result.append({
            'id': v.id, 'title': v.title,
            'order_index': v.order_index, 'chapter_count': cnt,
        })
    return jsonify({'volumes': result})


@chat_collab_bp.route('/api/ai/smart/chapters', methods=['GET'])
def smart_chapters():
    """列出书的所有章节（供去AI/校审Tab选择章节）。

    返回: { chapters: [{id, title, order_index, word_count, status}] }
    排序统一口径：按标题章节号升序（与写作/修改/去AI一致），无章节号者按 order_index 排后。
    """
    from app import Chapter, parse_chapter_number
    book_id = request.args.get('book_id')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    def _key(c):
        n = parse_chapter_number(c.title or '')
        return (0, n) if n is not None else (1, c.order_index)
    chs = sorted(chs, key=_key)
    return jsonify({'chapters': [
        {'id': c.id, 'title': c.title, 'order_index': c.order_index,
         'word_count': c.word_count or 0, 'status': c.status}
        for c in chs
    ]})


@chat_collab_bp.route('/api/ai/smart/chapter-replace', methods=['POST'])
def smart_chapter_replace():
    """用去AI味后的内容替换原章节正文（落地）。

    body: { book_id, chapter_id, content, session_id?, card_id? }
    返回: { ok, chapter_id, word_count }
    """
    from app import db, Chapter
    data = request.json or {}
    book_id = data.get('book_id')
    chapter_id = data.get('chapter_id')
    content = (data.get('content') or '').strip()
    session_id = data.get('session_id')
    card_id = data.get('card_id')

    if not book_id or not chapter_id or not content:
        return jsonify({'error': '参数无效'}), 400

    chapter = Chapter.query.get(chapter_id)
    if not chapter or chapter.book_id != book_id:
        return jsonify({'error': '章节不存在'}), 404

    # 防御性剥离标题行 + 统一 count_words 字数统计（与章节保存 API 口径一致）
    # 避免去AI后字数跳变
    from app import count_words
    _, body_content = _strip_chapter_title(content, fallback_title=chapter.title or '')
    chapter.content = body_content
    chapter.word_count = count_words(body_content)
    chapter.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    # 持久化去AI卡片状态为 adopted（避免重开聊天又提示替换）
    _persist_card_status(session_id, card_id, 'adopted', body_content)
    return jsonify({'ok': True, 'chapter_id': chapter_id, 'word_count': chapter.word_count})


# ============================================================================
# 防遗忘报告 → AI智驾 修正闭环
# 把防遗忘检查报告（violations/suggestions/pending_foreshadowing/narrative_debt）
# 打通给AI，让AI基于报告对设定维度内容生成修正方案，用户确认后落地。
# ============================================================================

# 可被修正的设定维度字段白名单（与 BookBible 维度字段一一对应）
_FIXABLE_DIM_FIELDS = {
    'concept': '核心构思', 'key_rules': '设定/规则', 'worldbuilding': '世界观',
    'character_profiles': '人物档案', 'plot_design': '大纲', 'timeline': '剧情时间线',
    'foreshadowing': '伏笔', 'locations': '地点', 'style_guide': '文风指南',
}


@chat_collab_bp.route('/api/ai/smart/fix-from-report', methods=['POST'])
def smart_fix_from_report():
    """基于防遗忘检查报告生成设定修正方案（不直接落地，返回给用户确认）。

    body: { book_id, report_id?, skill_pack_ids? }
    - report_id 为空时取最近一份防遗忘报告
    返回: { plan: [{ dim, label, issues:[..], action, new_content }], report_title, report_id }
    每个 plan 项对应一个维度的修正：列出该维度涉及的诊断问题、修正动作、修正后的完整设定内容
    """
    from app import db, Book, BookBible, Chapter, AIConfig
    from llm_gateway import get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    report_id = data.get('report_id')
    skill_pack_ids = data.get('skill_pack_ids') or []
    volume_ids = data.get('volume_ids') or []
    if not isinstance(volume_ids, list):
        volume_ids = []

    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 取指定报告（默认最近一份）
    target_report = None
    target_rec = None
    try:
        reports = json.loads(bb.anti_forget_reports) if bb.anti_forget_reports else []
    except Exception:
        reports = []
    if not isinstance(reports, list) or not reports:
        return jsonify({'error': '暂无防遗忘检查报告，请先执行检查'}), 400

    if report_id:
        for r in reports:
            if isinstance(r, dict) and r.get('id') == report_id:
                target_rec = r
                target_report = r.get('report') or {}
                break
        if not target_rec:
            return jsonify({'error': '未找到指定报告'}), 404
    else:
        # 取最近一份（按 checked_at 降序）
        def _ts(r):
            return r.get('checked_at', '') or ''
        recent = sorted([r for r in reports if isinstance(r, dict)], key=_ts, reverse=True)
        if not recent:
            return jsonify({'error': '暂无防遗忘检查报告'}), 400
        target_rec = recent[0]
        target_report = target_rec.get('report') or {}
        report_id = target_rec.get('id')

    # 构建诊断要点文本 + 各维度当前内容
    chapters = None
    if volume_ids:
        chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    diag_parts = []
    for key, label in [('violations', '一致性违规'), ('pending_foreshadowing', '待回收伏笔'),
                       ('narrative_debt', '叙事债务'), ('suggestions', '改进建议'),
                       ('character_cognition_issues', '角色认知问题')]:
        items = target_report.get(key) or []
        if not isinstance(items, list) or not items:
            continue
        # 诊断项截断：避免报告过大导致 LLM 上下文超限或响应极慢
        limit = 5 if key == 'violations' else 3
        kept_lines = []
        for it in items[:limit]:
            if isinstance(it, dict) and volume_ids and chapters:
                loc = it.get('location') or ''
                ch = _locate_chapter_by_location(loc, chapters)
                if ch and getattr(ch, 'parent_id', None) not in volume_ids:
                    continue
            if isinstance(it, dict):
                msg = it.get('desc') or it.get('message') or it.get('issue') or \
                      it.get('promise') or it.get('content') or it.get('suggestion') or str(it)
                fix = it.get('fix') or ''
                loc = it.get('location') or ''
                line = f'  - {msg[:120]}'
                if loc:
                    line += f'（位置：{loc[:60]}）'
                if fix:
                    line += f' 💡修正：{fix[:120]}'
                kept_lines.append(line)
            else:
                kept_lines.append(f'  - {str(it)[:120]}')
        if kept_lines:
            diag_parts.append(f'■ {label}（{len(items)}项）')
            diag_parts.extend(kept_lines)
    diag_text = '\n'.join(diag_parts) or '（报告无明确诊断项）'

    # 各维度当前内容（供AI参考，避免修正时凭空臆造）
    dim_now_parts = []
    for field, label in _FIXABLE_DIM_FIELDS.items():
        val = (getattr(bb, field, '') or '').strip()
        if val:
            if field == 'character_profiles' and val.startswith('['):
                try:
                    val = _character_profiles_to_text(val)
                except Exception:
                    pass
            dim_now_parts.append(f'【{label}·当前内容】\n{val[:500]}')
    dim_now_text = '\n\n'.join(dim_now_parts) or '（各维度暂无内容）'

    # 去AI/审稿阶段·专属规则（通用核心+行文规范+完整去AI手册+review技能包）
    skill_note = build_review_rules(skill_pack_ids, mode='agent')

    volume_section = ''
    if volume_ids:
        volume_section = f"\n\n【本次只处理以下卷】\n卷ID: {volume_ids}\n请只针对这些卷涉及的问题生成修正方案"

    system_prompt = f"""你是资深网文设定修正师。任务：基于防遗忘检查报告的诊断，对小说设定维度内容生成修正方案。

【防遗忘检查报告诊断要点】
{diag_text}{volume_section}

【各维度当前内容】
{dim_now_text}
{chr(10) + chr(10) + '【技能包指引】' + chr(10) + skill_note if skill_note else ''}

【你的任务】
针对报告诊断出的问题，逐维度生成“修正方案”。每个维度一个修正项，包含：
1. issues：该维度涉及的诊断问题（从上方诊断中归纳）
2. action：一句话说明怎么改（如：补全主角境界突破条件、修正时间线倒流）
3. new_content：修正后的完整设定内容（必须是可直接落地的完整内容，不是片段说明）

【输出格式铁律】严格输出 JSON 数组（不要 markdown 代码块、不要任何解释文字），结构：
[
  {{
    "dim": "维度字段名（concept/key_rules/worldbuilding/character_profiles/plot_design/timeline/foreshadowing/locations/style_guide 之一）",
    "label": "维度中文名",
    "issues": ["该维度涉及的诊断问题1", "问题2"],
    "action": "修正动作说明",
    "new_content": "修正后的完整设定内容（300-800字，保持与其它维度一致）"
  }}
]
只输出确实需要修正的维度，没有问题的维度不要输出。最多输出6个维度。"""

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400

    from app import _call_llm
    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': '请基于报告诊断生成设定修正方案'}],
        max_tokens=0, temperature=0.5, task_type='creation'
    )
    if err:
        return jsonify({'error': f'生成修正方案失败：{err}'}), 500

    # 解析 JSON 数组
    plan = []
    try:
        import re as _re_fix
        m = _re_fix.search(r'\[[\s\S]*\]', content or '')
        if m:
            plan = json.loads(m.group())
    except (json.JSONDecodeError, ValueError):
        pass
    # 兜底：若解析失败，把整段作为单项返回
    if not isinstance(plan, list) or not plan:
        plan = [{'dim': 'foreshadowing', 'label': '伏笔',
                 'issues': ['AI 输出未按 JSON 格式，请查看 new_content 原文'],
                 'action': '请人工核对', 'new_content': content or ''}]

    # 清洗：只保留白名单维度，字段补全
    cleaned = []
    for p in plan:
        if not isinstance(p, dict):
            continue
        dim = (p.get('dim') or '').strip()
        if dim not in _FIXABLE_DIM_FIELDS:
            continue
        item = {
            'dim': dim,
            'label': p.get('label') or _FIXABLE_DIM_FIELDS[dim],
            'issues': p.get('issues') if isinstance(p.get('issues'), list) else ([str(p.get('issues'))] if p.get('issues') else []),
            'action': (p.get('action') or '').strip(),
            'new_content': (p.get('new_content') or '').strip(),
        }
        if item['new_content']:
            cleaned.append(item)

    return jsonify({
        'plan': cleaned,
        'report_title': target_rec.get('title', '防遗忘检查报告'),
        'report_id': report_id,
    })


@chat_collab_bp.route('/api/ai/smart/apply-fix', methods=['POST'])
def smart_apply_fix():
    """应用用户确认的修正方案到对应 bible 维度字段（落地）。

    body: { book_id, fixes: [{ dim, new_content }] }
    仅写入用户勾选的维度，未勾选的不动。
    返回: { ok, applied: [{ dim, label }] }
    """
    from app import db, BookBible
    data = request.json or {}
    book_id = data.get('book_id')
    fixes = data.get('fixes') or []

    if not book_id or not isinstance(fixes, list) or not fixes:
        return jsonify({'error': '参数无效：需要 book_id 和非空 fixes'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    applied = []
    for f in fixes:
        if not isinstance(f, dict):
            continue
        dim = (f.get('dim') or '').strip()
        new_content = (f.get('new_content') or '').strip()
        if dim not in _FIXABLE_DIM_FIELDS or not new_content:
            continue
        setattr(bb, dim, new_content)
        applied.append({'dim': dim, 'label': _FIXABLE_DIM_FIELDS[dim]})

    if applied:
        bb.updated_at = datetime.now(timezone.utc)
        db.session.commit()

    return jsonify({'ok': True, 'applied': applied})


# ============================================================================
# 第三阶段：基于防遗忘报告自动定位章节段落并生成正文改写补丁
# ============================================================================

def _locate_chapter_by_location(location: str, chapters: list):
    """根据违规位置字符串定位章节。支持“第N章”或标题匹配。"""
    if not location:
        return None
    # 优先匹配“第N章”
    nums = re.findall(r'第\s*(\d+)\s*章', str(location))
    if nums:
        idx = int(nums[0]) - 1
        non_vol = [c for c in chapters if not getattr(c, 'is_volume', False)]
        if 0 <= idx < len(non_vol):
            return non_vol[idx]
    #  fallback：匹配章节标题
    for c in chapters:
        title = getattr(c, 'title', '') or ''
        if title and title in str(location):
            return c
    return None


def _extract_json_from_llm_text(text: str):
    """从 LLM 返回的文本中提取 JSON 对象（兼容代码块和普通文本）。"""
    if not text:
        return None
    # 优先尝试去掉 markdown 代码块
    fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        candidate = text.strip()
    # 取第一个 { 到最后一个 } 之间的内容
    m = re.search(r'\{[\s\S]*\}', candidate)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        # 尝试修复尾部多余逗号
        try:
            fixed = re.sub(r',\s*([}\]])', r'\1', m.group())
            return json.loads(fixed)
        except Exception:
            return None


@chat_collab_bp.route('/api/ai/smart/fix-text-from-report', methods=['POST'])
def smart_fix_text_from_report():
    """基于防遗忘检查报告，定位具体章节段落并生成正文改写补丁。

    body: { book_id, report_id?, skill_pack_ids? }
    返回: { fixes: [{ chapter_id, chapter_title, paragraph_index, original, rewritten, reason, violation_desc }] }
    """
    try:
        from app import db, Book, BookBible, Chapter, AIConfig, _call_llm

        data = request.json or {}
        book_id = data.get('book_id')
        report_id = data.get('report_id')
        skill_pack_ids = data.get('skill_pack_ids') or []
        volume_ids = data.get('volume_ids') or []
        if not isinstance(volume_ids, list):
            volume_ids = []

        if not book_id:
            return jsonify({'error': '缺少 book_id'}), 400
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            return jsonify({'error': '请先创建设定'}), 400

        cfg = AIConfig.get_active()
        if not cfg or not cfg.api_key:
            return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

        try:
            reports = json.loads(bb.anti_forget_reports) if bb.anti_forget_reports else []
        except Exception:
            reports = []
        if not isinstance(reports, list) or not reports:
            return jsonify({'error': '暂无防遗忘检查报告'}), 400

        target_rec = None
        target_report = {}
        if report_id:
            for r in reports:
                if isinstance(r, dict) and r.get('id') == report_id:
                    target_rec = r
                    target_report = r.get('report') or {}
                    break
            if not target_rec:
                return jsonify({'error': '未找到指定报告'}), 404
        else:
            recent = sorted([r for r in reports if isinstance(r, dict)], key=lambda x: x.get('checked_at', ''), reverse=True)
            if not recent:
                return jsonify({'error': '暂无防遗忘检查报告'}), 400
            target_rec = recent[0]
            target_report = target_rec.get('report') or {}
            report_id = target_rec.get('id')

        violations = target_report.get('violations') or []
        if not isinstance(violations, list):
            violations = []

        def _severity_key(v):
            sev = (v.get('severity') or '').strip()
            if sev == '严重':
                return 0
            if sev == '警告':
                return 1
            return 2

        # 只保留有 location 的 dict 违规，按严重程度排序，取前 5
        filtered = sorted(
            [v for v in violations if isinstance(v, dict) and v.get('location')],
            key=_severity_key
        )[:5]
        if not filtered:
            no_loc = [v for v in violations if isinstance(v, dict) and not v.get('location')]
            if no_loc:
                return jsonify({'fixes': [], 'empty_reason': '报告中的违规项缺少位置信息（location 字段为空），无法定位到具体章节。请重新运行防遗忘检查，确保违规项包含“第N章”或章节标题。'})
            return jsonify({'fixes': [], 'empty_reason': '报告中没有违规项，无需修正正文。'})

        chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

        # 去AI/审稿阶段·专属规则（通用核心+行文规范+完整去AI手册+review技能包）
        skill_note = build_review_rules(skill_pack_ids, mode='agent')

        cases = []
        case_chapters = []
        located_but_filtered_out = 0
        not_located = 0
        for v in filtered:
            location = v.get('location') or ''
            desc = v.get('desc') or ''
            fix_hint = v.get('fix') or ''
            severity = v.get('severity') or ''
            ch = _locate_chapter_by_location(location, chapters)
            if not ch or not ch.content:
                not_located += 1
                continue
            if volume_ids and getattr(ch, 'parent_id', None) not in volume_ids:
                located_but_filtered_out += 1
                continue
            cases.append({
                'case_index': len(cases),
                'chapter_id': ch.id,
                'chapter_title': ch.title or f'第{chapters.index(ch) + 1}章',
                'location': location,
                'severity': severity,
                'desc': desc,
                'fix_hint': fix_hint,
                'context': str(ch.content)[:1200],
            })
            case_chapters.append(ch)

        if not cases:
            if volume_ids and located_but_filtered_out > 0 and not_located == 0:
                return jsonify({'fixes': [], 'empty_reason': f'共 {located_but_filtered_out} 处违规已定位到章节，但均不在所选分卷内。请选择包含违规章节的分卷，或不限分卷重新检查。'})
            if not_located > 0 and located_but_filtered_out == 0:
                return jsonify({'fixes': [], 'empty_reason': f'共 {not_located} 处违规的位置无法匹配到已有章节（位置格式需为“第N章”或章节标题）。请检查违规位置描述，或重新运行防遗忘检查。'})
            return jsonify({'fixes': [], 'empty_reason': f'共 {len(filtered)} 处违规均无法生成可修正案例（{not_located} 处定位失败，{located_but_filtered_out} 处不在所选分卷）。请检查违规位置或重新选择分卷。'})

        case_blocks = []
        for case in cases:
            case_blocks.append(f"""【违规案例 {case['case_index']}】
位置：{case['location']}
严重程度：{case['severity']}
问题描述：{case['desc']}
修正建议：{case['fix_hint']}
章节上下文：
{case['context']}""")
        cases_text = '\n\n'.join(case_blocks)

        skill_section = f"【技能包指引】\n{skill_note}\n" if skill_note else ''
        volume_section = ''
        if volume_ids:
            volume_section = f"【本次只处理以下卷】\n卷ID: {volume_ids}\n请只从这些卷中定位需要改写的段落。\n"

        system_prompt = f"""你是资深网文正文修正师。下面有 {len(cases)} 处违规，每处包含位置、问题描述、修正建议和对应章节上下文。
请逐条分析，只改写确实需要修正的原文段落，保持文风、剧情、语气不变，不扩写。

{skill_section}{volume_section}{cases_text}

【输出格式铁律】严格输出 JSON 对象，不要 markdown 代码块、不要解释：
{{
  "fixes": [
    {{
      "case_index": 0,
      "paragraph_index": 0,
      "original": "需要改写的原文完整段落（50-400字）",
      "rewritten": "改写后的段落",
      "reason": "一句话说明为什么改写"
    }}
  ]
}}
如果某条违规无法定位或无需改写，可以不输出对应 fix。"""

        content, err = _call_llm(
            [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': '请生成正文改写补丁'}],
            max_tokens=0, temperature=0.4, task_type='creation'
        )
        if err:
            return jsonify({'error': f'生成正文改写补丁失败：{err}'}), 500

        parsed = _extract_json_from_llm_text(content)
        if parsed is None:
            return jsonify({'fixes': [], 'empty_reason': 'AI 返回内容无法解析为有效 JSON 补丁（可能模型未按格式输出）。请重试，或在 AI 配置中换一个响应更稳定的模型。'})
        arr = parsed.get('fixes') or []
        if not isinstance(arr, list):
            return jsonify({'fixes': [], 'empty_reason': 'AI 返回的 fixes 字段格式异常（非数组）。请重试，或换一个模型。'})

        fixes = []
        dropped_no_match = 0
        for fx in arr:
            if not isinstance(fx, dict):
                continue
            case_index = fx.get('case_index')
            if not isinstance(case_index, int) or case_index < 0 or case_index >= len(cases):
                continue
            case = cases[case_index]
            ch = case_chapters[case_index]
            original = (fx.get('original') or '').strip()
            rewritten = (fx.get('rewritten') or '').strip()
            if not original or not rewritten or original == rewritten:
                continue
            if original not in str(ch.content):
                dropped_no_match += 1
                continue
            try:
                paragraph_index = int(fx.get('paragraph_index', 0))
            except Exception:
                paragraph_index = 0
            fixes.append({
                'chapter_id': case['chapter_id'],
                'chapter_title': case['chapter_title'],
                'paragraph_index': paragraph_index,
                'original': original,
                'rewritten': rewritten,
                'reason': (fx.get('reason') or '').strip() or f"修正：{case['desc']}",
                'violation_desc': case['desc'],
                'report_id': report_id,
            })

        if not fixes and dropped_no_match > 0:
            return jsonify({'fixes': [], 'empty_reason': f'AI 生成了 {len(arr)} 条补丁，但原文片段均无法在章节内容中精确匹配（{dropped_no_match} 处对不上）。可能是模型对原文的复述偏差较大，建议重试或换一个模型。'})
        return jsonify({'fixes': fixes, 'report_title': target_rec.get('title', ''), 'report_id': report_id})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'生成正文改写补丁失败：{e}'}), 500


@chat_collab_bp.route('/api/ai/smart/apply-text-fix', methods=['POST'])
def smart_apply_text_fix():
    """应用用户确认的正文改写补丁到对应章节。

    body: { book_id, fixes: [{ chapter_id, paragraph_index?, original, rewritten }] }
    返回: { ok, applied: [{ chapter_id, chapter_title, count }] }
    """
    from app import db, BookBible, Chapter
    data = request.json or {}
    book_id = data.get('book_id')
    fixes = data.get('fixes') or []

    if not book_id or not isinstance(fixes, list) or not fixes:
        return jsonify({'error': '参数无效：需要 book_id 和非空 fixes'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    # 按章节分组，同章节按 paragraph_index 降序处理，避免索引漂移
    by_chapter: dict[str, list] = {}
    for f in fixes:
        if not isinstance(f, dict):
            continue
        cid = f.get('chapter_id')
        original = (f.get('original') or '').strip()
        rewritten = (f.get('rewritten') or '').strip()
        if not cid or not original or not rewritten:
            continue
        by_chapter.setdefault(cid, []).append(f)

    applied = []
    for cid, ch_fixes in by_chapter.items():
        ch = Chapter.query.filter_by(id=cid, book_id=book_id, is_volume=False).first()
        if not ch:
            continue
        content = ch.content or ''
        # 优先按整段替换；如果原文不在内容中，尝试按 paragraph_index 替换
        count = 0
        # 先尝试直接字符串替换（original 为完整段落）
        for f in ch_fixes:
            original = f.get('original')
            rewritten = f.get('rewritten')
            if original in content:
                content = content.replace(original, rewritten, 1)
                count += 1
        # 对于未直接替换成功的，尝试按段落索引
        paragraphs = [p for p in content.split('\n\n')]
        for f in sorted(ch_fixes, key=lambda x: int(x.get('paragraph_index', 0) or 0), reverse=True):
            if f.get('original') in (ch.content or ''):
                # 已在上一步替换
                continue
            idx = int(f.get('paragraph_index', 0) or 0)
            if 0 <= idx < len(paragraphs):
                old_para = paragraphs[idx].strip()
                if old_para and old_para != f.get('rewritten'):
                    paragraphs[idx] = f.get('rewritten')
                    count += 1
        if count > 0:
            ch.content = '\n\n'.join(paragraphs)
            ch.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            applied.append({'chapter_id': cid, 'chapter_title': ch.title or '', 'count': count})

    if applied:
        bb.updated_at = datetime.now(timezone.utc)
        db.session.commit()

    return jsonify({'ok': True, 'applied': applied})
