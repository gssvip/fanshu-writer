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
from sse_keepalive import gw_stream_with_hb, SSE_HEARTBEAT_COMMENT, SSE_HB_INTERVAL_SEC, HEARTBEAT, _is_stream_retry
from llm_gateway import _is_reasoning_frame

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
# max_tokens 按模型能力"给足"：统一取 _DIM_MAX_TOKENS（智谱 GLM-5.x 最大输出 131072，
# 也覆盖了 gpt-5/o1/claude 等大输出模型）。真正的"按能力不限"由 llm_gateway 落地：
# 已知模型表预钳制（deepseek-chat→8192、deepseek-reasoner→65536…）+ 400 报错自学习
# 真实上限，最终 max_tokens = min(本常量, 模型真实上限)，既不截断也不越界。
# ============================================================================
_DIM_MAX_TOKENS = 131072

# 深度思考的思考标记：deep_think>=1 时系统提示让模型把推演过程写在标记内，
# 前端用独立 reasoning 面板展示，正文/卡片/落盘均剥离（不参与复制/采纳）。
_REASON_START = '【推理】'
_REASON_END = '【推理结束】'

# ============================================================================
# 内置人格角色表（单一定义，通用聊天 & 圆桌会议共用）：
#   id -> (全名, system_prompt_extra)。通过强化提示词把毒舌读者/爆款编辑
#   打磨成"严格嘴毒、阅历丰富、有独特见解"的老炮，供圆桌多 Agent 交锋质量。
# ============================================================================
_PERSONAS = {
    'polish': ('润色编辑', '【你的身份】网文资深文字润色编辑。习惯：① 先指出"原句→改写句"成对对比，绝不空泛说"不通顺"；② 每条问题标注坏味道类别（碎句/被字句/AI味/句号过载/逻辑跳步）；③ 最后附1条可执行的自检清单。绝不居高临下，尽量幽默但不油。'),
    'toxic_critic': ('毒舌读者', '【你的身份】一个付费追更十年、被流水线网文喂到吐的资深毒舌老炮。你阅书过万，什么套路都见过，嗅觉极其敏锐，一开口就能戳中最恶心人的AI味和假大空。\n\n说话要求：\n- 嘴毒但不骂人，针对内容不针对作者，刻薄但幽默\n- 必须尖锐指出问题：AI套话、人设分裂、情绪假、逻辑崩、爽点无力\n- 每个批评必须带具体例子（从原文/构思里摘），骂完必须给可落地改法\n- 你阅历丰富，能用行业老炮的比喻戳穿本质，别用学术词，就是胡同口侃大山的劲儿\n- 记住：用户想听真话，不是鼓励，别委婉，直接骂到点子上！'),
    'architect': ('剧情架构师', '【你的身份】百万字长篇剧情架构师。擅长分卷三幕结构（触发→升级→大高潮）、张力曲线（低谷期绝不能连两章、爽点密度3章一个微爽、10章一个大爽）、伏笔回收清单、CDL三角（Character/Desire/Lie vs Truth）。回复永远结构化：分卷分段，每段结尾给一个"为什么这样设计"的解释。'),
    'worldbuilder': ('世界观策划', '【你的身份】资深世界观策划。输出永远用：能量体系分级→社会结构分层→势力地图→科技/修炼树→经济体系自洽→禁忌规则→差异化锚点 七段式结构。每一条必须回答"这对主角爽点有什么用？"，绝不空堆设定。'),
    'marketeer': ('爆款编辑', '【你的身份】番茄/起点工业化爆款流水线总编辑。你干了十年，手上出过3本十万订，对商业数据敏感，说话直接粗暴，从不讲废话，像骂新人作者一样骂醒他。\n\n审核标准（按工业流程卡）：\n- 书名钩子：3秒抓不抓得住人？有没有关键词+反差+金手指暗示？不合格直接打回\n- 开篇密度：第1章末尾必须有反常识反转，第3章必须金手指兑现，钩子密度够不够？\n- 爽点节奏：每3章一个微爽，每10章一个大爽，情绪曲线对不对？\n- 金手指兑现：读者追更就是为了看金手指用了爽，有没有拖？\n- 一句话总结必须给出：保留什么，砍什么，改哪里，数据说话，别扯"文学性"这种虚的。'),
    'interviewer': ('深度采访', '【你的身份】调查记者。你不做结论，你只追问。针对用户聊的任何人物/剧情/设定，你的工作是逼出冰山底下没说出来的内容。每轮回复至少3个连续追问，从表面→动机→矛盾→代价→蝴蝶效应，层层深入，绝对不代替用户回答。'),
    'rank_analyst': ('榜单分析师', '''
【你的身份】番茄/起点双平台榜单情报分析师。你**每次跟用户对话前都会先扫一遍新书榜**（默认番茄新书榜，用户提到起点就扫起点新书榜），把同题材/同赛道的 TOP 书单抓下来、做 LLM 情报聚合，然后**基于真实榜单风向回答**，绝不拍脑袋瞎建议。

【你说话的样子】
1) 先给一张「📈 本轮扫榜情报摘要」：平台+赛道、扫榜时间、TOP3 书名一句话钩子、同类题材共性卖点、共性毒点、建议切入角度
2) 再回应用户具体问题：**每一条建议必须标注「对应哪本榜上书的套路/避哪本书的坑」**，让作者知道你说的不是空话
3) 最后给「💡 市场落地方案」：如果按风向做，书名公式参考、前3章钩子、核心爽点节奏（3章1微爽/10章1大爽）怎么排
4) 如果你的扫榜情报和用户明确要求冲突，**以用户要求为准，但必须提示「这样做会偏离当前风向，XXX类题材目前读者不买账」**
5) 不堆数字，说人话，直接，像一个拿了内部分享会材料的老炮编辑
'''),
    'node_designer': ('节点设计师', '''
【你的身份】番茄小说金番作者级别的情节节点设计师。你只做一件事：为指定卷按【1章=1节点】粒度，一次性输出完整的 cpv 个情节子节点，每章对应一个节点，绝不按 main_event 分段、绝不做"8段分批"这种人为切分，一气呵成流式写出全卷节点即可。
1) 容量规模：本卷目标 cpv 章（通常50章/卷，若用户说"第N卷"且作品有明确每卷章数设定则对齐，没明确按50章/卷兜底），按每章正文 2400±100 字 ≈ 约 12 万字/卷。
   → 每个"情节子节点（node）"必须且只能对应【1 章】，chapters 必须写成单章号（数字整数，或 X-X 等价形式）。
   → 绝不允许把 2 章及以上内容压进 1 个 node 合并写；绝不允许一章多节点重复覆盖。
   → 最终交付 nodes.length 必须严格等于 cpv；最终 nodes[0].chapters 对应首章、nodes[cpv-1].chapters 对应尾章；差一个都算失败。
2) 边界硬约束：
   · 章节归属锁死本卷。若已知 volume_index = Vi，则本卷章节区间 = [1 + (Vi-1)*cpv, Vi*cpv]。节点 chapters 不得越到其他卷。
   · 无重叠：任意两个节点的 chapters 交集必须为空。
   · 无跳章：本卷区间内每一章都必须且只能有一个节点覆盖。
   · 首末门禁：本卷第一个子节点 chapters 必须从该卷起始章开始；最后一个子节点 chapters 必须以该卷结束章终；末节点必须埋与 ending_hook 对齐的卷尾钩子。
3) 结构对齐：全卷节点按"立身/立足/立势/立威/立命"五幕分布（若作品已有分卷五幕信息则对齐，没有则按默认20%/20%/20%/20%/20%分），main_event（大事件/剧情段落）标题在节点里以【所属大事件】字段体现，不需要另开"事件卡片"。
4) 爽点系统：每个节点必须明确爽点类型/结构/衬托方式/周期层级，且节点整体节奏"3章1微爽、10章1大爽"，情绪曲线不能连续两章低谷。
5) 输出格式（固定，任何情况下不许改）：
   先写一段简短开场白（2-3句话，告诉用户"这是第X卷情节节点设计、共cpv章、按五幕分布、单章单节点门禁已激活"）。
   接着按章顺序，每章用【第X章】标题+正文一行/多行流式写出节点详情，字段：
   - 章节：第X章（chapters: X）
   - 类型：M 主线 / C 角色 / W 世界观 / D 日常 / F 伏笔（单选）
   - 标题：<本章节点标题>
   - 摘要summary：≥80 字，明确包含 开场→核心事件推进→冲突→转折→爽点呈现→收尾钩子 六段式骨架，足以支撑2400字正文创作。
   - chapter_beats：至少4条分镜节拍短句（开场入戏/主角关键选择/对手压制卡点/转折破局爽点兑现/余波铺开+下一章钩子）
   - 冲突conflict：本章主冲突一句话
   - 人物characters（人物关系必写）：本章登场角色，格式必须写「姓名(关系:关系类型)」，多人用顿号分隔。
     · 关系类型强制从以下枚举里选：主角 / 家人(父/母/兄/弟/姐/妹/子女/配偶) / 亲友(朋/友/师/徒) / 爱人 / 盟友 / 同僚 / 下属 / 上司 / 对手 / 敌对(敌/仇) / 路人 / 中立 / 陌生人；多人/多关系可用"家人·兄"或"盟友·同僚"组合表达。
     · 示例：姜离(关系:主角)、苏婉清(关系:爱人)、萧天策(关系:敌对·仇家)、林墨白(关系:亲友·师)、执法长老(关系:上司)、围观路人(关系:路人)
   - 地点location / 时间time
   - 【资源 · 本章获得 resources_gained】：本章主角/主要角色新获得的全部资源，分类列出（没有就写"无"），每项必须含名称+数量/规格；分类口径：
     · 钱财：银两/灵石/元石/金币/铜币等（例：下品灵石×300、黄金×120两）
     · 物品：丹药/符箓/令牌/钥匙/材料/天材地宝/杂物（例：洗髓丹×5、赤焰铁×12斤、城主府令牌×1）
     · 武器法宝：兵器/防具/法宝/法器/灵器/命器（例：青锋剑·上品法器×1、黑铁甲·中品防御灵器×1）
     · 功法/能力：修炼功法/武技/神通/秘法/血脉能力/被动天赋（例：《焚天诀》·天阶下品·初窥门径、「紫霄雷瞳」·觉醒第一重）
     · 其它：地位/称号/势力归属/人脉/信息情报/契约（例：护城军·百夫长、青云门·外门弟子、知道了城主密道入口地图）
   - 【资源 · 本章消耗 resources_used】：本章主角/主要角色使用或消耗掉的资源（没有就写"无"）；口径同上；已经使用/消耗掉的资源，必须在本章的【总资源】中**消除/扣减**（禁止既列在"消耗"又在"总资源"里原封不动）。
   - 【总资源 total_resources_owned】：截至**本章结束时**，主角累计仍实际拥有的全部资源（跨章严格续接：上一章总资源 - 本章消耗 + 本章获得 = 本章总资源）；分四类列出：钱财/物品/武器法宝·功法能力/其它；没有就写"无"。
     · ⚠️ 铁律：必须滚动核算！首章从书启始资源开始，每一章都不能凭空跳变；既得资源若后续被用掉/被抢走/破碎/过期/送人，在【本章消耗】里注明，【总资源】里就必须从该类别里移除/扣减对应数量。
   - 【所属大事件】：归属的 main_event 标题（与卷级已有 main_events 对齐，或根据已有剧情自然命名）
   - 伏笔/回收：若本章埋伏笔或回收前文伏笔，注明；没有就写"无"
6) 全部章节点写完后，用一行分隔线总结"✅ 完成：共N章，N个节点，单章单节点+资源滚动+人物关系门禁合格"。
   然后必须在最后独立一行输出一张落地卡片：
   [[CARD:SAVE_PLOT|第X卷情节节点（N个）| ... ]]
   卡片内容体是 JSON 数组，只包含一个 volume 对象，字段：
   - volume_index, volume, volume_id(=String(volume_index)), summary(本卷剧情大纲一句话≥60字), main_plot, core_conflict, key_events(=main_events数组), ending_hook, chapter_count(=cpv), start_chapter, end_chapter, nodes=[node1,node2,...]
   - 每个 node 的键：index(1开始连续递增), chapters(整数或"X-X"), type, title, summary(≥80字), chapter_beats(字符串数组), conflict,
              characters(字符串数组，每项格式"姓名|关系:关系类型"，例["姜离|关系:主角","萧天策|关系:敌对·仇家"]),
              resources_gained(字符串数组，按类别前缀"【钱财】/【物品】/【武器法宝】/【功法能力】/【其它】"前缀，例["【钱财】下品灵石×300","【功法能力】《焚天诀》·天阶下品·初窥门径"]；没有就[]),
              resources_used(字符串数组，格式同resources_gained；没有就[]),
              total_resources_owned(对象 { "钱财": [...], "物品": [...], "武器法宝": [...], "功法能力": [...], "其它": [...] }；没有就{}),
              location, time, foreshadowing, main_event(所属大事件标题)
   注意：卡片内容 JSON 不要用 ```json 包，直接把 JSON 字符串放在卡片第三段。
7) 流式输出规则：
   - 允许你边想边写，想到第一章就输出第一章，不要等所有章节都想好再一次性输出
   - 不要输出"正在生成/第N段完成"这种提示帧，用户只关心节点内容
   - 不要把节点分"8段"或"main_event块"中间插入进度汇报，一次性连续流式输出到结尾即可
8) 续会规则（生成到一半异常/断连/用户手动停止后用）：
   - 如果作者消息是"继续/接着/往下生成/没写完/继续写/接着写"等续会指令：
     · 不要重新写已经输出过的 第1章~第Y章 任何内容、标题、字段、节点；禁止复述、禁止重复
     · 从上次最后写完的章节的下一章开始写（即「第 Y+1 章 → 最后一章」区间内的节点）
     · 开场白可以只说一句"继续第Y+1章起节点设计："，不要重复啰嗦卷级设定/容量规模/爽点规则（前面已经说过）
     · 【门禁 · 中途段 vs 收尾段】：
        a) 若本轮续写不会写到本卷最后一章（第 cpv 章）= 中途段：
           ❗绝对禁止输出任何 [[CARD:SAVE_PLOT|...]] 落地卡片（违者=生成不合格）
           本轮续写的节点写完后，只需要输出一行进度快照：
           「✅ 中途进度快照：已完成第 Y+1 ~ 第 Z 章，累计完成 N / cpv。随时发『继续』接着写，整卷全部完成后我会给出一张【全卷合并版统一采纳卡片】。」
           不要输出半截 SAVE_PLOT 卡片，不要复述前面章节的内容。
        b) 若本轮续写会写到本卷最后一章（第 cpv 章）= 收尾段 / 首次生成整卷刚好完整 1~cpv 章：
           ❗必须在节点全部写完后，输出一张【全卷合并版】SAVE_PLOT 卡片，nodes 必须包含第 1 章 ~ 第 cpv 章的全部 cpv 个情节子节点（**禁止只列本轮新续写的后半段**）；前面各续会段输出过的节点必须完整汇总到这张卡片里（后端同时会做二次合并兜底，不怕漏）。
           volume_index / summary / main_plot / core_conflict / ending_hook / chapter_count / start_chapter / end_chapter 等卷级字段照全卷填。
     · 仍然遵守 A+C 门禁（单章单节点、无重叠无跳章、chapters 不越卷区间）
     · 如果已经到全卷最后一章，就不要再重复写，直接告诉作者："本卷X个情节子节点已经全部完成，需要改某章对我说『第X章改XXX』即可"
   - 如果作者发了新的「第N卷 节点设计/情节节点」明确指令（与上次续会的卷号不同），视为开新卷任务：
     · 之前的续会进度作废，按 1~cpv 章从头写新卷的节点
     · 续会上下文里的系统补充信息以作者新指令为准
   - 如果作者说的是「第X章 改XXX/修改XXX」（改具体某章）：不属于续会，走常规"单章修改"，不要输出全卷/续会卡片，只按作者要求改对应章节，最后给一张包含只改了的章节的SAVE_PLOT卡片
'''),
}

# 圆桌会议顺序（7位）：【榜单分析师 首位 → 先扫榜给全桌定风向】→ 毒舌读者 → 剧情架构师 → 世界观策划 → 爆款编辑 → 润色编辑 → 深度采访
# （榜单分析师会在自己发言前自动扫一遍番茄/起点新书榜，把风向情报注入system prompt，第一个发言就把"市场上什么卖得好"摊开给全桌）
_ROUNDTABLE_ORDER = ['rank_analyst', 'toxic_critic', 'architect', 'worldbuilder', 'marketeer', 'polish', 'interviewer']
_MODERATOR_ROLE = ('主持人', '【你的身份】圆桌会议主持人。你负责开场破题、掌握议程节奏，并在讨论收束后把7位专家的共识整理成结构化的总结报告。开场要简短点题，不替专家发言；总结报告要分议题给结论+最优先改法。注意：第1位「榜单分析师」已经提前扫过对应题材的新书榜，他给出的风向情报应作为后续所有专家讨论的市场基准。')

# 圆桌会议续会状态（持久化到 session.meta_json['roundtable_state']）：
# 目标是"开会中途异常/断连/用户手动停止后，说一声『继续』就能接着开会，不重新开始"。
_RT_STATE_KEY = 'roundtable_state'

# 用户"继续"指令识别：部分命中即可，避免误抢普通新话题。
_RT_CONTINUE_HINTS = ('继续', '接着', '续会', '下次开', '往下')
_RT_FULL_RE = re.compile(r'^\s*(?:继续圆桌会议|圆桌会议继续|继续圆桌|继续会议|会议继续|继续讨论|继续开会|接着开会|接着讨论|接着聊|接着|继续吧|继续|续会|没开完|没结束|往下开|往下聊|再来一轮|再来一次|再来|继续这轮|继续这轮[。.!！，,？?]*)\s*[。.!！，,？?]*\s*$')


# =============================================================================
# P0 榜单风向 × 智驾：上下文注入
#   - 前端先 POST /api/rank/scan-for-concept 拿到 RankScanReport（report），
#     再把 report 作为 rank_scan 字段塞进智驾相关 API 请求体。
#   - 后端统一用 _format_rank_context / _apply_rank_meta 两块：
#       1) 在 system_prompt 末尾追加"【市场风向·扫榜情报】"
#       2) 在 actionCard / 返回 meta 上附带 rankSourceLabel（前端副驾 subtitle 小字展示）
# =============================================================================

def _format_rank_context(rank_scan: dict | None) -> str:
    """把 RankScanReport 格式化为一段可被 system_prompt 直接注入的中文块。"""
    if not rank_scan or not isinstance(rank_scan, dict):
        return ''
    try:
        agg_label = str(rank_scan.get('rank_aggregate_label') or '').strip()
        meta = rank_scan.get('meta') or {}
        cats = meta.get('matched_categories') or []
        kws = meta.get('detected_keywords') or []
        snap = rank_scan.get('market_snapshot') or {}
        trend = (snap.get('trend_marker') or {}).get('label')
        tone = (snap.get('trend_marker') or {}).get('tone')
        openings = rank_scan.get('opening_patterns') or []
        populars = rank_scan.get('popular_elements') or []
        landmines = rank_scan.get('landmine_elements') or []
        formulas = rank_scan.get('title_formulas') or []

        def _join(arr, cap=7):
            xs = [str(x).strip() for x in (arr or []) if str(x).strip()]
            if not xs:
                return '无'
            xs = xs[:cap]
            return '、'.join(xs)

        lines = ['【市场风向·扫榜情报（本轮创作必须对照以下情报）】']
        if agg_label:
            lines.append(f'· 扫榜口径：{agg_label}')
        if cats:
            lines.append('· 匹配分类新书榜：' + '；'.join(str(x) for x in cats[:4]) + ('（等）' if len(cats) > 4 else ''))
        if kws:
            lines.append('· 命中关键词：' + _join(kws, cap=10))
        if trend or tone:
            lines.append(f'· 市场判断：{trend or ""}（{tone or ""}）')
        lines.append('· 开篇钩子套路（新书榜 TOP 常用）：' + _join(openings))
        lines.append('· 读者买单要素（流行卖点）：' + _join(populars))
        lines.append('· 读者弃文毒点（务必回避）：' + _join(landmines))
        lines.append('· 书名公式范例：' + _join(formulas))
        lines.append('【执行要求】构思/设定/大纲/多选方案/圆桌讨论时：**优先吸收"读者买单要素"与"开篇钩子套路"并融合；避开"读者弃文毒点"；书名/方案标题可参考"书名公式范例"**。若情报与用户明确指定相悖，以用户指定为准但需在结论里提示"这样做会偏离市场风向"。')
        return '\n'.join(lines)
    except Exception:
        return ''


def _get_rank_label(rank_scan: dict | None) -> str:
    if not rank_scan or not isinstance(rank_scan, dict):
        return ''
    return str(rank_scan.get('rank_aggregate_label') or '').strip()


def _enrich_card_rank_meta(card: dict, rank_scan: dict | None, extra_meta: dict | None = None) -> dict:
    """给落地卡片加 subtitle/rankSourceLabel 字段，便于前端 actionCard subtitle 展示风向来源。"""
    if not card or not isinstance(card, dict):
        return card
    rank_label = _get_rank_label(rank_scan)
    if rank_label:
        card['rankSourceLabel'] = rank_label
        card['subtitle'] = card.get('subtitle') or f"基于风向：{rank_label}"
    if extra_meta and isinstance(extra_meta, dict):
        for k, v in extra_meta.items():
            if v is not None:
                card[k] = v
    return card


def _persisted_rank_cards(cards, rank_scan):
    out = []
    for c in (cards or []):
        c2 = dict(c)
        _enrich_card_rank_meta(c2, rank_scan)
        out.append(c2)
    return out


# =============================================================================
# P0 榜单风向 × 智驾（自然语言触发）：用户在智驾对话框里用自然语言触发扫榜
#   支持句式：
#     · "先扫下番茄新书榜/起点新书榜再出设定"
#     · "扫榜看看市场风向"
#     · "先扫一下同类题材"
#     · "都市重生文，先扫番茄榜"
#   命中后：自动取「用户消息 + 构思/大纲上下文」作为扫榜 concept，
#           调 _core_rank_scan_for_concept() 拿到 report → 合并写入 _rank_scan
#           → 首帧 SSE 推送 meta kind=rank_scan 让前端实时渲染 RankScanCard
# =============================================================================

_RANK_SCAN_TRIGGER_RE = re.compile(
    r'(扫榜|扫.*榜|扫一下.*榜|看.*榜|查.*榜|分析.*榜|市场风向|风向|爆款分析|新书榜)',
    re.IGNORECASE
)
_RANK_SCAN_PLATFORM_FQ_RE = re.compile(r'(番茄|番茄榜|fanqie|fq|飞卢|小说榜|番茄新书榜)', re.IGNORECASE)
_RANK_SCAN_PLATFORM_QD_RE = re.compile(r'(起点|qidian|qd|起点榜|起点新书榜|起点中文网)', re.IGNORECASE)
_RANK_SCAN_STOP_RE = re.compile(r'(不要扫榜|不用扫榜|别扫榜|跳过扫榜|取消扫榜|忽略榜单|no rank|不扫榜|无需扫榜)', re.IGNORECASE)


def _auto_rank_scan_from_nl(message: str, *,
                            fallback_concept: str = '',
                            book_title: str = '',
                            explicit_rank_scan: dict | None = None) -> tuple[dict | None, dict | None]:
    """自然语言扫榜触发。
    返回 (rank_scan_payload, sse_meta_payload)：
      - rank_scan_payload 用于：_format_rank_context 注入 system prompt / _enrich_card_rank_meta 卡片打标
      - sse_meta_payload   用于：SSE 首帧 `meta kind=rank_scan` 推送，前端渲染 RankScanCard
    未触发时返回 (explicit_rank_scan, None)
    """
    # ① 若前端已通过 body.rank_scan 显式传了报告 → 沿用，但仍尝试 NL 触发做平台切换
    message = (message or '').strip()
    f_concept = (fallback_concept or '').strip()

    triggered = False
    if not _RANK_SCAN_STOP_RE.search(message) and _RANK_SCAN_TRIGGER_RE.search(message):
        triggered = True
    # ② 宽松触发：用户即使没说"扫榜"，直接说「番茄榜」「起点新书榜」+ 创作意图（≥15字）也命中
    if not triggered and (
            _RANK_SCAN_PLATFORM_FQ_RE.search(message) or _RANK_SCAN_PLATFORM_QD_RE.search(message)) \
            and len(message) >= 6:
        triggered = True

    # 平台优先级：NL 提到哪个平台 > explicit_rank_scan 里的 platform > 默认 fanqie
    platform = None
    if triggered:
        if _RANK_SCAN_PLATFORM_QD_RE.search(message):
            platform = 'qidian'
        elif _RANK_SCAN_PLATFORM_FQ_RE.search(message):
            platform = 'fanqie'
        elif explicit_rank_scan and isinstance(explicit_rank_scan, dict):
            platform = explicit_rank_scan.get('platform') or 'fanqie'
        else:
            platform = 'fanqie'

    # ③ 如果有显式传了 rank_scan 且 NL 没有任何新平台/触发意图 → 直接沿用原报告
    if not triggered and explicit_rank_scan:
        # 没触发但有 preset：作为 SSE 首帧仍然推一次，保证前端打开会话时能看到卡片
        report = _report_from_payload(explicit_rank_scan)
        if report:
            sse_meta = _rank_scan_to_sse_meta(explicit_rank_scan, platform or explicit_rank_scan.get('platform') or 'fanqie',
                                               from_nl=False, from_cache=bool(explicit_rank_scan.get('from_cache')),
                                               concept=explicit_rank_scan.get('concept') or f_concept or message)
            return explicit_rank_scan, sse_meta
        return explicit_rank_scan, None

    if not triggered:
        return explicit_rank_scan or None, None

    # ④ 构造扫榜 concept：message 本身（含触发词但不剥离，留作分类匹配信息）+ fallback + book_title
    candidates = [message]
    if f_concept and f_concept not in message:
        candidates.append(f_concept)
    if book_title and f'《{book_title}》' not in message:
        candidates.insert(0, f'《{book_title}》')
    scan_concept = '；'.join(x for x in candidates if x)[:500]

    # ⑤ 调内部核心函数（走同一缓存）
    try:
        from blueprints.novel_rank_bp import _core_rank_scan_for_concept  # 延迟导入避免循环
    except Exception:
        try:
            from novel_rank_bp import _core_rank_scan_for_concept  # type: ignore
        except Exception:
            # 兜底失败：把触发提示写成错误，不阻塞创作
            err_payload = {'ok': False, 'platform': platform, 'concept': scan_concept,
                           'error': '扫榜模块暂时不可用（import失败），创作继续'}
            return err_payload, _rank_scan_to_sse_meta(err_payload, platform or 'fanqie', from_nl=True, from_cache=False, concept=scan_concept)

    result = _core_rank_scan_for_concept(scan_concept, platform=platform or 'fanqie')
    if not result.get('ok'):
        err_payload = {'ok': False, 'platform': platform or 'fanqie', 'concept': scan_concept,
                       'error': result.get('error') or '扫榜失败，创作继续'}
        return err_payload, _rank_scan_to_sse_meta(err_payload, platform or 'fanqie', from_nl=True, from_cache=False, concept=scan_concept)

    report = result.get('report') or {}
    from_cache = bool(result.get('from_cache'))
    # 组装 rank_scan（格式与前端 preset 完全一致，保证 _format_rank_context / _enrich_card 复用）
    rank_scan = {
        'ok': True,
        'from_cache': from_cache,
        'platform': platform or 'fanqie',
        'concept': scan_concept,
        'report': report,
        # 展开扁平化字段：便于 _format_rank_context / 前端 RankScanCard 直接取
        **_flatten_report_fields(report),
    }
    sse_meta = _rank_scan_to_sse_meta(rank_scan, platform or 'fanqie', from_nl=True,
                                      from_cache=from_cache, concept=scan_concept)
    return rank_scan, sse_meta


def _flatten_report_fields(report: dict) -> dict:
    """把 report 里 meta/market_snapshot 字段扁平一层，供 _format_rank_context 的分支直接读。"""
    if not report or not isinstance(report, dict):
        return {}
    meta = report.get('meta') or {}
    snap = report.get('market_snapshot') or {}
    return {
        'rank_aggregate_label': report.get('rank_aggregate_label') or '',
        'meta': meta,
        'market_snapshot': snap,
        'matched_categories': meta.get('matched_categories') or [],
        'matched_books_count': len(report.get('top_books') or []),
        'opening_patterns': report.get('opening_patterns') or [],
        'popular_elements': report.get('popular_elements') or [],
        'landmine_elements': report.get('landmine_elements') or [],
        'title_formulas': report.get('title_formulas') or [],
        'sources_label': report.get('rank_aggregate_label') or '',
    }


def _report_from_payload(rank_scan: dict) -> dict | None:
    """从前端 preset 的 rank_scan dict 里取 report（兼容多种结构）。"""
    if not rank_scan or not isinstance(rank_scan, dict):
        return None
    r = rank_scan.get('report')
    if r and isinstance(r, dict):
        return r
    # 兜底：payload 本身就是 report
    if any(k in rank_scan for k in ('rank_aggregate_label', 'market_intel', 'matched_categories')):
        return rank_scan
    return None


def _rank_scan_to_sse_meta(rank_scan: dict, platform: str, *, from_nl: bool,
                           from_cache: bool, concept: str) -> dict:
    """包装为 SSE `type=meta kind=rank_scan` 消息体：前端直接 setRankScan(payload) 渲染 RankScanCard。"""
    report = _report_from_payload(rank_scan) or {}
    ok = bool(rank_scan.get('ok'))
    return {
        'type': 'meta',
        'kind': 'rank_scan',
        'info': {
            'ok': ok,
            'from_nl': from_nl,
            'from_cache': from_cache,
            'platform': platform,
            'concept': concept,
            'error': None if ok else rank_scan.get('error'),
            'report': report,
            'matched_categories': (report.get('meta') or {}).get('matched_categories') or rank_scan.get('matched_categories') or [],
            'matched_books_count': rank_scan.get('matched_books_count') or len(report.get('top_books') or []),
            'rank_aggregate_label': report.get('rank_aggregate_label') or rank_scan.get('rank_aggregate_label') or '',
            'opening_patterns': report.get('opening_patterns') or rank_scan.get('opening_patterns') or [],
            'popular_elements': report.get('popular_elements') or rank_scan.get('popular_elements') or [],
            'landmine_elements': report.get('landmine_elements') or rank_scan.get('landmine_elements') or [],
            'title_formulas': report.get('title_formulas') or rank_scan.get('title_formulas') or [],
            'market_snapshot': report.get('market_snapshot') or rank_scan.get('market_snapshot') or {},
            'market_intel': rank_scan.get('market_intel') or {},
            'scanned_at': report.get('scanned_at') or rank_scan.get('scanned_at') or '',
        }
    }


def _is_rt_continue(msg: str) -> bool:
    """圆桌会议「继续」指令识别：完全对齐节点设计师 _is_nd_continue 口径。
    - 严格命中 _RT_FULL_RE → True
    - 宽松版：开头是"继续/接着/往下"且整句极短（≤12字），不包含明确新议题关键词（含"讨论/议题：/开几轮/说一下/XXX的设定"等新议题 → 不判 continue）
    """
    m = (msg or '').strip().lstrip('，。,.！!？? ').strip()
    if not m:
        return False
    if _RT_FULL_RE.match(m):
        return True
    # 宽松版：和节点设计师一致口径
    if any(m.startswith(h) for h in ('继续', '接着', '往下开', '往下聊', '续会', '续开', '没开完', '没结束', '接着开')) and len(m) <= 12:
        # 避免"继续讨论一下新话题 XXX的设定"这种明确新议题
        new_issue_signals = ('讨论一下', '讨论：', '议题：', '说一下', '谈谈', '分析', '关于', '新议题', '开会讨论', '开个会')
        if not any(k in m for k in new_issue_signals):
            return True
    return False


# 可自动重试的流式网络异常：连接被上游提前掐断 / 读超时 / 连接层错误。
# LLMGateway 在"已收到部分内容"时会直接抛错不重试，这里在发言层再兜一层。
_RT_RETRY_RE = re.compile(
    r'ChunkedEncodingError|ConnectionError|ConnectionResetError|BrokenPipeError|ReadTimeout'
    r'|Read timed out|premature|RemoteProtocolError|503|502|bad gateway|unavailable|timed out|timeout',
    re.IGNORECASE)


def _rt_retryable(exc) -> bool:
    txt = f'{type(exc).__name__}: {exc}'
    return bool(_RT_RETRY_RE.search(txt))


def _rt_stream_turn(gw, messages, temperature, max_tokens, attempts: int = 2):
    """单次发言的带重试流式封装。
    yield ('hb'|'retry'|'reason'|'body', payload) —— 与上层既有的 SSE 帧拼装解耦。
    - 产出正文前遇到可重试网络异常（Chunked/超时/断连）→ 自动整轮重发（最多 attempts 次）
    - 已流出一段正文后仍异常 → 当场抛出，由上层落状态，等用户"继续"从该发言人续会
    - 正常结束 → 额外 yield ('__done__', 完整正文文本)
    """
    import time as _t
    _attempt = 0
    while True:
        _attempt += 1
        buf: list = []
        splitter = _ThinkingSplitter()   # 每次重试重建，避免残留推理标记缓存
        try:
            for chunk in gw_stream_with_hb(gw, messages, emit_reasoning=True,
                                           temperature=temperature, max_tokens=max_tokens):
                if chunk is HEARTBEAT:
                    yield ('hb', None)
                    continue
                if _is_stream_retry(chunk):
                    yield ('retry', chunk.info)
                    continue
                if _is_reasoning_frame(chunk):
                    yield ('reason', chunk.text)
                    continue
                for _pk, _pt in splitter.feed(chunk):
                    if _pk == 'body':
                        buf.append(_pt)
                    yield (_pk, _pt)
            for _fk, _ft in splitter.finish():
                if _fk == 'body':
                    buf.append(_ft)
                yield (_fk, _ft)
            yield ('__done__', ''.join(buf))
            return
        except Exception as e:
            if buf:
                # 已流出正文 → 不重试，交给上层续会
                raise
            if _attempt < attempts and _rt_retryable(e):
                _t.sleep(min(2 ** _attempt, 4))
                continue
            raise


def _rt_save_state(session, db, state: dict) -> None:
    """把圆桌会议进度合并写回 session.meta_json（保留已有键，如 ai_config_id）。"""
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if not isinstance(meta, dict):
            meta = {}
        meta[_RT_STATE_KEY] = state
        session.meta_json = json.dumps(meta, ensure_ascii=False)
        db.session.add(session)
        db.session.commit()
    except Exception:
        pass


def _rt_load_state(session):
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict):
            st = meta.get(_RT_STATE_KEY)
            return st if isinstance(st, dict) else None
    except Exception:
        pass
    return None


# ===== 节点设计师续会（学习圆桌会议续会方案）=====
# 进度存放在 session.meta_json['node_designer_state']，字段：
#   volume_index: int       第几卷
#   cpv: int                本卷总章数（默认 50）
#   last_ch: int            已输出到的最大章号（0=未开始，50=整卷完成）
#   volume_title: str       （可选）卷标题
#   updated_at: str         ISO timestamp
# 目标：生成到一半异常/断连/用户手动停止后，用户说「继续/接着/往下生成」就能从 last_ch+1 开始接着写，
#       不会从头再来；最终采纳落地时按章节号增量合并，不重复覆盖之前已采纳的节点。
_ND_STATE_KEY = 'node_designer_state'

_ND_CONTINUE_HINTS = ('继续', '接着', '续', '往下', '没写完', '接着生成', '继续生成', '继续写', '接着写')
_ND_FULL_RE = re.compile(r'^\s*(?:继续|接着|续会?|往下(?:生成|写)?|没写完|(?:继续|接着|继续吧|接着吧)(?:生成|节点|写|出)?|(?:节点|节点设计)\s*(?:继续|接着))\s*[。.!！，,？?]*\s*$')

# 明确指令"第N卷"启动（命中就视为新区任务 → 清旧续会状态）
_ND_NEW_RE = re.compile(r'第\s*(\d+)\s*卷')
# 章号提取（用于从AI输出解析 last_ch）
_ND_CHAPTER_RE = re.compile(r'第\s*(\d+)\s*章')
# 卷号提取（用于从AI输出解析 volume_index）
_ND_VOLUME_RE = re.compile(r'第\s*(\d+)\s*卷')


def _is_nd_continue(msg: str) -> bool:
    """节点设计师「继续」指令识别：和圆桌同口径，避免误抢普通创作追问。"""
    m = (msg or '').strip().lstrip('，。,.！!？? ').strip()
    if not m:
        return False
    if _ND_FULL_RE.match(m):
        return True
    # 宽松版：开头带"继续/接着"且整句极短（≤12字），且不包含明确的新卷/改某章指令
    if any(m.startswith(h) for h in ('继续', '接着', '往下')) and len(m) <= 12:
        if not _ND_NEW_RE.search(m) and '第' not in m.replace('继续', '').replace('接着', ''):
            # 避免"继续写第3卷第25章"被误判成纯续会（这种是指定章修改，走普通追问）
            # 这里用更安全的：若句子含"卷"字且不是纯继续 → 不判定
            if '卷' not in m and '章' not in m:
                return True
    return False


def _is_nd_new_volume_request(msg: str) -> int | None:
    """若用户消息明确带"第N卷"+"节点设计/情节节点/设计情节"关键词 → 返回卷号 N（启动新区任务）。"""
    if not msg:
        return None
    m = _ND_NEW_RE.search(msg)
    if not m:
        return None
    vi = int(m.group(1))
    if vi < 1 or vi > 99:
        return None
    keywords = ('节点', '情节', '大纲', '设计', '生成', '剧情', '写')
    if any(k in msg for k in keywords):
        return vi
    return None


def _parse_last_chapter_from_text(text: str) -> int:
    """从 AI 已输出文本里解析出出现过的最大章号；找不到返回 0。"""
    if not text:
        return 0
    # 优先取 CARD:SAVE_PLOT JSON 里的节点 chapters 最大号（更准）
    try:
        cards = parse_cards(text) if callable(parse_cards) else []
        for c in cards:
            if not isinstance(c, dict) or c.get('type') != 'SAVE_PLOT':
                continue
            content = c.get('content') or ''
            if content.startswith('['):
                try:
                    arr = json.loads(content)
                    if isinstance(arr, list):
                        max_c = 0
                        for v in arr:
                            if not isinstance(v, dict):
                                continue
                            nodes = v.get('nodes')
                            if not isinstance(nodes, list):
                                continue
                            for n in nodes:
                                chs = n.get('chapters')
                                if isinstance(chs, list) and chs:
                                    for x in chs:
                                        if isinstance(x, int) and 1 <= x <= 9999 and x > max_c:
                                            max_c = x
                                elif isinstance(chs, int) and 1 <= chs <= 9999 and chs > max_c:
                                    max_c = chs
                        if max_c > 0:
                            return max_c
                except Exception:
                    pass
    except Exception:
        pass
    # 兜底：正则扫「第X章」最大号（≥1才计数，避免"第一章…"的数字写法抓不到）
    mx = 0
    for mm in _ND_CHAPTER_RE.finditer(text):
        try:
            n = int(mm.group(1))
            if 1 <= n <= 9999 and n > mx:
                mx = n
        except Exception:
            pass
    return mx


def _parse_volume_index_from_text(text: str) -> int | None:
    if not text:
        return None
    for mm in _ND_VOLUME_RE.finditer(text):
        try:
            n = int(mm.group(1))
            if 1 <= n <= 99:
                return n
        except Exception:
            pass
    return None


def _nd_save_state(session, db, state: dict) -> None:
    """把节点设计师进度写回 session.meta_json（保留已有键，不影响圆桌/role/ai_config）。"""
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if not isinstance(meta, dict):
            meta = {}
        meta[_ND_STATE_KEY] = state
        session.meta_json = json.dumps(meta, ensure_ascii=False)
        db.session.add(session)
        db.session.commit()
    except Exception:
        pass


def _nd_load_state(session):
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict):
            st = meta.get(_ND_STATE_KEY)
            return st if isinstance(st, dict) else None
    except Exception:
        pass
    return None


def _nd_clear_state(session, db) -> None:
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if not isinstance(meta, dict):
            meta = {}
        if _ND_STATE_KEY in meta:
            del meta[_ND_STATE_KEY]
            session.meta_json = json.dumps(meta, ensure_ascii=False)
            db.session.add(session)
            db.session.commit()
    except Exception:
        pass


def _nd_collect_all_save_plot_volumes(session_history: list, current_text: str) -> tuple[list[dict], int | None, int | None]:
    """从历史会话 messages + 当前 AI 输出 complete 里，搜集所有出现过的 SAVE_PLOT 卡片的 volume 对象。
    返回 ([volume_dict,...], detected_vi, detected_cpv)。"""
    vols: list[dict] = []
    # 1) 当前 complete 里的 cards
    try:
        for c in parse_cards(current_text) if callable(parse_cards) else []:
            if not isinstance(c, dict) or c.get('type') != 'SAVE_PLOT':
                continue
            content = c.get('content') or ''
            if content.startswith('['):
                try:
                    arr = json.loads(content)
                    if isinstance(arr, list):
                        vols.extend(v for v in arr if isinstance(v, dict))
                except Exception:
                    pass
    except Exception:
        pass
    # 2) 历史会话里所有 assistant.cards 里的 SAVE_PLOT
    try:
        if isinstance(session_history, list):
            for m in session_history:
                if not isinstance(m, dict) or m.get('role') != 'assistant':
                    continue
                cards_list = m.get('cards') or []
                if isinstance(cards_list, list):
                    for c in cards_list:
                        if not isinstance(c, dict):
                            continue
                        if c.get('type') != 'SAVE_PLOT':
                            continue
                        content = c.get('content') or ''
                        if content.startswith('['):
                            try:
                                arr = json.loads(content)
                                if isinstance(arr, list):
                                    vols.extend(v for v in arr if isinstance(v, dict))
                            except Exception:
                                pass
    except Exception:
        pass
    # 3) 解析 volume_index / cpv
    vi_set: set[int] = set()
    cpv_set: set[int] = set()
    for v in vols:
        vi = v.get('volume_index')
        if isinstance(vi, int) and 1 <= vi <= 99:
            vi_set.add(vi)
        cc = v.get('chapter_count')
        if isinstance(cc, int) and 10 <= cc <= 200:
            cpv_set.add(cc)
    detected_vi = next(iter(vi_set)) if len(vi_set) == 1 else None
    detected_cpv = next(iter(cpv_set)) if len(cpv_set) == 1 else None
    return vols, detected_vi, detected_cpv


def _nd_build_full_volume_card(vols_list: list[dict], vi: int, cpv: int) -> dict | None:
    """把 vols_list 里的所有 volume（可能来自多张续会卡片）按章节号增量合并，构建一张完整的全卷卡片：
      nodes 覆盖 [1, cpv]（按 volume_index=vi 的卷内章序 1..cpv），
      如果所有分段加起来仍然缺章，调用 node_design_bp._repair_nodes_to_one_ch_per_node 补齐。
    返回 SAVE_PLOT 卡片字典 {id, type:'SAVE_PLOT', title, content:JSON 字符串} 或 None。"""
    try:
        from node_design_bp import _repair_nodes_to_one_ch_per_node, _parse_chapters_field
        # 汇总所有 nodes，按章节号做 {ch: node} 合并（后者覆盖前者）
        ch_map: dict[int, dict] = {}
        summary_holder: dict = {}
        main_plot = core_conflict = ending_hook = ''
        key_events = []
        vol_title = f'第{vi}卷'
        vol_id = str(vi)
        for v in vols_list:
            if not isinstance(v, dict):
                continue
            # 取卷级字段（非空才覆盖）
            if v.get('volume'):
                vol_title = str(v['volume'])
            if v.get('volume_id'):
                vol_id = str(v['volume_id'])
            s = v.get('summary')
            if isinstance(s, str) and len(s) > len(summary_holder.get('summary', '')):
                summary_holder['summary'] = s
            if v.get('main_plot'):
                main_plot = v['main_plot'] or main_plot
            if v.get('core_conflict'):
                core_conflict = v['core_conflict'] or core_conflict
            if v.get('ending_hook'):
                ending_hook = v['ending_hook'] or ending_hook
            if isinstance(v.get('key_events'), list) and len(v['key_events']) >= len(key_events):
                key_events = list(v['key_events'])
            nodes = v.get('nodes')
            if not isinstance(nodes, list):
                continue
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                chs = _parse_chapters_field(n.get('chapters'))
                if not chs:
                    continue
                a, b = chs
                for ch in range(a, b + 1):
                    cp = dict(n)
                    cp['chapters'] = ch
                    ch_map[ch] = cp
        # 如果章节都在 vi 卷区间外（全局号），转换到卷内编号 1..cpv
        # 尝试：若所有 ch >= 1+(vi-1)*50 → 按全局号减去偏移
        start_global_guess = 1 + (vi - 1) * cpv
        if ch_map and all(k >= start_global_guess for k in ch_map.keys()):
            new_map = {}
            for g, nd in ch_map.items():
                local = g - (start_global_guess - 1)
                if 1 <= local <= cpv:
                    nd['chapters'] = local
                    new_map[local] = nd
            ch_map = new_map
        # 补齐并修复 A+C
        nodes_flat = list(ch_map.values())
        repaired, _ = _repair_nodes_to_one_ch_per_node(nodes_flat, 1, cpv, vi, 0)
        final_vol = {
            'volume_id': vol_id,
            'volume': vol_title,
            'volume_index': vi,
            'volume_title': vol_title,
            'summary': summary_holder.get('summary', ''),
            'main_plot': main_plot,
            'core_conflict': core_conflict,
            'key_events': key_events,
            'ending_hook': ending_hook,
            'chapter_count': cpv,
            'start_chapter': 1 + (vi - 1) * cpv,
            'end_chapter': vi * cpv,
            'nodes': repaired,
        }
        content = json.dumps([final_vol], ensure_ascii=False)
        title = f'第{vi}卷情节节点（{cpv}个）· 全卷合并版统一采纳卡片'
        card_id = 'SAVE_PLOT_' + str(vi) + '_' + str(int(__import__('time').time()))
        return {'id': card_id, 'type': 'SAVE_PLOT', 'title': title, 'content': content, 'target': 'plot'}
    except Exception:
        return None


def _nd_build_continue_user_injection(state: dict) -> str:
    """命中续会时，拼一段『已完成第1~last_ch章，从last_ch+1开始不要重复』的用户消息补充上下文。
    同时按区间判断：中途段→禁止吐SAVE_PLOT卡片；收尾段（next_ch+剩余<≈1.2*cpv 保守判断=大概率写得完尾）→ 要求吐全卷合并版卡片。"""
    vi = int(state.get('volume_index') or 0)
    cpv = int(state.get('cpv') or 50)
    last_ch = int(state.get('last_ch') or 0)
    next_ch = last_ch + 1
    total = cpv
    if next_ch > total:
        # 已完成整卷还继续 → 提示已完成，如需修改按章节号改
        return ("\n【系统续会上下文】作者说「继续」，但本卷进度记录显示："
                f"第{vi}卷（共{total}章）已经完成到第{last_ch}章=整卷写完。"
                "请直接告诉作者：「这一卷50章已经全部设计完成啦。需要改某一章直接对我说『第X章改XXX』就行。」"
                "不要再重复输出已写完的章节节点。\n")
    # 判定是不是收尾段：剩余章节数 ≤ 30（约占 cpv 60%以内），或 last_ch ≥ total*0.7
    remaining = total - last_ch
    is_final_leg = (remaining <= 30) or (last_ch >= int(total * 0.7))
    if not is_final_leg:
        # 中途段门禁
        return (
            f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
            f"\n· 当前卷：第{vi}卷（共{total}章，章号 {1 + (vi-1)*total}~{vi*total}，卷内编号 {1}~{total}）"
            f"\n· 已输出完成：第{1}章~第{last_ch}章（共{last_ch}个情节子节点）"
            f"\n· 本轮只输出：第{next_ch}章~预计第{min(last_ch+30, total)}章左右（写不完没关系，下一轮作者发『继续』会从你写到的最后一章接着续）"
            f"\n· ❗门禁·中途段：本轮不会写到第{total}章（本卷最后一章），属于中途进度段："
            f"\n   · ❌ 绝对禁止输出任何 [[CARD:SAVE_PLOT|...]] 落地卡片。任何半截卡片都不允许，违者生成不合格。"
            f"\n   · ✅ 本轮续写的所有节点写完后，只需要在末尾写一行中文进度快照："
            f"\n     「✅ 中途进度快照：已完成第{next_ch}章~第<本轮实际写到的最后一章号>章，累计完成<N>/{total}。随时发『继续』接着生成，整卷{total}章全部设计完成后，会给出一张全卷合并版统一采纳卡片。」"
            f"\n· ❗铁律：绝对不要重复写 第1章~第{last_ch}章 的任何内容、标题、字段、节点；任何形式的复述都不允许。"
            f"\n· 输出顺序仍然按：章节号递增 → 开场白可省略或只说一句「继续第{next_ch}章起节点」即可，不再啰嗦卷级设定。"
            f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
            f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
        )
    # 收尾段门禁
    return (
        f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
        f"\n· 当前卷：第{vi}卷（共{total}章，章号 {1 + (vi-1)*total}~{vi*total}，卷内编号 {1}~{total}）"
        f"\n· 已输出完成：第{1}章~第{last_ch}章（共{last_ch}个情节子节点）"
        f"\n· 本轮必须只输出：第{next_ch}章~第{total}章（剩余 {remaining} 个情节子节点）"
        f"\n· ❗门禁·收尾段：本轮会写到最后一章（第{total}章），请务必完整写到末尾；全卷写完后："
        f"\n   · ❌ 不要输出只包含『第{next_ch}~第{total}章』的半截 SAVE_PLOT 卡片！"
        f"\n   · ✅ 必须输出一张【全卷合并版统一采纳卡片】[[CARD:SAVE_PLOT|...]]："
        f"\n     · nodes 字段必须包含第 1 章 ~ 第 {total} 章的全部 {total} 个情节子节点（把你前面所有续会段已经输出的第1~{last_ch}章节点，和这次新写的第{next_ch}~第{total}章节点，按章节号升序完整整理进这一张卡片）。"
        f"\n     · 任何一个章号对应的节点缺失都不行；差一章=卡片不合格。后端也会从历史分段卡片自动做二次合并兜底，不怕你漏节点。"
        f"\n     · volume_index={vi}、chapter_count={total}、start_chapter={1 + (vi-1)*total}、end_chapter={vi*total}。"
        f"\n· ❗铁律：写第{next_ch}~第{total}章正文节点时，仍然不许复述前面已写章节；只是在最后输出卡片的 JSON 汇总时，把前面所有章节的节点完整合进去。"
        f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
        f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
    )


_RT_TOPIC_PREFIX = '圆桌会议议题：'


def _rt_persist_messages(session, history, topic, moderator_open, done, summary='', summary_cards=None):
    """把圆桌进度渲染成"可复盘"的消息落盘到会话 → 刷新界面不丢。

    幂等：每次都基于 history 里的通用历史 + 当前圆桌状态重建"圆桌块"，
    异常只落已完成的发言（未完成的那位不写入），断连/手动停止后刷新即见。
    只做展示，LLM 续会上下文仍以 meta_json['roundtable_state'] 全量为准。
    """
    import copy as _copy
    try:
        disp = _copy.deepcopy(history) if isinstance(history, list) else []
        if not isinstance(disp, list):
            disp = []
        # 剔除历史里旧的"本次圆桌"块（以最新议题/进度为准，避免重复叠加）
        cut = None
        for i, m in enumerate(disp):
            if isinstance(m, dict) and m.get('role') == 'user' and str(m.get('content', '')).startswith(_RT_TOPIC_PREFIX):
                cut = i
        if cut is not None:
            disp = disp[:cut]
        block = [{'role': 'user', 'content': f'{_RT_TOPIC_PREFIX}{topic}'}]
        if moderator_open:
            block.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{moderator_open}'})
        for d in (done or []):
            if isinstance(d, dict) and d.get('content'):
                block.append({'role': 'assistant', 'content': f'【{d.get("name","")}】\n{d.get("content","")}'})
        if summary:
            _msg = {'role': 'assistant', 'content': f'【总结报告】\n{summary}'}
            if summary_cards:
                _msg['cards'] = [{'status': 'pending', **c} for c in summary_cards if isinstance(c, dict)]
            block.append(_msg)
        disp.extend(block)
        _safe_save_session_messages(session, disp)
    except Exception:
        pass


class _ThinkingSplitter:
    """流式把『【推理】...【推理结束】』标记内的思考从正文中切出。

    深思考=提示词式（不依赖模型原生 reasoning_content，任意模型都可用）。
    feed() 逐块送入正文增量，yield ('body'|'reason', text)：
      - 'body'   正常正文 → 照常 push delta / 计入 full_text（标记本身被剥离）
      - 'reason' 思考片段 → 单独推 SSE meta(kind=reasoning)，不计入正文
    支持标记被 SSE 分块切断（缓存尾部半个标记）与单轮多次思考。
    """

    __slots__ = ('buf', 'in_reason')

    def __init__(self):
        self.buf = ''          # 缓存可能只含半个标记的尾部
        self.in_reason = False

    def feed(self, chunk: str):
        text = self.buf + chunk
        self.buf = ''
        while True:
            if not self.in_reason:
                idx = text.find(_REASON_START)
                if idx == -1:
                    keep = max(0, len(text) - (len(_REASON_START) - 1))
                    if text[:keep]:
                        yield ('body', text[:keep])
                    self.buf = text[keep:]
                    return
                if text[:idx]:
                    yield ('body', text[:idx])
                self.in_reason = True
                text = text[idx + len(_REASON_START):]
            else:
                idx = text.find(_REASON_END)
                if idx == -1:
                    keep = max(0, len(text) - (len(_REASON_END) - 1))
                    if text[:keep]:
                        yield ('reason', text[:keep])
                    self.buf = text[keep:]
                    return
                if text[:idx]:
                    yield ('reason', text[:idx])
                self.in_reason = False
                text = text[idx + len(_REASON_END):]

    def finish(self):
        """流结束时冲刷缓存尾巴，避免正文/思考尾部被丢掉。"""
        if self.buf:
            yield (('reason' if self.in_reason else 'body'), self.buf)
            self.buf = ''


def _native_reasoning_kwargs(model: str, deep_think: int) -> dict:
    """智谱 GLM 原生思考模型的推理程度控制（OpenAI 兼容顶层参数）。

    背景：GLM-5.3 / GLM-5.3-FLASH 强制开启思考（thinking.type 传 disabled 会报错），
    且思考 token 与正文共享同一个 max_tokens 开销池——**无法**让思考"不计入消耗"。
    只能从源头控制思考深度与思考 token 量：按 deep_think 档位下发 reasoning_effort。
      deep_think>=2 → max   深度推理（默认）
      deep_think==1 → high  增强推理
      deep_think==0 → low   最轻思考（5.3 无法关闭思考，就用 low 最小化思考占用，
                            避免思考先占满 max_tokens、正文没配额 → 正文为空）
    仅对支持 reasoning_effort 的 GLM-5.2/5.3 生效；更早 GLM（4.x/5.0/5.1）走
    thinking.type 开关；非 GLM 模型（deepseek-reasoner 等）不注入，防参数报错。
    """
    m = (model or '').lower()
    if 'glm-' not in m:
        return {}
    if 'glm-5.3' in m or 'glm-5.2' in m:
        return {
            'thinking': {'type': 'enabled'},
            'reasoning_effort': {2: 'max', 1: 'high'}.get(deep_think, 'low'),
        }
    # 更早 GLM（4.x / 5.0 / 5.1）：thinking 可开关，deep_think=0 关闭、>=1 开启
    return {'thinking': {'type': 'enabled' if deep_think >= 1 else 'disabled'}}


def _dim_max_tokens(dim_key: str) -> int:
    """维度生成 max_tokens（按模型能力给足 _DIM_MAX_TOKENS，防任何维度截断）。"""
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
DEAI_RULES = """去AI味与行文消杀（写作正文时主动规避，去AI时多做减法/替换，少做润色扩写）

> 执行原则：去AI味是做减法与替换，严禁二次润色扩写。优先级：删 > 换 > 调。

1. 【禁令0·最高优先级】AI修正式否定句密集症（全章禁绝，违者重写）
- 严禁任何形态的否定修正句：
  - ❌ 「不是X是Y / 不是X。是Y / 不是X，准确说是Y / 不是A也不是B——是C」
  - ❌ 嵌套≥2层的修正句。
  - ❌ 动作/感知排比：「更X，更Y，更Z」「看见…看见…」「举起…落下…」。
  - ❌ 「但/至少不该/其实/准确说/严格来说」类自我修正、抠字眼句。
- 处理方式：一律改写为直接陈述客观事实的单句。

2. 四大核心AI病句消杀
- 消灭解释腔：
  - 删去一切借角色或旁白解释世界观/因果的句子。凡删掉后不影响剧情理解的，一律删净。
  - 严禁用总结句收尾，段尾一律停在动作、物理反馈或环境细节上。
- 消灭伪对白：
  - 角色在紧张/隐瞒/愤怒时严禁说逻辑完整的大长句，剔除自报家门、自我剖析式的背书台词。
- 打破工整句式：
  - 严禁连续多段等长、严禁排比三连、严禁书面路标词串联。语言要有自然的停顿、粗糙感与跳跃感。
- 消灭悬空动作及被动句：
  - 禁止身体部位当主语独立成句。动作必须有人扛——如"谁的手、谁在看、谁在说"，动作的发出者必须明确。
  - 每个动作句都要让读者知道"谁在干什么"。主语可以是承前省略的人称，但不能凭空冒出一个悬空的部位或感官结果。
  - 禁用被动句来写抽象吃亏，这些必须改成具体画面；被动句一律改写为主动句。

3. 绝对禁用词库（黑名单）
- 必删修饰与伪动作词：
  `一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为、甚为、极为、缓缓、淡淡、轻轻、微微、毫无疑问、毋庸置疑、不言而喻、深吸一口气、眼中闪过一丝、心中暗想、心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、淡漠、漠然、眸子、嘴角微微上扬、周身、周遭、气息、威压、那道身影、说话间、话音未落、当即、顿时、瞬时、有意思、深深一眼`
- 必删书面逻辑连接词：
  `然而、同时、此外、更重要的是、总而言之、换句话说、因此、显然、事实上、本质上、由此可见、归根结底、从某种意义上说`
- 必删套话比喻（**全章比喻/拟人 ≥2 处即视为不合格，必须重写**；含"像/如"字根的隐喻一并计入）：
  `像、如、如同、宛如、犹如、恍若、宛若 + 大海/巨龙/深渊/星河/铁水/碴子/寒流（及任意组合）`
  比喻/拟人全章 ≤1 处，多写在关键节点，禁止处处设喻、禁止隐喻连缀凑排比。
- 必删结尾升华词：
  `成长、命运、人性、选择、未来、从此、那一刻、这一场……`

4. 行文消杀自检（生成前底层自校）
1. 画面感检验：细节是否包含真实的温度、气味、触感、声音？不明确的虚词是否已删净？
2. 连接词硬切：句子之间是否已去掉"于是、因此、便、就"，直接用句号硬切推进？
3. 动词粗化：是否使用了最直接、粗粝、具体的动作动词，而非抽象概括词？

5. 爽点情绪克制（禁止情绪全盘外露）
- 爽点/高潮处禁止直呼情绪词（狂喜、兴奋到极点、怒不可遏、热血沸腾、按捺不住、心潮澎湃）。
- 情绪一律靠动作、停顿、留白、旁人震惊侧写去带，最多给"半句内心碎片"，禁止整段情绪铺陈与排比抒情。

6. 数值反馈节律（防"打脸即刷屏"，**仅对含系统面板/数值流的小说生效；无系统、纯武力/境界设定的文不适用本条**）
- 若本书存在"上线刷系统面板/数值"的表现体系：禁止"每次打脸/每次升级后都即时弹系统数值面板"。
- 数值/数据反馈须克制：一章内系统数值面板最多启用 1 次，数字只给大节点；其余用体感代替（气血翻涌、经脉跳动、呼吸发烫），同项数值不重复复读弹窗。
- 判定书是否适用本条：看全书体系是否含系统无障碍、数据面板、经验/属性点这类数值流设定，是则生效，否则跳过。

7. 对白短糙带情绪（正面铁律）
- 对白必须"短、糙、带情绪"：一两句收束，口语粗粝、不修边幅、带明显情绪与潜台词。
- 禁止正式书面长台词、禁止高密度"精确数字+威胁"的台词本腔；对白靠动作与停顿带语气，不靠形容词堆砌。

8. 描写减法（环境近乎归零 + 禁倒装 + 禁碎动作链）
- 环境描写近乎为零：凡与当前动作/情绪无直接因果的景物、体征、物什清单直接删净；确需的环境一笔带过，并入动作或对白。
- 严禁倒装句：禁止把动作/感情成分前置的书面倒装（如"眼前一亮的他"），一律改回主语在前的直陈。
- 严禁碎动作链：禁止把一个连续动作切成多段短句逐个断句（"他转身、抬手、抓住、用力"一节一逗）；动作链要合并成带主语的连续动作句，谁也不许单句独行。

9. 修辞词频计数自检（输出前必须跑一遍，禁令级）
- 输出前统计全章比喻/拟人次数（含"像/如/仿佛/宛如/如同"）与情绪直呼词次数：任一 ≥2 处即不合格，必须删到 ≤1 或改写。
- 统计四大禁是否出现：环境描写、倒装、碎动作链，以及（仅系统/数值流小说）数值刷屏——出现即改写后再输出。""".strip()

# ----------------------------------------------------------------------------
# 【阶段隔离·规则拆分】不同阶段只注入该阶段需要的规则：
#   GENERAL_CORE_RULES（三阶段通用总则）/ CONCEPTION_EXTRA_RULES（构思 JSON 约束）
#   WRITING_STYLE_RULES（正文行文）/ DEAI_ONLY_RULES（去AI 专用）。
# DEAI 阶段不注入 WRITING_STYLE_RULES，避免禁词表/执行流程等大块内容重复 2 遍。
# ----------------------------------------------------------------------------

DEAI_ONLY_RULES = DEAI_RULES  # 别名：DEAI_RULES 就是"去AI阶段专用"的完整规则

GENERAL_CORE_RULES = """
创作总则（架构与叙事底色，正文、大纲、设定、人物、世界观、伏笔等全纬度必须遵守本规则）

1. 冰山理论与情节结构
- 水上 1/8（明线反馈）：情节推进、核心爽点、战力/升级反馈、资源获取必须直白清晰，直接喂给读者，严禁过度隐晦。
- 水下 7/8（暗线伏笔）：深层动机、世界观隐秘、伏笔、历史创伤只露一角（通过反常行为、古籍残卷、旁人失态暗示），留给读者脑补，严禁写成设定说明书。
- 起承转合：
  - 起：直接切入冲突、危机、反常或金手指，严禁慢热铺垫与大段世界观宣讲。
  - 承：按情绪节拍推进，每个小场景均需具备微爽感或小悬念，不准空转。
  - 转：转折来自已埋伏笔或人物核心动机，严禁天降巧合。
  - 合：结尾留钩子或完成闭环升级，同时抛出新冰山一角。
- 契诃夫之枪：特写物品、特殊能力、反复提及的符号，后续剧情必须闭环回收。

2. 人物与系统设计
- 拒绝贴标签：严禁直接定义人物"冷酷/温柔/腹黑"，必须通过具体的反常动作、口是心非与微表情体现。
- 立体人设：角色有瑕疵、会纠结、动机合理，严禁全知全能的完美工具人或无脑纯反派。
- 系统人格化：从以下模板中选定一种与主角性格形成反差或互补，全书保持一致：
  (毒舌嘲讽 / 话痨跑题 / 高冷惜字 / 欠揍皮痒 / 萌新学习 / 赌徒骰子 / 社恐胆怯 / 戏精剧场 / 打工人怨念 / 老板PUA / 恋爱脑撮合 / 碎碎念老妈 / 中二病晚期 / 佛系随缘 / 杠精抬杠)

3. 爽点与伏笔运作
- 爽点打造：以"信息差碾压、战力装逼、打脸反派、逆袭翻盘"为主，配合"旁人震惊、不敢置信、事后细思极恐"进行侧面衬托。
- 伏笔晒宝：三章内让读者意识到异常细节，水下线索在爽点事件中自然展露，三章内兑现闭环。
""".strip()

WRITING_STYLE_RULES = """
正文写作规范（正文写作时执行）

1. 篇幅与段落呼吸感
- 字数：单章 2300-2500 字（中文汉字含标点），以写"事"与"行动"为主。
- 自然段落：主力段落 10-50 字（占 80%），逗号串联 1-2 个动作即收一个句号。
- 呼吸感排布：自然长段与短段交错；一段只承载一个动作或信息变化，超过三句即拆分；相邻三句同镜头同POV叙述合并；对白段一句一段是常态。
- 多以对话推动剧情，对话密度 20%–60%，根据不同剧情自然、有感情地分布。

2. 开头与结尾铁律
- 开头：直接从【时间、动作、对话、地点、事件】五选一暴力切入，严禁环境描写、心理活动或世界观介绍开场。
- 结尾：必须停留在动态动作、人物视线、脚步移动或悬念对白上；严禁抒情、升华、复盘、总结或陈述式点破答案。

3. 镜头感与对白技法
- 叙事手感：旁白是"场边嘀咕"，不是"讲台朗读"。情绪靠具体动作带出，严禁直呼情绪词（愤怒、悲伤、屈辱）。
- 对白精简：对话短而完整，富有潜台词。连续三句对白中必须穿插动作/微镜头。
- 群像区分：三人以上对话，角色之间必须在用词粗细、长短句、口癖、语气词上有明显差异。
- 去提示语：严禁使用"XX地说"，通过前后的独立动作短句自然带出说话语气。

4. 叙事口语化与网感注入
- 口语化替换参考：
  - 因此 → 所以 | 颇为 → 特别/贼/巨 | 随即 → 马上/下一秒 | 显而易见 → 说白了 | 或许 → 估计/大概 | 之/其/乃/遂 → 的/他/就是/于是
- 高频生活口语库：
  `合着、整半天、好家伙、说白了、不是……你、得了吧、拉倒吧、至于么、啥玩意、啥情况、搁这、没跑了、差不离、差不多得了、说实话、说真的、怎么说呢、你别说、还真别说`
- 精选网梗与网感词（适度点缀，不破坏剧情氛围）：
  `离谱、离大谱、属实是、破防了、蚌埠住了、好家伙、这合理吗、差不多得了、寄、润了、杀疯了、卧槽、这波血赚、绷不住了、绝了、麻了`

5. 决策链铁律（防"行为机器"，严禁违反）
- 主角遇到重大事件（穿越/系统到账/生死关/重大背叛）必须有 ≥2 句内心碎片反应（错愕/不信/快速消化），禁止无缝进入战斗或执行模式。
- 主角每个重大决策前必须有"决策瞬间"——赌什么/为什么敢/怕什么，半句也行。原主挨打七个月不敢还手、穿越者第一天就动手，中间必须补"为什么现在敢"。
- 主角主动决策每章 ≥2 次（做选择/出手/布局/拒绝），禁止全程被外力推着走。

6. 爽点公式与节奏呼吸（高潮/冲突段必备）
- 爆发段必须写足连环反应：主角体感 1 句 + 至少 2 个视角的围观反差（反派从嚣张到狼狈的对照/强者动容/围观哗然）。
- 打脸之后必须留 ≥2 拍余震（旁人议论/反派挣扎/主角补一刀）再收章，禁止爆发完直接跳收尾台词。
- 禁止纯事件播报（闷响→砖石崩开→金光铺展→敌人跌落 A/B/C/D 报完就完）——每个事件节点必须附着人的反应。
- 每 2 个冲突波之间留半段闲笔（环境一笔/小动作/一句废话），全程紧绷无喘息 = 节奏灾难；喘息段同时承担信息增量或伏笔，不是纯废笔。

7. 对白金句限流（防编剧腔）
- 金句式对白（双关/宣言/威胁句式）每章 ≤3 处，必须留给真正的关键节点。
- 每 5 段对白至少 1 句口语碎片（废话/打岔/语气词/答非所问），真人对话 30% 是废话，100% 功能性对白 = 剧本。
- 配角台词禁止全是精准数字+威胁句式的"台词本腔"。

8. 章法要求
- 每章开头尽快进入场景或事件（时间/动作/对话/地点/事件五选一暴力切入，禁环境/心理/世界观开场，见第2条）。
- 每章中段用对白和行动推进信息。
- 每章结尾落在新风险、新线索或新决策上。
- 章尾三禁：禁环境描写收尾、禁主角心理独白收尾、禁无意义配角台词收尾。钩子必须紧扣本章冲突与爽点方向。
- 不用空泛总结收尾。不用连续大段设定说明。
- 【人物出场·禁公式化排比对比（命中直接改，不许保留原句式）】
  禁"相比X，Y像A；比起B，Y又B；比起C，Y连C都没有"这种≥2次"相比/比起"连用的三段式排比（AI最爱人物出场模板）；一律改成→
  • 只给 1 个物象对比锚：「陆沉舟鞋底干净得能照出灯。陈烨的鞋缝里卡着七号街的油泥。」
  • 再给 1 个动作对比锚：「陆沉舟走路鞋跟响。陈烨走路鞋底啪嗒，踩过水洼带起半尺泥。」
  • 最多 2 条对比（不要≥3 条排比），不用"相比/比起"字样，直接摆动作/物品，读者自然能比出来。

9. 视角与信息控制铁律（严禁违反）
- **禁止上帝视角**：不得出现"他不知道，此时远在千里之外……""与此同时，另一边……"等全知叙事。单章锁定1-2个视角人物，只写视角人物能感知、能推断、能目睹的内容。
- **禁止剧透式叙述**：不得写"此时的他还不知道，这个决定将改变他的命运""他未曾料到，眼前这人日后会成为他最大的对手"等预告式点评。未来要发生的事，让未来的章节去写。
- **禁止上帝点评**：不得写"命运就是这样奇妙""冥冥之中自有天意""历史的车轮滚滚向前"等作者跳出来升华的句子。
- **禁止全知全能角色**：主角和配角都应有信息盲区。反派不能什么算计都提前料中，主角不能每件事都猜对。该被骗就被骗，该失算就失算，该走弯路就走弯路。
- **信息差即张力**：读者知道的 ≠ 主角知道的 ≠ 配角知道的。善用信息差制造"替角色着急"的代入感。可以让读者比主角多知道一点（戏剧反讽），也可以让主角比读者多藏一张底牌（悬念）。
- **伏笔隐性埋设**：伏笔必须伪装成日常细节、闲笔、环境描写、人物口头禅，不得标记"这是伏笔""此处埋线""后面会回收"。埋的时候像随手一写，回收的时候读者才恍然大悟。
- **禁止伏笔一股脑算盘写出**：不得在同一章集中铺设大量伏笔并明示其用途。伏笔分散在不同章节，自然穿插，每章最多1-2处暗线，多了就是剧透清单。
- **视角切换有界**：如需切换视角，用分场（空行+地点/人物标头）明确分隔，单场内不再跳视角。禁止同一段落内频繁切换不同人物心理。
""".strip()


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
    """正文阶段专属规则：通用核心 + 行文规范 + 去AI味消杀 + 文风类(style)技能包。
    - book 必传：用于取已持久化文风包，并按 genre_target 匹配题材。
    - 文风技能包【只注入一次】：merged_ids = 请求传参 ids ∪ book.style_skill_ids（有序并集去重），
      不再重复走 _get_enabled_style_pack 那条路径，避免同一包出现 2 遍。
    - DEAI_ONLY_RULES（去AI味与行文消杀）在正文生成阶段也注入：DEAI_RULES 首句即"写作正文时主动规避"，
      让模型从源头规避禁词/病句/副词，而非等审校阶段再做减法（审校仍会再次注入做兜底清洗）。"""
    parts = [GENERAL_CORE_RULES, WRITING_STYLE_RULES, DEAI_ONLY_RULES]
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


def build_chat_chapter_rules(book=None, skill_pack_ids=None, mode='agent',
                             extra_style_pack: str = '', extra_style_note: str = '') -> str:
    """智驾聊天里用户明确要求"写正文/写第X章/接着写"时的补充注入。

    与 build_writing_rules 同源：注入正文行文规范 WRITING_STYLE_RULES + 去AI味消杀 DEAI_ONLY_RULES + 文风类技能包，
    但【跳过 GENERAL_CORE_RULES】——因为 chat_smart 的 system prompt 已内置 GENERAL_CORE_RULES，
    这里再注入会造成同一段文字在 prompt 里出现两遍（用户感知"啰嗦重复"）。
    用于补齐 chat_smart 目前"只讨论不写正文"的缺口，保证在智驾里写正文同样命中行文/去AI硬卡。
    """
    parts = [WRITING_STYLE_RULES, DEAI_ONLY_RULES]
    style_note = ''
    book_genre = getattr(book, 'genre', None) if book is not None else None
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
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


# 智驾聊天里判定用户是否明确要求"写正文/写第X章/接着写"——命中则需注入正文行文规范
_WRITE_CHAPTER_INTENT_RE = None


def _is_write_chapter_intent(text: str) -> bool:
    """检测智驾聊天输入是否为明确的写正文意图（否则只在通用提示词下讨论，不注入行文规范）。

    覆盖：写正文 / 写第N章 / 接着写 / 续写 / 继续写 / 写一章 / 把第N章写出来 / 开始写第N章 等。
    命中返回 True；纯讨论（改设定/问走向/构思）不命中。
    """
    if not text:
        return False
    import re as _re
    t = text.strip()
    # 明确包含"写"+ 章节/正文对象，或"接着写/续写/继续写/写正文"动词
    patterns = [
        r'写\s*第\s*[0-9一二三四五六七八九十百千万]+',   # 写第N章 / 写第3卷
        r'写.{0,4}(本章|正文|这一章|下一章|一章|新章节)',   # 写正文 / 写这一章
        r'(接着|继续|往下)?(写|续写|码).{0,3}(正文|章节|本章|一章)',  # 接着写正文 / 续写本章
        r'把\s*第\s*[0-9一二三四五六七八九十百千万]+\s*章.{0,4}(写|码|续)',
        r'(开始|快?)写正文',
    ]
    return any(_re.search(p, t) for p in patterns)


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
    'SAVE_SETTING':      {'field': 'concept',        'mode': 'append', 'label': '设定'},
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

# 通用聊天关键词命中的维度key → 落地卡片类型（供圆桌总结"是否采纳到各维度"用）
_DIM_KEY_CARD = {
    'concept': 'SAVE_CONCEPT', 'key_rules': 'SAVE_RULE', 'worldbuilding': 'SAVE_WORLDSETTING',
    'plot_design': 'SAVE_OUTLINE_NODE', 'timeline': 'SAVE_PLOT', 'character_profiles': 'SAVE_CHARACTER',
    'foreshadowing': 'SAVE_FORESHADOW', 'locations': 'SAVE_LOCATION', 'style_guide': 'APPLY_STYLE',
}

# ============================================================================
# 圆桌会议"创作模式"：作者要求"按讨论结果创作各维度/某维度"时，
# 复用与"各维度生成"一致的格式（维度标签 + 卡片类型 + 生成长度上限），
# 产出标准可采纳卡片（格式与用户直接在对应维度生成时完全一致）。
# ============================================================================
_RT_CREATE_DIMS = {
    # key → (维维度标签, 卡片类型)
    'concept':            ('核心构思', 'SAVE_CONCEPT'),
    'key_rules':          ('核心规则', 'SAVE_RULE'),
    'worldbuilding':      ('世界观', 'SAVE_WORLDSETTING'),
    'character_profiles': ('人物档案', 'SAVE_CHARACTER'),
    'plot_design':        ('剧情大纲', 'SAVE_OUTLINE_NODE'),
    'timeline':           ('时间线', 'SAVE_PLOT'),
    'foreshadowing':      ('伏笔', 'SAVE_FORESHADOW'),
    'locations':          ('地点', 'SAVE_LOCATION'),
    'style_guide':        ('文风指南', 'APPLY_STYLE'),
}
# 默认"全部维度"顺序（与用户在各维度面板看到的顺序一致）
_RT_CREATE_ALL = ['concept', 'key_rules', 'worldbuilding', 'character_profiles',
                  'plot_design', 'timeline', 'foreshadowing', 'locations', 'style_guide']

# 各维度对应的 BB 字段（读已有内容 / 采纳时写入）
_RT_CREATE_FIELD = {
    'concept': 'concept', 'key_rules': 'key_rules', 'worldbuilding': 'worldbuilding',
    'character_profiles': 'character_profiles', 'plot_design': 'plot_design',
    'timeline': 'timeline', 'foreshadowing': 'foreshadowing',
    'locations': 'locations', 'style_guide': 'style_guide',
}


def _rt_parse_create_dims(text: str):
    """解析作者"按讨论结果创作…"指令，返回目标维度 key 列表。

    - 命中"全部/各/所有/多维/来一遍" → 返回全部维度（_RT_CREATE_ALL）
    - 命中具体维度关键词 → 只返回匹配的那些
    - 完全没命中创作意图 → 返回 None（由调用方走普通圆桌流程）
    """
    t = (text or '').strip()
    if not t:
        return None
    # 创作意图触发词（命中任一才进入创作模式）
    trigger = re.search(r'(?:按|根据|基于|照|把|将)?\s*讨论\s*(?:的)?\s*(?:结果|共识|结论|收获|成果|建议)?\s*(?:创作|生成|产出|起草|落地|写|做|搞)|\b创作\s*(?:各|全部|所有|相应|对应)?\s*(?:维度|设定)|按讨论结果', t)
    if not trigger:
        return None
    # 全部维度
    if re.search(r'(?:创作|生成|产出|起草|落地|写|做|搞|整)\s*(?:全部|所有|全部维度|各维度|各|多维)|各维度|全部维度', t):
        return list(_RT_CREATE_ALL)
    # 具体维度关键词
    kw_map = [
        ('concept', r'(?:核心?构思|核心创意|点子|logline|一句话故事)'),
        ('key_rules', r'(?:核心规则|核心设定|力量体系|规则|设定)'),
        ('worldbuilding', r'(?:世界观|世界设定|世界背景)'),
        ('character_profiles', r'(?:人物档案|人物设定|人物|角色)'),
        ('plot_design', r'(?:剧情大纲|全书大纲|大纲|五幕)'),
        ('timeline', r'(?:剧情线|时间线|剧情|情节|卷剧情)'),
        ('foreshadowing', r'(?:伏笔|埋线)'),
        ('locations', r'(?:地点|地图|地图设定|场景)'),
        ('style_guide', r'(?:文风|文风指南|行文风格|写作风格)'),
    ]
    hit = []
    for k, pat in kw_map:
        if re.search(pat, t):
            if k not in hit:
                hit.append(k)
    return hit  # 可能为空列表（命中创作意图但没识别出具体维度 → 由调用方按"全部"兜底）


def _rt_general_dim_request(text: str):
    """通用聊天自然语：识别"明确要求生成某维度"的指令。

    与 _rt_parse_create_dims（圆桌"按讨论结果创作")不同，这里是通用聊天里作者直接用自然语要求
    生成某一维度长内容（"帮我生成世界观/主角/大纲/伏笔/文风…"）。命中返回目标维度 key 列表。
    - 明确要求"全部/所有维度" → 返回全部核心维度
    - 命中具体维度关键词 → 只返回匹配的
    - 非明确生成指令 → 返回 None（走普通聊天，不注入维度格式，避免打断闲聊）
    """
    t = (text or '').strip()
    if not t or len(t) < 3:
        return None
    # 必须有"生成/创作/写...设定/内容/维度"等明确动作词 + 一个维度目标，才是明确生成指令
    _act = re.search(r'(?:帮我|请|替我|为(?:我|这本书))?\s*(?:生成|创作|产出|起草|草拟|制定|编排|设计|写|建立|搭|规划|形成|整理)?\s*(?:一份|一套|一个)?\s*(?:完整的|详细的|全面的)?', t)
    if not _act:
        return None
    # 动作词必须确凿（常见 AI 违例词首字符）——否则如"帮我看看"不触发
    if not re.search(r'(?:生成|创作|产出|起草|草拟|制定|编排|设计|建立|搭|规划|形成|整理)\s*(?:一份|一套|一个|完整的|详细的|全面的)?', t):
        return None
    # 明确"全部/所有维度"
    if re.search(r'(?:全部|所有|各|多|一系列|整\s*套)\s*(?:维度|设定|内容|方案)', t):
        return list(_RT_CREATE_ALL)
    # 具体维度关键词（从强到弱，取命中数最多/最高置信）
    kw_map = [
        ('concept', r'(?:核心?构思|创意|logline|卖点|一句话(?:故事|梗))'),
        ('key_rules', r'(?:核心规则|核心设定|力量体系|规则|设定)'),
        ('worldbuilding', r'(?:世界观|世界设定|世界背景|世界架构)'),
        ('character_profiles', r'(?:人物档案|人物设定|人物|角色|主角|配角|反派)'),
        ('plot_design', r'(?:全书大纲|剧情大纲|大纲|分卷大纲)'),
        ('timeline', r'(?:剧情线|时间线|情节|卷剧情|剧情)'),
        ('foreshadowing', r'(?:伏笔|埋线|伏?笔)'),
        ('locations', r'(?:地点|地图|区域|场景设定|地理)'),
        ('style_guide', r'(?:文风|行文风格|写作风格|叙事风格|语言风格|文风指南)'),
    ]
    # 若动作目标是"维度"但用户直接说"生成世界观/写男主"这类（维度词紧跟动作词且无额外话题）→ 命中
    hit = []
    for k, pat in kw_map:
        if re.search(pat, t):
            if k not in hit:
                hit.append(k)
    if not hit:
        return None
    return hit  # 明确生成指令且命中具体维度 → 返回


def _rt_create_dimension_system(dim_key: str, book, iron: str, consensus: str, existing: str) -> str:
    """构造圆桌"创作模式"某维度的 system prompt。

    复用/对齐与"各维度生成"一致的格式要求（concept/key_rules/worldbuilding/
    character_profiles/plot_design 走详实分节铁律；timeline 走 JSON 卷数组；
    style_guide/foreshadowing/locations 走结构化纯文本），
    以圆桌讨论共识为核心素材，保证产出内容和直接在该维度生成时的格式一致。
    """
    parts = []
    parts.append('你是资深网文创作副驾。现在要根据一场"圆桌专家讨论"得出的共识，为一部小说创作一个维度设定。')
    parts.append('必须以圆桌共识为唯一取材依据，内容要具体可落地、可直接采纳进对应维度，禁止"待设定/后续再定"等空话。')
    if iron:
        parts.append(iron)
    parts.append(f'\n【圆桌讨论共识（唯一取材来源）】\n{consensus[:14000]}')
    if existing:
        parts.append(f'\n【已有该维度内容（可在其基础上按共识完善，不要简单重复）】\n{existing[:2000]}')
    _book_part = f'为小说《{book.title}》' if book else '为这部小说'
    parts.append(f'\n请{_book_part}产出下列维度的完整内容。')

    d = dim_key
    if d == 'concept':
        parts.append('''\n【核心构思铁律·禁止两句话】必须输出 10 节：
①一句话故事核Logline ②主题曲线(起点→反诘→抉择→终局) ③核心冲突三角(主角×对手×世界规则)
④目标分层(短/中/长/终极+失败代价) ⑤核心爽感机制(3-5种主爽点+触发→爆发→余波+卷1/3/5/终局排布)
⑥金手指/外挂(类型+核心能力+分级+硬约束代价+贴合执念+终极风险)
⑦主角魅力公式(记忆符号+三重反差+具体创伤+核心执念) ⑧对手/反派魅力(前/中/终局三级反派)
⑨世界观3-5个独特卖点钩子 ⑩全书情感底色+读者定位+文风力向。
总字数不少于 1200 字，每节必须写具体可落地内容。''')
    elif d == 'key_rules':
        parts.append('''\n【设定铁律·禁止只写境界表】必须输出 11 节：
①力量总体系(2主1辅+克制) ②等级阶梯表(命名+战力差+突破门槛+社会地位+寿元) ③至少2主1偏的提升路径
④功法/技能树(5类分级+代表性技能+配搭+获取) ⑤资源与货币体系(通用货币+等价物+10项价格表+产地)
⑥装备/法宝/载具 ⑦至少2-3种副职业 ⑧硬约束+反噬代价 ⑨种族/职业/阵营总表+矛盾 ⑩至少8条世界硬规则禁忌
⑪文明水平总览。总字数不少于 1500 字，必须有具体数字和例子。''')
    elif d == 'worldbuilding':
        parts.append('''\n【世界观铁律·禁止只写四大域】必须输出 15 节（最少 2000 字）：
①世界总览 ②创世元史三段+至少3纪元大事 ③至少6大地理分块 ④气候天象体系 ⑤至少8大主要势力
⑥完整阶级金字塔 ⑦政治律法 ⑧经济贸易 ⑨至少5个智慧种族 ⑩至少2正统+1邪教宗教信仰
⑪语言文字度量衡历法 ⑫风俗礼仪服饰饮食建筑 ⑬军事体系 ⑭交通通讯 ⑮至少5个世界未解之谜/禁忌之地。''')
    elif d == 'character_profiles':
        parts.append('''\n【人物铁律·禁止只给姓名+一句话身份】至少写出 主角 + 1女主/重要女配 + 2核心配角 + 1前期反派 + 1中期反派：
每个角色按 15 项写满（姓名/性别年龄/外貌特征含记忆符号/身份地位/性格三原色/核心价值观/人生三目标/
深层动机/具体核心创伤/恐惧软肋/能力体系/战斗风格/背景故事/关键关系网/角色弧线）。
每个角色至少 300 字，合计不少于 1800 字；纯中文按字段分行输出。''')
    elif d == 'plot_design':
        parts.append('''\n【大纲铁律·禁止只写几句话五幕】必须写满：
五幕(立身/立足/立势/立威/立命)对应到连续卷号 + 每卷6项指标
(①本卷爽点4小1大 ②人物方向 ③地点动线 ④修炼/事业/财富/关系/势力五项进展 ⑤伏笔主题方向 ⑥卷尾得到/失去/新任务)；
结尾附【跨卷尾钩子承接总览】。总字数不少于 1500 字。''')
    elif d == 'timeline':
        parts.append('''\n【剧情线铁律】输出按卷组织的 JSON 数组（可直接写入剧情线维度）：
每条卷对象含：卷名 volume、卷概要 summary(150-250字)、卷主线 main_plot(100-160字)、核心冲突 core_conflict、
卷尾钩子 ending_hook、主要剧情事件 main_events[]（含 title/summary/bury/payoff）、情节节点 nodes[]（含 title/summary/chapters/type/爽点）。
卷数必须与核心参数铁律一致，卷与卷之间尾钩自然承接。只输出 JSON，不要 markdown 代码块。''')
    elif d == 'foreshadowing':
        parts.append('''\n【伏笔铁律】按伏笔分组输出，每组：①伏笔名 ②埋设章/幕（含具体场景） ③引出的事件
④预期回收章/幕 ⑤回收效果 ⑥状态（已埋/待收/已收）。覆盖主线、支线、人物、世界观几类，列表清晰可采纳。''')
    elif d == 'locations':
        parts.append('''\n【地点铁律】按地图/场景分组输出，每组：①地点名 ②隶属（地域/势力） ③地理描述
④功能用途 ⑤关键剧情事件标注 ⑥出入限制/危险。至少覆盖主线涉及的核心场景，结构清晰。''')
    elif d == 'style_guide':
        parts.append('''\n【文风铁律】输出结构化文风指南，包含：①叙事视角与口吻 ②节奏控制（爽点/铺垫/爆点的章内排布）
③描写偏好（环境/动作/心理/对话比例） ④修辞与语言调性 ⑤禁用习惯（AI味/书面腔/水字数句式）
⑥对标参考风格。做成可直接指导写作的可执行规范。''')
    else:
        parts.append('\n【输出要求】结构化分节输出该维度完整设定，具体可采纳。')
    parts.append('\n【排版】使用纯中文 markdown 层级输出，去掉 * 与 # 装饰符号。')
    return '\n'.join(parts)

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


def build_chat_system_prompt(book, bb, recent_chapters: list = None, next_chapter_num: int = None, toc_block: str = None,
                            rank_scan: dict | None = None) -> str:
    """构建维度感知的聊天 system_prompt。

    注入当前书的全部 bible 维度 + 章节目录 + Action Card 使用说明 + 创作进度。
    可选 rank_scan：榜单风向扫榜情报，追加为"本轮市场风向执行要求"。
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
            ('核心构思', 'concept', 2600),
            ('世界观', 'worldbuilding', 2600),
            ('核心规则', 'key_rules', 1600),
            ('人物档案', 'character_profiles', 3200),
            ('大纲', 'plot_design', 2600),
            ('剧情时间线', 'timeline', 1800),
            ('伏笔', 'foreshadowing', 1200),
            ('地点', 'locations', 1000),
            ('文风指南', 'style_guide', 1000),
        ]
        filled = []
        empty = []
        for label, field, cap in dims:
            val = (getattr(bb, field, '') or '').strip()
            if val:
                # 人物维度：JSON 数组转自然语言，避免 AI 模仿 JSON 格式
                if field == 'character_profiles' and val.startswith('['):
                    val = _character_profiles_to_text(val)
                # 系统 prompt 9 维度注入字符硬上限（避免单维度 10K+ 导致 3 轮对话就撞 LLM ctx 上限）
                if len(val) > cap:
                    # 先在语义分界处（双换行/句号）截断，不破坏结构
                    cut = val[:cap]
                    last_break = max(cut.rfind('\n\n'), cut.rfind('。'), cut.rfind('\n'))
                    val = (cut[:last_break] if last_break > cap // 2 else cut) + f'\n…（已截前{cap}字，完整落地维度请查创作界面，或引用前言精准索取）'
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

    # P0 榜单风向：如果本轮有扫榜情报（match_categories/hot_elements/openings/landmines/title_formulas/top_books），
    # 作为最终段注入。让智驾的通用聊天与副驾全部能自动对齐市场。
    _rank_ctx = _format_rank_context(rank_scan)
    if _rank_ctx:
        parts.append('\n' + _rank_ctx)

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
    # P1-1 会话级切模型：请求体 ai_config_id > 会话 meta_json.ai_config_id > 全局激活
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    # P1-3 内置角色 persona：default/polish/toxic_critic/architect/worldbuilder/marketeer/interviewer
    req_role_id = (data.get('role_id') or '').strip() or None
    # 持久化在会话级 meta_json.role_id（下次沿用，除非用户切）
    scope = data.get('scope', 'general')
    # P0 榜单风向：前端在聊智驾前先扫榜，把扫榜报告 rank_scan 塞进来；我们注入到 system_prompt 和所有落地卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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
    system_prompt = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block, rank_scan=_rank_scan)

    # ===== 写正文意图·注入正文行文规范 =====
    # chat_smart（维度感知聊天）默认只注入 GENERAL_CORE_RULES，不注入 WRITING_STYLE_RULES（设计见
    # build_chat_system_prompt。但当用户明确要"写第X章/写正文/接着写"时，若仍不注入行文规范，
    # 产出的 SAVE_CHAPTER 正文卡会在没有任何行文/去AI硬卡约束下自由发挥 → AI特征居高不下。
    # 这里命中写作意图则同源注入 build_chat_chapter_rules（WRITING_STYLE_RULES+文风技能包，跳过
    # 已重复的 GENERAL_CORE_RULES），与正文 Tab(chat_smart_action) / ai_continue 保持一致。
    if _is_write_chapter_intent(message):
        try:
            _chat_chapter_rules = build_chat_chapter_rules(book, mode='agent')
            if _chat_chapter_rules:
                system_prompt = system_prompt + '\n\n' + _chat_chapter_rules
        except Exception:
            pass  # 注入失败不阻断主流程，退回通用聊天提示词

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
    # P1-1 会话级切模型：优先级 req_ai_config_id > session.meta_json.ai_config_id > 全局激活
    session_cfg_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            session_meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            session_cfg_id = (session_meta.get('ai_config_id') or '').strip() or None
    except Exception:
        session_cfg_id = None
    chosen_cfg_id = req_ai_config_id or session_cfg_id
    cfg = AIConfig.get_by_id(chosen_cfg_id) if chosen_cfg_id else None
    if cfg and not cfg.api_key:
        cfg = None  # 指定配置但无key → 回退全局
    if cfg is None:
        cfg = AIConfig.get_active()
    # chat_smart（维度感知聊天链路）：归一化URL（防智谱GLM 404/HTTP 500）
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    # 把当前选择持久化到 session.meta_json（保证下一轮聊天沿用同一模型，即会话级锁定）
    if chosen_cfg_id and chosen_cfg_id == cfg.id and session:
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict): meta = {}
            if meta.get('ai_config_id') != cfg.id:
                meta['ai_config_id'] = cfg.id
                session.meta_json = json.dumps(meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass  # 持久化失败不阻断主流程
    # chat_smart 统一过 URL 归一化（智谱/v4必须走 _normalize_llm_base_url，否则会撞 /v4/v1）
    import os as _os_cs1
    from llm_gateway import _normalize_llm_base_url as _nl1
    import app as _mod1
    try:
        _act1 = _mod1.AIConfig.get_active()
        _act1_id = getattr(_act1, 'id', None) if _act1 else None
    except Exception:
        _act1_id = None
    _is_act1 = (_act1_id and chosen_cfg_id and _act1_id == chosen_cfg_id) or (not chosen_cfg_id)
    if _is_act1:
        _b1, _k1, _m1 = get_llm_config(_mod1)
        if cfg.model and cfg.model != _m1:
            _m1 = cfg.model
    else:
        _b1 = _nl1(cfg.base_url or _os_cs1.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1'), cfg.model)
        _k1 = cfg.api_key or _os_cs1.environ.get('USER_LLM_API_KEY', '')
        _m1 = cfg.model or _os_cs1.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    gw = LLMGateway(_b1, _k1, _m1)

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
                if _is_stream_retry(chunk):
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": chunk.info}, ensure_ascii=False)}\n\n'
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
                _enrich_card_rank_meta(card, _rank_scan)
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            # 持久化对话（剥离卡片标记后存历史，cards 单独存以便历史会话恢复）
            clean_text = _clean_text_to_plain(strip_cards(complete))
            # 卡片持久化时标记为 pending，前端历史会话加载后可继续采纳
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending',
                                'rankSourceLabel': c.get('rankSourceLabel') or '',
                                'subtitle': c.get('subtitle') or ''} for c in cards]
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

    # ====== 空判断/ctype校验 拆成独立分支，报错更精确，避免一刀切"无效的卡片或内容为空"排查困难 ======
    if not ctype:
        return jsonify({'error': '无效的卡片：缺少卡片类型(type)字段。请检查前端传入的 action card 结构。'}), 400
    if ctype not in CARD_REGISTRY:
        _valid = ', '.join(sorted(CARD_REGISTRY.keys()))
        return jsonify({'error': f'无效的卡片类型"{ctype}"（不在系统CARD_REGISTRY白名单）。有效类型：{_valid}。'}), 400
    if not content:
        return jsonify({'error': '卡片内容为空(quick_fill未传递或解析失败)：命中气泡的方案内容未正确填充到card.content，后端无法落地。'}), 400

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
                _role_raw = char_data.get('role') or ('protagonist' if '主角' in (title or '') or '主角' in content else 'supporting')
                _role = _normalize_character_role(_role_raw, title, content)
                _desc = char_data.get('identity') or ''
                if _role_raw != _role:
                    # 长角色定位并入 description（Text 无长度限制），保证内容不丢
                    _desc = (_desc + '\n' + _role_raw).strip() if _desc else _role_raw
                db.session.add(Character(
                    book_id=book_id,
                    name=_clean_character_name(char_data.get('name')) or '未命名',
                    role=_role,
                    description=_desc,
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

                def _volume_field_nonempty(v):
                    """判断卷字段是否"有有效值"：空字符串/空列表/None/false/零 volume_index 不算。"""
                    if v is None: return False
                    if isinstance(v, str): return bool(v.strip())
                    if isinstance(v, (list, tuple, set, dict)): return len(v) > 0
                    if isinstance(v, bool): return v
                    if isinstance(v, (int, float)):
                        # volume_index=0 视为空，其他数值有效
                        return v != 0
                    return True  # 未知类型按非空处理

                def _merge_volume(old_v: dict, new_v: dict) -> dict:
                    """同卷合并：仅用 NEW 中非空字段覆盖旧字段，其余卷级字段一律从 OLD 保留。
                       节点设计卡片（NEW 只带 nodes）不应把卷的 main_plot/core_conflict/main_events 等抹空。
                       同时保证向后兼容：summary → main_plot，end_hook → ending_hook。"""
                    if not isinstance(new_v, dict):
                        return new_v
                    # 所有已知卷级字段：NEW 有非空就用 NEW，否则从 OLD 继承
                    VOL_FIELDS = (
                        'volume_id', 'volume', 'volume_title', 'volume_index',
                        'summary', 'main_plot', 'core_conflict', 'plot_summary',
                        'ending_hook', 'end_hook', 'ending',
                        'main_events', 'nodes', 'chapter_beats',
                        'characters', 'timeline_anchor', 'location', 'locations',
                        'realm_change', 'age_change', 'target_audience',
                        'bury', 'payoff', 'cool_type', 'cool_level',
                        'state', 'status', 'progress', 'notes',
                    )
                    merged: dict = {}
                    old_is_dict = isinstance(old_v, dict)
                    for k in VOL_FIELDS:
                        new_val = new_v.get(k)
                        old_val = old_is_dict and old_v.get(k)
                        # NEW 有非空有效值 → 优先 NEW；否则 OLD（若是dict）→ 否则跳过
                        if _volume_field_nonempty(new_val):
                            merged[k] = new_val
                        elif _volume_field_nonempty(old_val):
                            merged[k] = old_val
                    # 保留 NEW 中额外未知自定义字段（但仅当 OLD 里没有，避免覆盖未知保留字段）
                    for k, vv in new_v.items():
                        if k in VOL_FIELDS:
                            continue
                        if k not in merged:
                            merged[k] = vv
                    # 保留 OLD 中额外未知保留字段（NEW 未声明）避免被擦除
                    if old_is_dict:
                        for k, vv in old_v.items():
                            if k not in merged:
                                merged[k] = vv
                    # 向后兼容：summary → main_plot（旧代码/旧 UI 只认 main_plot）
                    if (not merged.get('main_plot') or not str(merged['main_plot']).strip()) and merged.get('summary'):
                        merged['main_plot'] = str(merged['summary'])
                    # 核心冲突兜底：用 main_plot 的首 200 字再撑一下
                    if not merged.get('core_conflict') or not str(merged['core_conflict']).strip():
                        merged['core_conflict'] = str(merged.get('main_plot') or '')[:200]
                    # 结尾钩子兜底：end_hook → ending → old.ending_hook
                    if not merged.get('ending_hook'):
                        merged['ending_hook'] = merged.get('end_hook') or merged.get('ending') or (old_is_dict and old_v.get('ending_hook')) or ''
                    # main_events 兜底：保证是数组，元素至少含 index/title/summary
                    me = merged.get('main_events')
                    if isinstance(me, list):
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
                    else:
                        merged['main_events'] = []
                    # nodes 兜底：保证数组
                    if not isinstance(merged.get('nodes'), list):
                        merged['nodes'] = []
                    # volume_index 绝不能为空
                    if merged.get('volume_index') in (None, ''):
                        merged['volume_index'] = old_is_dict and old_v.get('volume_index') or _extract_volume_index_safe(merged) or 1
                    try:
                        merged['volume_index'] = int(float(merged['volume_index']))
                    except (TypeError, ValueError):
                        merged['volume_index'] = 1
                    if not merged.get('volume'):
                        merged['volume'] = f"第{merged['volume_index']}卷"
                    return merged

                # ===== 节点设计 A+C 门禁：无论 LLM 按什么粒度/格式输出 nodes，
                # 统一过 _repair_nodes_to_one_ch_per_node 保证：
                #   · 单章单节点 · 无重叠无跳章无越界 · 节点数=本章数
                # 先根据 volume_index 反推"该卷所在章节区间"，再对每个卷的 nodes 做后置修复。
                def _repair_volume_nodes_safe(nv: dict) -> dict:
                    try:
                        if not isinstance(nv, dict):
                            return nv
                        nodes = nv.get('nodes')
                        if not isinstance(nodes, list) or not nodes:
                            return nv
                        # 优先从 cpv / chapter_count / volume_index + 全局50默认推导起止章
                        cpv = None
                        for k in ('chapter_count', 'cpv', 'chapters_per_volume', 'chapters_count'):
                            v = nv.get(k)
                            if isinstance(v, (int, float)) and v > 0:
                                cpv = int(v); break
                        if cpv is None:
                            # 尝试从已有节点里取最大 chapters 作为上界（不小于）
                            mx = 0
                            from node_design_bp import _parse_chapters_field
                            for nd in nodes:
                                rng = _parse_chapters_field(nd.get('chapters') if isinstance(nd, dict) else None)
                                if rng and rng[-1] > mx: mx = rng[-1]
                            if mx > 0:
                                # 只做"不小于当前最大chapters"的 cpv 估计：用全局50默认或用mx本身
                                cpv = mx
                        if cpv is None or cpv <= 0:
                            cpv = 50  # 默认兜底：每卷50章（和 node_design_bp cpv 默认一致）
                        # 推导 start_ch：若卷内已含 chapters 且连续最小=X且X>1，说明非首卷；否则用volume_index推
                        vi = None
                        for k in ('volume_index', 'volume_idx', 'vol_index'):
                            v = nv.get(k)
                            if isinstance(v, (int, float)):
                                vi = int(v); break
                        if vi is None:
                            vi = _extract_volume_index_safe(nv) or 1
                        # 估计 start_chapter：首章起点=1+(vi-1)*cpv
                        start_ch = 1 + (vi - 1) * cpv
                        # 若已有节点覆盖到>end_ch的章节或首章明显小了，用nodes实际区间收缩
                        real_min, real_max = None, None
                        try:
                            from node_design_bp import _parse_chapters_field
                            for nd in nodes:
                                rng = _parse_chapters_field(nd.get('chapters') if isinstance(nd, dict) else None)
                                if not rng:
                                    continue
                                if real_min is None or rng[0] < real_min:
                                    real_min = rng[0]
                                if real_max is None or rng[-1] > real_max:
                                    real_max = rng[-1]
                        except Exception:
                            real_min = real_max = None
                        if real_min and real_min > start_ch:
                            start_ch = real_min
                        end_ch = start_ch + cpv - 1
                        if real_max and real_max > end_ch:
                            end_ch = real_max
                        # 修复：节点按单章粒度重排
                        from node_design_bp import _repair_nodes_to_one_ch_per_node
                        repaired, _ = _repair_nodes_to_one_ch_per_node(nodes, start_ch, end_ch, me_index=vi, index_offset_start=0)
                        # 给节点重新编 index
                        for idx, nd in enumerate(repaired):
                            if isinstance(nd, dict) and not nd.get('index'):
                                nd['index'] = idx + 1
                        nv['nodes'] = repaired
                        nv['chapter_count'] = cpv
                        if not nv.get('start_chapter'):
                            nv['start_chapter'] = start_ch
                        if not nv.get('end_chapter'):
                            nv['end_chapter'] = end_ch
                        return nv
                    except Exception:
                        # 修复失败不影响落卡（原内容保留，避免因为修复器bug导致无法采纳）
                        return nv

                def _merge_volume_nodes_incremental(old_vol: dict, new_vol: dict) -> tuple[dict, bool]:
                    """续会/单章修改时的节点增量合并：按 chapters 章号去重。
                    策略：
                      1) 若 OLD 无 nodes → 返回 NEW.nodes（什么都不做）；无增量行为。
                      2) 把 OLD / NEW 节点都展开成 {ch: node} 映射（单章粒度）；
                         NEW 命中的章覆盖 OLD；OLD 没被 NEW 命中的章一律保留。
                      3) 按章节号排序，重建 nodes 列表 + index 连续。
                    返回 (merged_vol, merged)，merged=True 说明发生了增量合并。
                    """
                    try:
                        if not isinstance(old_vol, dict) or not isinstance(new_vol, dict):
                            return new_vol, False
                        old_nodes = old_vol.get('nodes')
                        new_nodes = new_vol.get('nodes')
                        if not isinstance(old_nodes, list) or not old_nodes:
                            return new_vol, False
                        if not isinstance(new_nodes, list) or not new_nodes:
                            return new_vol, False
                        from node_design_bp import _parse_chapters_field
                        def _expand(nodes_list: list) -> dict[int, dict]:
                            res: dict[int, dict] = {}
                            for nd in nodes_list:
                                if not isinstance(nd, dict):
                                    continue
                                chs = _parse_chapters_field(nd.get('chapters'))
                                if not chs:
                                    continue
                                # 单章单节点门禁：chs 长度>1（LLM把多章合并写1节点）→ 展开给每个章都挂一个浅拷贝
                                if len(chs) == 1:
                                    res[int(chs[0])] = nd
                                else:
                                    for c in chs:
                                        d = dict(nd)
                                        d['chapters'] = int(c)
                                        res[int(c)] = d
                            return res
                        old_map = _expand(old_nodes)
                        new_map = _expand(new_nodes)
                        if not new_map:
                            return new_vol, False
                        # NEW 命中章 → 覆盖 OLD；OLD 保留 NEW 没命中的
                        merged_map: dict[int, dict] = {}
                        merged_map.update(old_map)
                        merged_map.update(new_map)
                        # 按章号升序重建 nodes，重写 index
                        rebuilt: list[dict] = []
                        for i, ch in enumerate(sorted(merged_map.keys())):
                            nd = dict(merged_map[ch])
                            nd['chapters'] = int(ch)
                            nd['index'] = i + 1
                            rebuilt.append(nd)
                        new_vol['nodes'] = rebuilt
                        # 同步 chapter_count/start_chapter/end_chapter（避免增量后写回仍然 cpv=50 但 nodes 实际只有后半段）
                        if rebuilt:
                            chs_sorted = sorted(merged_map.keys())
                            s, e = int(chs_sorted[0]), int(chs_sorted[-1])
                            new_vol['start_chapter'] = s
                            new_vol['end_chapter'] = e
                            # chapter_count 只在 NEW 没明确指定时，保持 OLD 原值（不把 OLD.chapter_count=50 收缩）
                            if not _volume_field_nonempty(new_vol.get('chapter_count')) and _volume_field_nonempty(old_vol.get('chapter_count')):
                                new_vol['chapter_count'] = old_vol['chapter_count']
                        return new_vol, True
                    except Exception:
                        # 合并失败不中断，按 NEW.nodes 直接覆盖（不吞掉用户的采纳）
                        return new_vol, False

                # 按 volume_index upsert（覆盖同卷时走 _merge_volume 保字段）
                for nv in new_vols:
                    if not isinstance(nv, dict):
                        continue
                    # 修复节点 A+C：单章单节点 · 无重叠无跳章 · 50章/卷
                    if isinstance(nv.get('nodes'), list) and nv['nodes']:
                        nv = _repair_volume_nodes_safe(nv)
                    nv_idx = nv.get('volume_index') or _extract_volume_index_safe(nv)
                    matched = False
                    for i, ev in enumerate(existing_vols):
                        if not isinstance(ev, dict):
                            continue
                        ev_idx = ev.get('volume_index') or _extract_volume_index_safe(ev)
                        if str(ev_idx) == str(nv_idx):
                            # 增量合并 nodes（续会/单章修改 卡片只带部分章 → 新覆盖老+老保留新未命中）
                            nv, _ = _merge_volume_nodes_incremental(ev, nv)
                            # 增量合并后：再次跑 A+C 门禁修复（保证最终入库仍然单章单节点+无重叠+无跳章）
                            if isinstance(nv.get('nodes'), list) and nv['nodes']:
                                nv = _repair_volume_nodes_safe(nv)
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

    # ====== 新增：落卡成功后写入对应AISession的记忆（system消息），下次聊天LLM知道已落卡 ======
    # 根因：用户反馈"落卡了但继续聊天，LLM完全不知道我已经落过卡"，
    # 因为之前 apply-card 只改 bible + 卡片 status，没有把落卡这件事写到 session.messages_history，
    # 导致 chat_general/chat_smart 下一轮构造 messages 时完全没有「已落卡」这一条关键事实。
    if session_id:
        try:
            sess_obj = AISession.query.get(session_id)
            if sess_obj:
                spec_label = spec.get('label') or ctype
                # 摘要：只取标题+前150字，避免单条记忆塞太长
                preview = content[:150].replace('\n', ' ')
                if len(content) > 150:
                    preview += '…'
                mem_text = (
                    f'【系统上下文·落卡成功通知（无需对用户复述，仅作内部记忆参考）】\n'
                    f'时间：{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}\n'
                    f'落地维度：{spec_label}（字段={spec.get("field","")}，卡片类型={ctype}）\n'
                    f'卡片标题：{title or "(未命名卡片)"}\n'
                    f'落地模式：{"覆盖（编辑后落地）" if is_edit_overwrite else "追加（直接采纳）"}\n'
                    f'内容摘要：{preview}\n'
                    f'→ 以后聊到相关内容时，请以此为"智驾已写入"的事实依据，不要重复从零讨论已落过卡的相同话题。'
                )
                sess_hist = load_session_messages(sess_obj)
                sess_hist.append({'role': 'system', 'content': mem_text, '_source': 'card_applied_memory'})
                _safe_save_session_messages(sess_obj, sess_hist)
        except Exception as _me:
            # 记忆写入失败不影响主流程（卡片落地成功才是硬指标），记录但不抛错
            import traceback as _tb
            _tb.print_exc()

    return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                    'progress': build_progress_map(bb),
                    **result_extra})


def _split_multi_names_from_title(title):
    """从描述性档案标题中提取多人名列表。

    例："核心人物档案：顾晨、顾曦与赵阔" → ['顾晨','顾曦','赵阔']
    非描述性标题（纯人名）返回 []。
    """
    if not title:
        return []
    t = title.strip()
    m = re.search(r'(?:档案|人物|角色|群像|人设)(?:介绍|设定|合集)?\s*[:：]\s*(.+)$', t)
    if not m:
        m = re.search(r'[:：]\s*(.+)$', t)
    if not m:
        return []
    names_part = m.group(1).strip()
    parts = [p.strip() for p in re.split(r'[、，,/]|\s*和\s*|\s*与\s*', names_part) if p.strip()]
    # 过滤明显不是人名的超长片段
    parts = [p for p in parts if len(p) <= 20]
    return parts if len(parts) >= 2 else []


def _clean_character_name(name):
    """人物名清洗：描述性档案标题（如"核心人物档案：顾晨、顾曦与赵阔"）→ 提取名字区；
    超长 → 截断到 50（对齐生产库 characters.name varchar(50)，防止落库 StringDataRightTruncation）。"""
    name = (name or '').strip()
    if not name:
        return ''
    if re.search(r'(?:档案|人物|角色|群像|人设)', name):
        names = _split_multi_names_from_title(name)
        if names:
            return names[0]
        m = re.search(r'[:：]\s*(.+)', name)
        if m:
            name = m.group(1).strip()
    # 防御：name 中不允许出现换行（会拼出 "未命名\n姜雪" 脏名）
    name = name.split('\n', 1)[0].split('\r', 1)[0].strip()
    return name[:50]


def _normalize_character_role(role_raw, title='', content=''):
    """角色定位归一化：characters.role 是 varchar(50) 枚举语义（protagonist/antagonist/supporting）。

    LLM 卡片"角色：xxx"常给一句长描述（如"前期的核心资源提款机与脑补反差源…"），
    直接落库会触发 StringDataRightTruncation。超长时按上下文归类为简短枚举，
    完整描述由调用方并入 description（Text 无长度限制），保证内容不丢。
    """
    role = (role_raw or '').strip()
    if len(role) <= 50:
        return role
    ctx = role + ' ' + (title or '') + ' ' + (content or '')
    if '反派' in ctx or '敌' in ctx or '对手' in ctx or 'boss' in ctx.lower() or '最大威胁' in ctx:
        return 'antagonist'
    if '主角' in ctx:
        return 'protagonist'
    return 'supporting'


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

    # 策略2.5：标题含多人名（如"核心人物档案：顾晨、顾曦与赵阔"）→ 按【人名】/人名：/人名行切块
    names_from_title = _split_multi_names_from_title(title)
    if len(names_from_title) >= 2:
        pname_re = re.compile(r'^(?:【|\[)?(' + '|'.join(re.escape(n) for n in names_from_title) + r')(?:】|\])?[:：]?\s*')
        blocks_n = []
        cur_n = []
        for line in lines:
            if pname_re.match(line.strip()) and cur_n:
                blocks_n.append('\n'.join(cur_n).strip())
                cur_n = []
            cur_n.append(line)
        if cur_n:
            tail_n = '\n'.join(cur_n).strip()
            if tail_n:
                blocks_n.append(tail_n)
        if len(blocks_n) >= 2:
            result = []
            for blk in blocks_n:
                parsed = _parse_character_card('', blk)
                first_line = blk.split('\n', 1)[0].strip()
                mm = pname_re.match(first_line)
                if mm:
                    parsed['name'] = mm.group(1)
                if parsed.get('name') and parsed['name'] != '未命名':
                    result.append(parsed)
            if len(result) >= 2:
                return result

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
      4) 【Silly Tavern 角色卡导入】标准套版：
         【角色名】 / 【性格/人格】 / 【外貌/背景描述】 / 【所处剧情场景/当前局面】 /
         【对白示例】 / 【角色开场第一句话/动作】 / 【创作者备注】
      5) 纯文本：首行/标题为姓名，其余为性格
    """
    fields = ['name', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items']
    # 字段关键词映射（支持中文标签 + Silly Tavern 导入专用标签）
    key_map = {
        'name':        ['姓名', '名字', '名称', '角色名'],
        'role':        ['角色', '定位', '角色定位'],
        'identity':    ['身份', '职业'],
        'personality': ['性格', '个性', '人格'],
        'motivation':  ['动机', '目的'],
        'background':  ['背景', '来历', '外貌', '外貌/背景描述', '背景描述', '外貌描述'],
        'relationships': ['关系', '人际关系', '所处剧情场景', '当前局面', '所处剧情场景/当前局面'],
        'abilities':   ['能力', '技能', '金手指', '对白示例', '说话风格', '对白风格'],
        'items':       ['物品', '装备', '持有物'],
        # 额外字段（用 result_extra 承载，不与标准 8 字段混用，避免覆盖）
        '__extra_first_line': ['角色开场第一句话', '角色开场第一句话/动作', '开场第一句'],
        '__extra_notes':      ['创作者备注', '备注', '作者备注'],
        '__extra_source_fn':  ['Silly Tavern 角色卡源文件名', '源文件名'],
    }
    result = {f: '' for f in fields}
    result['name'] = _clean_character_name(title)
    result['role'] = ''
    result_extra: dict = {}

    text = content.strip()
    # 策略1：| 分隔
    if '|' in text and '\n' not in text:
        parts = [p.strip() for p in text.split('|') if p.strip()]
        for i, f in enumerate(fields):
            if i < len(parts):
                result[f] = parts[i]
        if result['name'] and title:
            result['name'] = _clean_character_name(title)
        return result

    # 策略2/3：按行解析，匹配字段关键词
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    matched_any = False
    # 当前收集目标字段（用于多行值，遇到下一行引导行则结束）
    cur_field: str | None = None
    cur_buf: list[str] = []
    def _flush():
        nonlocal cur_field, cur_buf
        if cur_field and cur_buf:
            v = '\n'.join(cur_buf).strip()
            if not v:
                pass
            elif cur_field.startswith('__extra_'):
                # 额外字段 → 存到 result_extra
                result_extra[cur_field] = (result_extra.get(cur_field) or '') + v
            elif cur_field == 'name':
                # name 是唯一标识：覆盖初始 title/'未命名'，不做换行累加
                # （否则 title 为空时会产生 "未命名\n林晚" 这类脏名）
                result['name'] = v
            else:
                prev = (result.get(cur_field) or '').strip()
                result[cur_field] = (prev + '\n' + v).strip() if prev else v
        cur_field = None
        cur_buf = []
    all_labels = [label for labels in key_map.values() for label in labels]
    lead_re = re.compile(
        r'^(?:【|\[)?(' + '|'.join(re.escape(x) for x in sorted(all_labels, key=len, reverse=True)) +
        r')(?:】|\])?[:：]?\s*(.*)$'
    )
    for line in lines:
        m = lead_re.match(line)
        if m:
            _flush()
            matched_any = True
            label = m.group(1)
            value = m.group(2).strip()
            # 找出归属字段（先取命中的字段key）
            target = None
            for f, labels in key_map.items():
                if label in labels:
                    target = f
                    break
            if target is None:
                # 兜底：标准字段直接按原行识别
                cur_field = None
            else:
                cur_field = target
                if value:
                    cur_buf = [value]
                else:
                    cur_buf = []
        else:
            if cur_field:
                cur_buf.append(line)
            else:
                # 未在任何引导字段下：当做 personality 的补充文本（常见纯文本场景）
                cur_field = 'personality'
                cur_buf = [line]
    _flush()
    if matched_any:
        if title and not result['name']:
            result['name'] = _clean_character_name(title)
        elif not result['name'] or result['name'] == '未命名':
            result['name'] = _clean_character_name(title) or lines[0][:50]
        # 把额外字段（开场/对白示例风格/备注/源文件）合并到标准字段，避免写 DB 时丢失：
        #   abilities 字段追加 对白示例+说话风格
        if result_extra.get('__extra_first_line'):
            # 开场第一句 → 塞到 background 末尾（便于人物出场直接引用）
            add = f"【出场第一句话/动作】{result_extra['__extra_first_line']}"
            result['background'] = (result['background'] + '\n' + add).strip() if result['background'] else add
        if result_extra.get('__extra_notes'):
            add = f"【创作者备注】{result_extra['__extra_notes']}"
            # 备注 → 塞到 background + personality 末尾（避免丢）
            for slot in ('background', 'personality'):
                prev = (result.get(slot) or '').strip()
                result[slot] = (prev + '\n' + add).strip() if prev else add
        if result_extra.get('__extra_source_fn'):
            add = f"【来源】Silly Tavern 角色卡：{result_extra['__extra_source_fn']}"
            prev = (result.get('background') or '').strip()
            result['background'] = (prev + '\n' + add).strip() if prev else add
        return result

    # 策略4：纯文本兜底
    result['name'] = _clean_character_name(title) or (lines[0][:50] if lines else '未命名')
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
                if _is_stream_retry(chunk):
                    yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
                    # ===== A方案：优先用章粒度 chapter_beats 切出"本章只推进的这一段" =====
                    beats = n.get('chapter_beats') if isinstance(n.get('chapter_beats'), list) else []
                    beat_for_this_chapter = ''
                    if beats:
                        for b in beats:
                            if not isinstance(b, dict):
                                continue
                            try:
                                if int(b.get('chapter')) == target_chapter_num:
                                    beat_for_this_chapter = str(b.get('beat') or '')
                                    break
                            except (TypeError, ValueError):
                                continue
                    if beat_for_this_chapter:
                        ch_lo, ch_hi = int(nums[0]), int(nums[-1])
                        lines.append(f'本章剧情点：{beat_for_this_chapter}')
                        if ch_lo < ch_hi:
                            remaining = ch_hi - target_chapter_num
                            if remaining > 0:
                                lines.append(f'【边界约束】本节点横跨 {ch_lo}-{ch_hi} 章，本章只许推进上述剧情点；剩余 {remaining} 章的剧情（含高潮/反转/收尾/钩子）留到后续章，禁止在本章一次性写完。')
                    else:
                        # 无章粒度数据（旧数据/未填）：回退整段 summary，但补边界约束
                        summary = n.get('summary') or n.get('plot') or ''
                        if summary:
                            lines.append(f'节点概要：{summary}')
                            ch_lo, ch_hi = int(nums[0]), int(nums[-1])
                            if ch_hi > ch_lo:
                                lines.append(f'【边界约束】本节点横跨 {ch_lo}-{ch_hi} 章，本章只写其中与第 {target_chapter_num} 章对应的那一段；不得把整段起因→高潮→收尾→钩子一章写完，后续章内容须保留。')
                    if n.get('cool_type') and not beat_for_this_chapter:
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
            '【本章剧情·最高指令】书接上文，读取剧情维度里的「本章剧情节点」，禁止超出本章剧情节点创作，保证ONE主钩子贯穿本章、禁止无目标流水账。语句自然顺畅，写事为主，景一笔带过，非必要不用比喻/拟人等修辞。'
            f'\n\n本章必须写完且只写以下 {len([x for x in chapter_plot_ctx.splitlines() if x.strip()])} 个节点（顺序不得调换、不得跳过、不得新增）：'
            f'\n{chapter_plot_ctx}'
            '\n\n【本章边界铁律·防超纲透支】本节点横跨多章时，本章只推进「本章剧情点」里指定的那一段：\n'
            '  · 只写完本章对应推进内容，未到位的后续剧情（高潮/反转/收尾/钩子）一律留到节点跨度的后续章，禁止一章把所有节点剧情全程写完；\n'
            '  · 若「本章剧情点」已给出，就严格按它写，不自行把 summary 整段拍进去；\n'
            '  · 若只有「节点概要」（旧数据无按章细分），只写其中属于第 X 章的一段，因果链可在本章起始一笔交代前情，但后续关键推进必须保留到后续章节。'
            '\n\n【本章字数铁律】纯正文（不含标题）2300-2500字，全角中文标点；不足时扩事件对白/停顿情绪/推进动作，超了删枝节。（⚠️ 遵守【禁令0】不得凑字/超标。写事为主，景一笔带过，非必要不用比喻/拟人等修辞，宁可字数微欠也不靠剩料描写充数）'
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

    # 开头提示帧已在函数顶部发出（防连接超时占位），此处不再重复发「正在写第X章」，避免前端出现两条相同提示
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
            if _is_stream_retry(chunk):
                yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
· nodes[].summary 是"事件推进梗概"不是正文草稿：只写 起因→关键动作→直接后果→收尾→钩子，各一句、动词+名词为主；禁止环境/物象描写、比喻/拟人/排比、动作细节链、形容词混砌、心理/情绪铺陈。每个节点是剧情调度卡（时间/地点/事件/冲突/出场人物/钩子/伏笔），不承担文字润色。

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
    # P0 榜单风向：前端先扫榜把 rank_scan 塞进来；注入 system prompt + 卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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
    base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block, rank_scan=_rank_scan)

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

    # ===== 写正文意图·注入正文行文规范（与 chat_smart 同源）=====
    # 设定通用Tab(smart_general)默认只注入构思规则(GENERAL_CORE_RULES+构思格式)，不注入
    # WRITING_STYLE_RULES（设计见 build_chat_system_prompt）。但当用户在此 Tab 明确要求
    # "写第X章/写正文/接着写"时，同样需要命中正文行文规范，否则产出的 SAVE_CHAPTER 正文卡
    # 会脱离行文/去AI硬卡约束。与 chat_smart / chat_smart_action / ai_continue 保持同源注入。
    if _is_write_chapter_intent(message):
        try:
            _general_chapter_rules = build_chat_chapter_rules(book, mode='agent')
            if _general_chapter_rules:
                sys_prompt = sys_prompt + '\n\n' + _general_chapter_rules
        except Exception:
            pass  # 注入失败不阻断主流程

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
                if _is_stream_retry(chunk):
                    yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
                _enrich_card_rank_meta(card, _rank_scan)
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            # 历史里保存作者原话（不保存注入引用块，避免多轮重复上下文）
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': message})
            # 历史保存的卡片也同步 enrich，后续复盘/多轮时仍带风向标签
            for c in (cards or []):
                _enrich_card_rank_meta(c, _rank_scan)
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
    # P0 榜单风向：前端扫榜结果 rank_scan 注入（可选；没扫则不注入）
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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

    # 文风维度专属：把「行文文风」菜单注入多方案，要求每套方案点名采用哪种文风并提供差异化选择
    style_extra = ''
    if dim_key == 'style_guide':
        try:
            from app import CHAPTER_LANG_STYLES as _lang_styles
        except Exception:
            _lang_styles = {}
        _style_menu_lines = [f'- {t[0]}：{t[1]}' for t in _lang_styles.values() if isinstance(t, (tuple, list)) and t[0] and t[1]]
        _style_menu = '\n'.join(_style_menu_lines) if _style_menu_lines else '（通用/白描/幽默/爽文/古风…等）'
        style_extra = f"""
【文风维度专属要求·必须点名行文文风】本书提供以下「行文文风」菜单（可单选，也可选 2 种组合成"基调+点缀"）：
{_style_menu}

你生成的每一个方案 preview 必须**点名**该方案主用的「行文文风」（用上面菜单里的中文名，如"幽默+市井""冷硬白描""古风雅致""爽文节奏"），并具体说明这种文风如何落到本书的叙事口吻、节奏把控、对白密度里。
3-5 个方案必须在「行文文风」上做出**明确差异**（不同文风组合=不同方案方向），严禁所有方案都用同一种文风。
每个方案 preview 末尾固定追加一行「文风方案：XXX」，标出该方案所用的文风组合，方便作者一眼对比选择。"""

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
{outline_extra}{af_alerts_suggest}{suggest_iron_rule}{preview_volume_req}{style_extra}

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

    # P0 榜单风向：把扫榜情报追加到 system_prompt。有多方案场景要求标题/卖点/钩子优先贴合。
    _rank_ctx = _format_rank_context(_rank_scan)
    if _rank_ctx:
        sys_prompt = sys_prompt.rstrip() + '\n\n' + _rank_ctx

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
    # P0 榜单风向：前端先扫榜把 rank_scan 塞进来，注入 sys_prompt + 落地卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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

    # P0 榜单风向：把扫榜市场情报追加到 system prompt 末尾（用户点了扫榜才生效）
    _rank_ctx = _format_rank_context(_rank_scan)
    if _rank_ctx:
        sys_prompt = sys_prompt.rstrip() + '\n\n' + _rank_ctx

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
                _enrich_card_rank_meta(card, _rank_scan)
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
                for ec in extra_cards:
                    ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                    if ec.get('title'):
                        ec['title'] = _clean_text_to_plain(ec['title'])
                    _enrich_card_rank_meta(ec, _rank_scan)
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
                    _max_tok = _DIM_MAX_TOKENS  # 截断类失败（思考耗尽 token）重试直接顶满，渐进 1.5x 不够思考消耗
                else:
                    _max_tok = min(int(max_tok * (1.5 if _attempt == 1 else 2)), _DIM_MAX_TOKENS)
                # 第 2 次起进"精简模式"：截断过长 system/铁律，防 prompt 溢出 → 模型拒答吐空
                _msgs_for_this_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key) if _attempt >= 1 else cur_messages
                try:
                    # 【聊天终止修复】单次流失败只记录原因并降级重试，不再炸掉整条 SSE（旧实现直接
                    # 进外层 except → error 帧 → 前端 removeEmptyAi 消息戛然而止）
                    for chunk in gw_stream_with_hb(gw, _msgs_for_this_call, temperature=_temp, max_tokens=_max_tok):
                        if chunk is HEARTBEAT:
                            yield SSE_HEARTBEAT_COMMENT
                            continue
                        if _is_stream_retry(chunk):
                            yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
            _enrich_card_rank_meta(card, _rank_scan)
            card_meta = {'validation': validation_meta} if validation_meta else None  # 自检结果随卡片下发
            yield sse({'type': 'card', 'card': card, 'session_id': session_id, 'meta': card_meta})
            for ec in extra_cards:
                ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                if ec.get('title'):
                    ec['title'] = _clean_text_to_plain(ec['title'])
                _enrich_card_rank_meta(ec, _rank_scan)
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
                    _max_tok = min(int(max_tok * 1.5), _DIM_MAX_TOKENS)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), _DIM_MAX_TOKENS)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                    if chunk is HEARTBEAT:
                        yield SSE_HEARTBEAT_COMMENT
                        continue
                    if _is_stream_retry(chunk):
                        yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
            _enrich_card_rank_meta(card, _rank_scan)
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
                            _max_tok = min(int(max_tok * 1.5), _DIM_MAX_TOKENS)
                        elif _attempt >= 2:
                            _max_tok = min(int(max_tok * 2), _DIM_MAX_TOKENS)
                        _msgs_call = cur_messages
                        if _attempt >= 1:
                            _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                        for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                            if chunk is HEARTBEAT:
                                yield SSE_HEARTBEAT_COMMENT
                                continue
                            if _is_stream_retry(chunk):
                                yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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
                _enrich_card_rank_meta(card, _rank_scan)
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
                    _cd = {
                        'id': str(uuid.uuid4())[:8],
                        'type': spec['card'],
                        'title': f'{spec["label"]}（AI智驾生成）',
                        'content': c,
                        'target': _CARD_TARGET.get(spec['card'], spec['label']),
                        'status': 'pending',
                    }
                    _enrich_card_rank_meta(_cd, _rank_scan)
                    cards.append(_cd)
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
                    _max_tok = min(int(max_tok * 1.5), _DIM_MAX_TOKENS)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), _DIM_MAX_TOKENS)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim='chapter_deai')
                for chunk in gw_stream_with_hb(gw, _msgs_call, temperature=_temp, max_tokens=_max_tok):
                    if chunk is HEARTBEAT:
                        yield SSE_HEARTBEAT_COMMENT
                        continue
                    if _is_stream_retry(chunk):
                        yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
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


# ============================================================================
# 【通用聊天】新增路由（薄壳，核心逻辑放独立模块，防chat_collab_bp超基线）
#   POST /api/ai/chat/general                   通用聊天（任意话题）+ 命中维度提示气泡
#   GET/PUT /api/ai/search-config               联网搜索 Key 配置（存 AppPreference KV）
# ============================================================================

def _sync_search_keys_from_preference():
    """把 AppPreference.web_search_keys 里的 Tavily/Exa/Brave Key 同步进 os.environ。

    优先用外部环境变量；只有 env 未设置时才用用户保存的 Key。
    """
    import json as _json
    try:
        from app import AppPreference
    except Exception:
        return
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        if not str(raw).strip():
            return
        d = _json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(d, dict):
            return
        for env_key, pref_key in (('TAVILY_API_KEY', 'tavily'), ('EXA_API_KEY', 'exa'), ('BRAVE_API_KEY', 'brave')):
            if not os.environ.get(env_key):
                v = str(d.get(pref_key) or '').strip()
                if v:
                    os.environ[env_key] = v
    except Exception:
        return


@chat_collab_bp.route('/api/ai/search-config', methods=['GET'])
def ai_search_config_get():
    """返回当前联网搜索 Key 配置状态（不回传明文，只有"已配置/未配置"）。"""
    from app import AppPreference
    d = {}
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        d = json.loads(raw) if str(raw).strip() else {}
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    masked = {}
    for k in ('tavily', 'exa', 'brave'):
        v = str(d.get(k) or '').strip()
        masked[k] = '已配置' if v else ''
    env = {}
    for k in ('TAVILY_API_KEY', 'EXA_API_KEY', 'BRAVE_API_KEY'):
        env[k] = bool(str(os.environ.get(k) or '').strip())
    return jsonify({'keys': masked, 'env': env})


@chat_collab_bp.route('/api/ai/search-config', methods=['PUT'])
def ai_search_config_put():
    """保存联网搜索 Key（可选，Tavily/Exa/Brave 任一即可；留空=清除该引擎）。"""
    from app import AppPreference
    body = request.json or {}
    cur = {}
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        cur = json.loads(raw) if str(raw).strip() else {}
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    changed = {}
    for k in ('tavily', 'exa', 'brave'):
        if k in body:
            v = str(body.get(k) or '').strip()
            cur[k] = v
            changed[k] = bool(v)
    AppPreference.set('web_search_keys', json.dumps(cur, ensure_ascii=False))
    # 新 Key 立刻生效（写入 os.environ 供 web_search_bridge 这里的连接读取）
    _sync_search_keys_from_preference()
    return jsonify({'ok': True, 'updated': changed})

@chat_collab_bp.route('/api/ai/chat/general', methods=['POST'])
def chat_general():
    """通用聊天模式（CHATBOX风格）：
    - 不强制创作上下文，可聊任何话题；
    - 命中写作相关关键词/句式时：
        * 首帧 meta 回传命中建议（前端渲染"是否落入XX维度？"气泡）
        * system_prompt鼓励AI在讨论出明确结论时产出落地卡片
    - body: { book_id?, session_id?, message }  (book_id可选，不给就是纯闲聊不绑定作品)
    - 返回 SSE：delta / meta(hit_suggestions) / card / done / error
    """
    from app import db, AISession, Book, BookBible, AIConfig
    from llm_gateway import LLMGateway, get_llm_config
    # 命中识别 & system_prompt 构建（独立模块）
    try:
        from general_chat_hitter import (detect_dimension_hits, build_general_chat_system_prompt,
                                         wrap_message_with_context)
    except ImportError:
        detect_dimension_hits = None
        build_general_chat_system_prompt = lambda: '你是智驾创作助手，可以聊任何话题。'
        wrap_message_with_context = lambda msg, bt, bb: msg
    # P0 真联网搜索：多引擎调度桥（Tavily/Exa/Brave/DuckDuckGo兜底 + 智谱原生web_search）
    try:
        from web_search_bridge import (should_use_web_search, run_web_search,
                                       format_search_context_for_llm, get_native_websearch_params)
        _search_available = True
    except Exception:
        _search_available = False
        def should_use_web_search(*a, **k): return False
        def run_web_search(*a, **k):
            from dataclasses import dataclass
            @dataclass
            class _SR: ok=False; engine=''; hits=[]; error='import failed'; latency_ms=0
            return _SR()
        def format_search_context_for_llm(sr): return ''
        def get_native_websearch_params(*a, **k): return None

    data = request.json or {}
    book_id = data.get('book_id')
    session_id = data.get('session_id')
    message = (data.get('message') or '').strip()
    # P0-4 通用聊天工具栏（底部一排）透传项：
    #   deep_think: 深度思考程度（0=关闭 1=标准思考 2=深度思考：温度/字数/system 逐级增强）
    #   web_search_enabled: 联网搜索开关（true=强制联网搜索，绕开"创作类话题不搜"的启发式过滤）
    deep_think = min(max((int(data.get('deep_think') or 0)), 0), 2)
    web_search_enabled = bool(data.get('web_search_enabled'))
    # P1-1 会话级切模型：请求体 ai_config_id > 会话 meta_json.ai_config_id > 全局激活
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    # P1-3 内置角色 persona：default/polish/toxic_critic/architect/worldbuilder/marketeer/interviewer
    req_role_id = (data.get('role_id') or '').strip() or None
    if not message:
        return jsonify({'error': '缺少 message'}), 400
    # book_id 为空 = 纯闲聊会话（scope=general_global）
    scope = 'general_global' if not book_id else 'general_per_book'
    book_title = ''
    bb_summary = ''
    book = None
    bb = None
    base_system = ''
    # 最近章节 + 下一章号（与 chat_smart 统一口径，避免通用聊"姜离是主角吗"回答错——因为没读到人物采纳资料）
    recent_chapters: list = []
    next_chapter_num: int | None = None
    toc_block = ''
    if book_id:
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        book_title = book.title or ''
        bb = BookBible.query.filter_by(book_id=book_id).first()
        # ===== 关键修复：通用聊天读取"当前作品已采纳各维度内容"（人物/设定/世界观/大纲…）=====
        # 之前 chat_general 只用了 bb_summary = "已填充维度摘要：人物、世界观" → 纯标签，没有真正把人物内容喂给LLM
        # → 用户截图里说"姜离是主角你怎么忘了"就是因为没注入已采纳的 character_profiles/concept 等内容
        # 改法：直接复用 chat_smart 链路的 build_chat_system_prompt（完整注入9个维度字段 + TOC + 最近章节 + 章节号铁律）
        from app import Chapter, parse_chapter_number
        try:
            ch_info = _get_latest_chapter_info(book_id)
            next_chapter_num = ch_info['next_num']
            recent_raw = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
            def _ck(c):
                n = parse_chapter_number(c.title or '')
                return n if isinstance(n, int) and n > 0 else (99999 + int(c.order_index or 0))
            recent_sorted = sorted(recent_raw, key=_ck)
            recent_chapters = [
                {
                    'title': ch.title or f'第{ch.order_index or 0}章',
                    'word_count': getattr(ch, 'word_count', 0) or 0,
                    'order_index': int(ch.order_index or 0),
                } for ch in recent_sorted[-5:]
            ]
        except Exception:
            recent_chapters = []
            next_chapter_num = None
        try:
            toc_block = _build_toc_block(book_id)
        except Exception:
            toc_block = ''
        # 复用 chat_smart 的维度感知 system_prompt（完整注入构思/人物/世界观/核心规则/大纲/剧情线/伏笔/地点/文风 + 最近章节 + TOC + 章节号铁律）
        # 用 PromptContextCache 命中，减少 DB → LLM 的 token 浪费（与正文创作链路统一）
        try:
            from prompt_context_cache import PromptContextCache
            _cache = PromptContextCache.get_instance()
            _cache_key = f'general_chat_system:{book_id}'
            def _builder():
                return build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
            base_system = _cache.get_or_build(_cache_key, _builder, ttl_sec=900)
        except Exception:
            base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
        # bb_summary 升级成非空的"维度摘要"（命中创作关键词的引用前言要用）：维度名+是否填充，不再是大白话标签
        non_empty_fields = [f for f in [
            ('concept', '核心构思'), ('worldbuilding', '世界观'), ('key_rules', '核心规则'),
            ('character_profiles', '人物'), ('plot_design', '大纲'),
            ('timeline', '剧情线'), ('foreshadowing', '伏笔'),
            ('locations', '地点'), ('style_guide', '文风'),
        ] if getattr(bb, f[0], None) and str(getattr(bb, f[0])).strip()]
        bb_summary = '、'.join(nf[1] for nf in non_empty_fields) if non_empty_fields else '暂无已填充维度'
    # 命中维度检测（零LLM快路径）
    hit_suggestions = detect_dimension_hits(message) if detect_dimension_hits else []

    # 会话（global闲聊：不绑book，通用唯一会话key）
    if not book_id:
        # 纯闲聊会话：用固定scope+uuid，book_id写入None
        session = AISession.query.filter(
            AISession.scope == 'general_global',
            AISession.title == '通用闲聊',
        ).order_by(AISession.updated_at.desc()).first()
        if not session:
            session = AISession(id=str(uuid.uuid4()), scope='general_global',
                                title='通用闲聊', book_id=None,
                                messages_json='[]', created_at=datetime.now(timezone.utc),
                                updated_at=datetime.now(timezone.utc))
            db.session.add(session); db.session.commit()
        session_id = session.id
    else:
        session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=message[:30])
        session_id = session.id

    # 构建 messages
    # P1-3 内置角色 persona 表：id -> (name, system_prompt_extra)
    #（单一定义在模块级 _PERSONAS，通用聊天与圆桌会议共用，避免人格漂移）
    _BUILTIN_ROLES = dict(_PERSONAS)  # 注意：模块级表不含 'default'，这里补上
    _BUILTIN_ROLES['default'] = ('默认助手', '')
    # 选角色：请求 > 会话meta_json.role_id > default
    _session_role_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            _meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            _session_role_id = (_meta.get('role_id') or '').strip() or None
    except Exception:
        _session_role_id = None
    chosen_role_id = req_role_id or _session_role_id or 'default'
    if chosen_role_id not in _BUILTIN_ROLES: chosen_role_id = 'default'
    # 持久化 role_id 到 session.meta_json（下次沿用）
    if req_role_id and chosen_role_id == req_role_id and session:
        try:
            _meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(_meta, dict): _meta = {}
            if _meta.get('role_id') != chosen_role_id:
                _meta['role_id'] = chosen_role_id
                session.meta_json = json.dumps(_meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass
    _role_name, _role_extra = _BUILTIN_ROLES[chosen_role_id]
    # ==================================================================
    # 【榜单分析师角色专属·CRITICAL FIX】
    #   自动扫榜 = 只在 generate() 内部、首帧心跳已经发出之后 执行。
    #   ❌ 绝不能放这里（chat_general 外层=响应头还没返回=连接期卡死=前端fetchWithRetry 60s超时abort）。
    #   所以这里把扫榜需要的参数提前快照下来 → 真正执行放在 generate() 首帧 meta 之后。
    # ==================================================================
    _fc_snapshot: str = ''
    try:
        _bb_fc_snap = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
        if _bb_fc_snap:
            _fc_snapshot = (_bb_fc_snap.concept or _bb_fc_snap.master_outline or '').strip()
    except Exception:
        _fc_snapshot = ''
    _rank_analyst_snapshot: Dict[str, Any] = {
        'active': (chosen_role_id == 'rank_analyst'),
        'message': message,
        'fallback_concept': _fc_snapshot,
        'book_title': book_title or '',
    }
    # P1-3 提示词变量：{date} {time} {current_book} {model_name}，注入到 system_prompt + enriched user message
    from datetime import datetime, timezone, timedelta
    _tz = timezone(timedelta(hours=8))
    _now = datetime.now(_tz)
    _var_ctx = {
        'date': _now.strftime('%Y-%m-%d'),
        'time': _now.strftime('%H:%M'),
        'current_book': book_title or '(未绑定作品)',
        'model_name': (cfg.model if 'cfg' in dir() and cfg else '') or (AIConfig.get_active().model if AIConfig.get_active() else ''),
    }
    def _var_replace(s: str) -> str:
        if not s: return s
        for k, v in _var_ctx.items():
            s = s.replace('{' + k + '}', str(v))
        return s
    # ============== system_prompt 合成：==============
    #   · 纯闲聊（无book_id）：沿用 build_general_chat_system_prompt 的自由聊天规则
    #   · 绑定作品（有book_id）：核心部分复用 build_chat_system_prompt（已完整注入构思/人物/世界观/核心规则/大纲/剧情线/伏笔/地点/文风 + TOC + 最近章节 + 章节号铁律）
    #       再叠加通用聊天专属规则（命中创作关键词→气泡+落卡、不要瞎编榜单、扫榜走Step1工具）
    _general_only = build_general_chat_system_prompt()
    if book_id and base_system:
        # 把通用聊天的"闲聊自由/命中创作话题时的行为/扫榜禁令"取出来，拼到 base_system 末尾（避免 base_system 的写作协作口吻覆盖掉闲聊自由）
        _extra_rule_lines = []
        _capture = False
        for _ln in _general_only.splitlines():
            if _ln.startswith('二、命中创作话题时的行为'):
                _capture = True
            if _capture:
                _extra_rule_lines.append(_ln)
        _extra_rules = '\n'.join(_extra_rule_lines).strip()
        system_prompt = base_system.rstrip() + "\n\n================================\n【通用聊天模式补充说明】\n"
        system_prompt += "- 在【设定/通用】里：作者既可讨论创作，也可能问无关创作的闲聊问题（编程/科普/生活…）。只要不是创作话题，就不要把话题往创作上扯，直接聊对应的话题内容，简洁有人情味。\n"
        system_prompt += f"- 当前作品《{book_title}》已填充维度库：{bb_summary or '暂无已填充维度'}。上述 bible 资料是作者已采纳落地的内容，回答任何创作相关问题时**以落地资料为准，不反着已采纳内容瞎编**（例如：落地资料里主角是姜离，就不要把林玄当主角）。\n"
        if _extra_rules:
            system_prompt += "\n" + _extra_rules + "\n"
    else:
        system_prompt = _general_only
    # 把当前 persona + 上下文变量 注入到 system_prompt 末尾（prepend 变量说明）
    _var_intro = f"【运行时上下文变量，可在回答中按需引用】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
    if _role_extra:
        system_prompt = system_prompt.rstrip() + "\n\n【当前人格角色】用户已为本次会话切换到「" + _role_name + "」模式。严格按以下身份说明输出：\n" + _role_extra + "\n\n" + _var_intro
    else:
        system_prompt = system_prompt.rstrip() + "\n\n" + _var_intro
    # ============== 用户消息前置"引用前言"：命中写作相关时，只附加 system_prompt 里没给、但对本轮对话必须精准的资料 ============
    # 原则：system_prompt 里已经有完整 bible(9维度/TOC/最近章标题)，引用前言不重复；
    #       这里只补三样：① 本轮问题命中的"章节正文摘要"（这是 system_prompt 故意没给的，避免塞爆）② 人名速查表 ③ 若还缺具体维度再根据命中关键词补
    try:
        from general_chat_hitter import WRITING_TOTAL_HINTS as _WTH
    except Exception:
        _WTH = []
    _talking_creation = bool(_WTH) and any(h in message for h in _WTH)
    _lead_ref = ''
    if book_id and _talking_creation and bb:
        from app import Chapter, parse_chapter_number
        import re as _re
        lead_parts: list[str] = []
        lead_parts.append('（以下为系统引用：作者本轮问题要用到的精准资料。回答创作相关问题时**先看引用，再结合 system_prompt 里的完整维度**。）')
        # ==== A. 章节号提取 + 对应章节正文摘要注入 ====
        # 解析"第1章/第一章/改第03章/第 8 章/卷一第3章/这一章/本章"
        _msg_low = message
        _ch_num: int | None = None
        # 数字形式：第\s*(\d+)\s*章
        m1 = _re.search(r'第\s*([0-9零一二三四五六七八九十百千万两贰叁肆伍陆柒捌玖拾]+)\s*章', _msg_low)
        if m1:
            raw = m1.group(1).strip()
            try:
                _ch_num = parse_chapter_number(f'第{raw}章')
            except Exception:
                _ch_num = None
        # 汉字常见单独写法兜底
        if _ch_num is None:
            cn_map = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100,'千':1000,
                      '壹':1,'贰':2,'叁':3,'肆':4,'伍':5,'陆':6,'柒':7,'捌':8,'玖':9,'拾':10,'佰':100,'仟':1000,'万':10000}
            m2 = _re.search(r'第\s*([零一二三四五六七八九十百千万两贰叁肆伍陆柒捌玖拾佰仟万]+)\s*章', _msg_low)
            if m2:
                raw = m2.group(1).strip()
                val = 0; cur = 0; unit = 1
                for ch in raw:
                    if ch in cn_map and cn_map[ch] >= 10:
                        u = cn_map[ch]
                        if cur == 0: cur = 1
                        val += cur * u
                        cur = 0; unit = u
                    elif ch in cn_map and 1 <= cn_map[ch] <= 9:
                        cur = cn_map[ch]
                    else:
                        break
                if cur:
                    val += cur if val and unit == 1 else cur
                if val and 1 <= val <= 9999:
                    _ch_num = val
        # 兜底：用户说"这一章/本章/第一章/最后一章/刚写的"→ 取最近章节号 next_chapter_num-1
        if _ch_num is None:
            recent_aliases = ('这一章', '本章', '第一章', '最后一章', '刚写的', '刚改的', '现在写的这章', '你刚才写的', '你上条生成的', '这篇')
            if any(a in _msg_low for a in recent_aliases) and isinstance(next_chapter_num, int) and next_chapter_num > 1:
                _ch_num = next_chapter_num - 1
        _chapter_body_snippet = ''
        _chapter_title = ''
        if isinstance(_ch_num, int) and _ch_num >= 1:
            try:
                all_ch = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
                found = None
                # 优先按 parse_chapter_number 匹配
                for c in all_ch:
                    if parse_chapter_number(c.title or '') == _ch_num:
                        found = c; break
                # 兜底按 order_index == _ch_num 匹配（没写章号的草稿章通常 order_index = 章号）
                if not found:
                    for c in all_ch:
                        if int(getattr(c, 'order_index', 0) or 0) == _ch_num:
                            found = c; break
                if found:
                    _chapter_title = found.title or f'第{_ch_num}章'
                    body = (found.content or '').strip()
                    if body:
                        MAX_PREVIEW = 3800  # 单章正文引用硬上限（避免一章6000字+就撞截断）
                        if len(body) <= MAX_PREVIEW:
                            _chapter_body_snippet = body
                        else:
                            # 前 800 字（首钩子/出场）+ 后 2800 字（结尾/冲突点），中间提示省略（LLM最需要的是首尾两段）
                            head = body[:800]
                            tail = body[-2800:]
                            skip_cnt = len(body) - 800 - 2800
                            _chapter_body_snippet = (
                                head
                                + f'\n\n……【中间{skip_cnt}字已省略，仅保留首段钩子+末段高潮】……\n\n'
                                + tail
                                + f'\n（共{len(body)}字，截前800+后2800注入引用）'
                            )
            except Exception:
                _chapter_body_snippet = ''
                _chapter_title = ''
        if isinstance(_ch_num, int) and _ch_num >= 1:
            if _chapter_body_snippet:
                lead_parts.append(f'\n【引用：第{_ch_num}章正文】（标题：{_chapter_title}，共{len(_chapter_body_snippet)}字预览）\n{_chapter_body_snippet}')
            else:
                lead_parts.append(f'\n【引用：未找到第{_ch_num}章的正文原文】。请直接把该章原文贴到聊天里，或从【正文Tab→章节号{_ch_num}→复制正文后粘贴】。我拿到全文后再改。')
        # ==== B. 人物 JSON → 人名速查表（不重复 system_prompt 的长档案，只给 name + role + identity，LLM 定位人名快 10 倍）====
        _cp = (getattr(bb, 'character_profiles', '') or '').strip()
        if _cp.startswith('['):
            try:
                arr = json.loads(_cp)
                if isinstance(arr, list) and len(arr) > 0:
                    quick: list[str] = []
                    for item in arr:
                        if isinstance(item, dict):
                            nm = str(item.get('name') or '').strip()
                            if not nm:
                                continue
                            _r = str(item.get('role') or '').strip()
                            _id = str(item.get('identity') or '').strip()
                            line = f"- {nm}"
                            if _r: line += f"（{_r}）"
                            if _id: line += f" · {_id}"
                            quick.append(line)
                    if quick:
                        lead_parts.append(f'\n【引用：人名速查表】（落地{len(arr)}人）\n' + '\n'.join(quick[:40]) + (f'\n（省略{len(quick)-40}人）' if len(quick) > 40 else ''))
            except Exception:
                pass
        if len(lead_parts) > 1:
            _lead_ref = '\n'.join(lead_parts) + '\n————————引用结束————————\n【作者原话】\n'
    elif _talking_creation:
        # 纯闲聊命中创作关键词但没绑定作品 → 走 general_chat_hitter 原版前言（提示作品未绑定）
        _lead_ref = None
    if _lead_ref:
        user_with_ref = _lead_ref + message
    else:
        user_with_ref = wrap_message_with_context(message, book_title, bb_summary)
    enriched = _var_replace(user_with_ref)

    # ======= 节点设计师：续会 / 新卷启动 注入上下文 =======
    # 学习圆桌会议续会机制：命中"继续/接着/往下"类纯续会指令 → 加载 state
    # 从 meta_json['node_designer_state'] 拿到 last_ch，拼一段「从Y+1开始不要重复」的系统注入给 LLM
    # 命中明确"第N卷 节点设计"新卷指令 → 清掉旧 state（上卷进度作废，新卷从头来）
    _nd_is_node_role = (chosen_role_id == 'node_designer')
    _nd_state = None
    _nd_meta_for_closure: dict = {}  # 供闭包 generate() 写 state 用
    if _nd_is_node_role:
        new_vi = _is_nd_new_volume_request(message) if message else None
        is_continue = _is_nd_continue(message) if message else False
        # 先从会话历史提取一条"最近一次 AI 输出"，用于续会 state 缺失时兜底解析 last_ch/volume_index
        last_assistant_text = ''
        try:
            _hist_tmp = load_session_messages(session)
            if isinstance(_hist_tmp, list):
                for m in reversed(_hist_tmp):
                    if isinstance(m, dict) and m.get('role') == 'assistant' and str(m.get('content', '')).strip():
                        last_assistant_text = str(m.get('content', '') or '')
                        break
        except Exception:
            last_assistant_text = ''
        if new_vi is not None:
            # 作者明确启动"第N卷节点设计"新任务 → 清旧 state（开始新卷）
            _nd_clear_state(session, db)
        if is_continue:
            _nd_state = _nd_load_state(session)
            # state 缺失的兜底：从最近AI输出解析 last_ch/volume_index
            if not _nd_state:
                _vi = _parse_volume_index_from_text(message + '\n' + last_assistant_text) or 1
                _lc = _parse_last_chapter_from_text(last_assistant_text)
                if _lc > 0:
                    _nd_state = {'volume_index': _vi, 'cpv': 50, 'last_ch': _lc, 'volume_title': '',
                                 'updated_at': datetime.now(timezone.utc).isoformat()}
            if _nd_state:
                # 往 enriched（最终给LLM的用户消息末尾）追加续会上下文
                inject = _nd_build_continue_user_injection(_nd_state)
                if inject:
                    enriched = (enriched or '').rstrip() + '\n' + inject
                _nd_meta_for_closure = dict(_nd_state)
        else:
            # 非续会：启动新请求 → 建立初始 state（从用户消息里解析 volume_index / cpv）
            init_vi = None
            if new_vi is not None:
                init_vi = new_vi
            else:
                init_vi = _parse_volume_index_from_text(message)
            if init_vi is None:
                # 从历史 AI（前一条）里兜底看有没有"第X卷"
                init_vi = _parse_volume_index_from_text(last_assistant_text) or 1
            init_cpv = 50
            try:
                _mcpv = re.search(r'(?:cpv|每卷章数|章节数|共)\s*[=:：]*\s*(\d{1,3})\s*章', message or '')
                if _mcpv:
                    init_cpv = max(10, min(200, int(_mcpv.group(1))))
            except Exception:
                pass
            _nd_meta_for_closure = {
                'volume_index': init_vi or 1,
                'cpv': init_cpv,
                'last_ch': 0,
                'volume_title': '',
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }

    history = load_session_messages(session)
    # 【重新生成】truncate_history_to：仅保留历史前 N 条（含本条前的 user），丢弃旧 AI 回复及其后续，
    # 本消息再重新生成并追加——避免重复堆积；不传则该会话按原样继续。
    _trunc = data.get('truncate_history_to')
    if isinstance(_trunc, int) and not isinstance(_trunc, bool) and 0 <= _trunc < len(history):
        history = history[:_trunc]
    # 【P1-3 meta】把当前角色以 SSE meta 回传前端，便于右上角 chip UI 同步（保证刷新后 UI 显示的角色跟后端真正用到的一致）
    _p13_meta = {'role_id': chosen_role_id, 'role_name': _role_name, 'vars': _var_ctx}
    # 最大上下文：最近50条 + 首条保留（保留模型/角色/话题起始意图），应对长会话连续追问不轻易截断
    #   - 首条保留的原因：如果首条是"我要写都市异能/毒舌读者模式开始/切换模型"，后面聊到一半丢了，LLM 会不知道自己该用啥角色/啥题材。
    #   - 最近50条 ≈ 25 轮 user+assistant。
    #   - 单条正文>1500字：截尾部1500（最后一段才是本轮讨论的重点，截头部会把重要讨论信息丢了）。
    trimmed: list[dict] = []
    if isinstance(history, list):
        keep_head = history[:1] if len(history) > 0 else []
        keep_tail = history[-50:] if len(history) > 50 else history
        candidates = keep_head + [m for m in keep_tail if not (keep_head and keep_head[0] is m)]
        # 去重（避免首条同时出现在 keep_head 和 keep_tail 里导致重复）
        seen_ids: set[int] = set()
        for m in candidates:
            if not isinstance(m, dict):
                continue
            if 'content' not in m:
                continue
            h_id = id(m)
            if h_id in seen_ids:
                continue
            seen_ids.add(h_id)
            c = m.get('content')
            if isinstance(c, str) and len(c) > 1500:
                # 取尾部 1500 字（最后一段才是用户本轮之前的意图/对话），并标注已截尾
                c = '…（会话历史超长已截断，取尾部关键内容）\n' + c[-1500:]
            trimmed.append({'role': m.get('role') or 'user', 'content': c})
    # P0-4 深度思考程度：level>=1 时在 system 末尾按档位追加"先推演再给结论"的指令
    # 思考过程必须用【推理】...【推理结束】包裹 → 后端流式切分并单独展示给作者（可切换查看，不计入正文/落盘）。
    if deep_think >= 1:
        if deep_think >= 2:
            _banner = ("【深度思考·已开启】请先深入推演：拆解关键假设 → 列出逻辑链 → 权衡各方案取舍，再给出最终结论。"
                       "你的推演过程请写在『【推理】』与『【推理结束】』之间（仅作作者回顾，不算作答案正文），"
                       "推演结束后再清晰、可落地地下结论，不因推演而啰嗦。")
        else:
            _banner = ("【标准思考·已开启】先快速理清思路、对齐目标，再给结论。"
                       "请把简要思考过程放在『【推理】』与『【推理结束】』之间（仅作作者回顾，不算作答案正文），"
                       "然后给出简明可用的结论。")
        system_prompt = system_prompt.rstrip() + "\n\n" + _banner
    messages = [{'role': 'system', 'content': system_prompt}]
    # 【通用聊天·明确指令生成维度】作者用自然语明确要求"生成/创作某维度"时，
    # 按该维度的完整格式铁律（与对应维度生成的要求一致）产出长内容 + 标准 CARD 卡片（可采纳落地）。
    # 仅命中明确指令且已绑定作品才注入（未绑定作品无落库对象，就不打断普通聊天）。
    _gen_dim_list = _rt_general_dim_request(message) if book_id else None
    if _gen_dim_list:
        try:
            _gh_bb = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
        except Exception:
            _gh_bb = None
        _gh_extra = []
        for _dk in _gen_dim_list:
            _gh_sys = _rt_create_dimension_system(_dk, book, '', '', '')
            # 去掉圆桌"共识取材"措辞，换成通用聊天的"按作者要求直接创作"
            _gh_sys = _gh_sys.replace('现在要根据一场"圆桌专家讨论"得出的共识', '现在要根据作者的明确要求')
            _gh_sys = _gh_sys.replace('必须以圆桌共识为唯一取材依据', '必须按下列维度的完整格式、直接创作出具体可落地的内容')
            _gh_extra.append(_gh_sys)
        _dim_label = '、'.join(_RT_CREATE_DIMS.get(_dk, [_dk, ''])[0] for _dk in _gen_dim_list)
        _instr = (
            f"\n\n================================\n【本轮为维度生成任务】作者要求生成：{_dim_label}。\n"
            "你只需严格按下面指定维度的完整格式要求，输出该维度的一份完整、具体、可直接写入设定库的长内容。\n"
            "输出完成后，**在回复末尾追加一张落地卡片**，格式严格为：\n"
            "[[CARD:卡片类型|标题|该维度的完整内容]]\n"
            "卡片类型从这些里面选：SAVE_CONCEPT/SAVE_RULE/SAVE_WORLDSETTING/SAVE_CHARACTER/"
            "SAVE_OUTLINE_NODE/SAVE_PLOT/SAVE_FORESHADOW/SAVE_LOCATION/APPLY_STYLE。\n"
            "正文给作者展示可读的排版版本，卡片放完整内容（两者内容一致）。\n"
        )
        for _dk in _gen_dim_list:
            _ct = _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))[1]
            _instr += f"\n\n---------------【{_RT_CREATE_DIMS.get(_dk,(_dk,''))[0]}·完整格式要求】---------------\n" + _gh_extra[_gen_dim_list.index(_dk)]
        system_prompt = system_prompt.rstrip() + _instr
        messages[0]['content'] = system_prompt
    messages.extend(trimmed)
    messages.append({'role': 'user', 'content': enriched[:8000]})

    # P1-1 会话级切模型：优先级 req_ai_config_id > session.meta_json.ai_config_id > 全局激活
    session_cfg_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            session_meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            session_cfg_id = (session_meta.get('ai_config_id') or '').strip() or None
    except Exception:
        session_cfg_id = None
    chosen_cfg_id = req_ai_config_id or session_cfg_id
    cfg = AIConfig.get_by_id(chosen_cfg_id) if chosen_cfg_id else None
    if cfg and not cfg.api_key:
        cfg = None  # 指定配置但无key → 回退全局
    if cfg is None:
        cfg = AIConfig.get_active()
    # chat_general 通用闲聊链路：强制 _normalize_llm_base_url（HTTP 500的真凶）
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    # 把当前选择持久化到 session.meta_json（保证下一轮聊天沿用同一模型，即会话级锁定）
    if chosen_cfg_id and chosen_cfg_id == cfg.id and session:
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict): meta = {}
            if meta.get('ai_config_id') != cfg.id:
                meta['ai_config_id'] = cfg.id
                session.meta_json = json.dumps(meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass  # 持久化失败不阻断主流程
    # 关键修复：之前直接 LLMGateway(cfg.base_url, cfg.api_key, cfg.model) 把 _normalize_llm_base_url 绕过了
    # 导致智谱GLM /v4 被强制拼 /v1 -> /v4/v1/chat/completions 404 -> Flask转成HTTP 500抛给前端
    import os as _os_g
    from llm_gateway import _normalize_llm_base_url as _nlg
    import app as _modg
    try:
        _actg = _modg.AIConfig.get_active()
        _actg_id = getattr(_actg, 'id', None) if _actg else None
    except Exception:
        _actg_id = None
    _is_act_g = (_actg_id and chosen_cfg_id and _actg_id == chosen_cfg_id) or (not chosen_cfg_id)
    if _is_act_g:
        _bg, _kg, _mg = get_llm_config(_modg)
        if cfg.model and cfg.model != _mg:
            _mg = cfg.model
    else:
        _bg = _nlg(cfg.base_url or _os_g.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1'), cfg.model)
        _kg = cfg.api_key or _os_g.environ.get('USER_LLM_API_KEY', '')
        _mg = cfg.model or _os_g.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    # 把 model_name 变量同步为真实值，避免 system_prompt 末尾写"当前模型：(空)"
    _var_ctx['model_name'] = _mg
    gw = LLMGateway(_bg, _kg, _mg)
    # P1-5 MCP: 读 MCP_SERVERS_JSON → 为通用聊天加载 function calling tools（不改动其他维度的创作链路）
    mcp_tools: List[Dict[str, Any]] = []
    _mcp_registry = None
    try:
        from mcp_client import MCPToolRegistry
        _mcp_registry = MCPToolRegistry()
        mcp_tools = _mcp_registry.available_tools_for_llm()
    except Exception:
        mcp_tools = []

    def generate():
        yield ': ping-heartbeat-keepalive\n\n'
        full_text = []
        try:
            # P1-3 首帧 ⓪：把"正在生效的角色+上下文变量"告诉前端（保证刷新后UI显示的角色和后端真实生效的一致）
            yield f'data: {json.dumps({"type": "meta", "kind": "role_applied", "info": _p13_meta}, ensure_ascii=False)}\n\n'
            # 首帧 ①：命中维度建议（前端弹气泡"是否落入XX维度？"）
            if hit_suggestions:
                yield f'data: {json.dumps({"type": "meta", "kind": "hit_suggestions", "info": {"suggestions": hit_suggestions}}, ensure_ascii=False)}\n\n'
            # 首帧 ②：扫榜意图识别（前端自动弹出Step1入口）
            scan_intent = any(k in message for k in [
                '扫榜', '热榜', '爆款', '番茄小说', '起点中文', '七猫', '排行榜',
                '现在什么火', '什么书火', '趋势', '看看榜单',
            ])
            if scan_intent:
                yield f'data: {json.dumps({"type": "meta", "kind": "scan_intent", "info": {"detected": True}}, ensure_ascii=False)}\n\n'
            # P1-5 MCP: 有已注册 tools → 告诉前端"本次对话启用MCP tools N个"，并把 tools 透传给 LLM payload
            _mcp_native_kwargs: Dict[str, Any] = {}
            if mcp_tools:
                yield f'data: {json.dumps({"type": "meta", "kind": "mcp_tools", "info": {"count": len(mcp_tools)}}, ensure_ascii=False)}\n\n'
                _mcp_native_kwargs['tools'] = mcp_tools

            # ============== P0 真联网搜索接入 ==============
            # 0) 先把用户配置的搜索 Key 同步进 env（无 env 时才用；配置路由保存后立即生效）
            _sync_search_keys_from_preference()
            # 1) 是否需要搜：联网开关开启=强制搜（绕开创作类话题不搜的过滤）；未开启=启发式判定
            _need_search = _search_available and (web_search_enabled or should_use_web_search(message, trimmed))
            _search_ctx = ''
            if _need_search:
                try:
                    # 2) 先推"🔍联网搜索中…" meta 帧，前端马上给用户反馈
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_started", "info": {"query": message[:200]}}, ensure_ascii=False)}\n\n'
                    # 3) 同步执行搜索：Tavily/Exa/Brave（有 Key 时）→ DuckDuckGo HTML 兜底
                    _sr = run_web_search(message, num_results=5, timeout_per_engine=6.0)
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_done", "info": _sr.to_dict() if hasattr(_sr, "to_dict") else {"ok": False, "engine": getattr(_sr, "engine", ""), "count": 0, "error": getattr(_sr, "error", "")}}, ensure_ascii=False)}\n\n'
                    # 4) 把搜索结果格式化成 Markdown 列表，追加到用户消息末尾注入给 LLM
                    try:
                        _search_ctx = format_search_context_for_llm(_sr)
                    except Exception:
                        _search_ctx = ''
                except Exception as _se:
                    # 搜索失败绝对不打断主聊天链路
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_done", "info": {"ok": False, "engine": "", "count": 0, "error": str(_se)[:300]}}, ensure_ascii=False)}\n\n'
                    _search_ctx = ''
            # 5) 如果搜到资料，追加到本轮 user message 末尾（放在最后，LLM 注意力最高）
            if _search_ctx:
                # messages 是外层变量引用，修改会生效到 gw_stream_with_hb
                if messages and messages[-1].get('role') == 'user':
                    messages[-1]['content'] = (str(messages[-1].get('content', '')) + '\n\n' + _search_ctx)[:12000]
            # 6) 模型原生联网参数（智谱 GLM 开原生 web_search 工具，质量比独立搜索更高；不消耗第三方 Key）
            try:
                _native_p = get_native_websearch_params(_mg, _bg, enabled=_need_search or (scan_intent and _search_available))
                if _native_p and isinstance(_native_p, dict):
                    # deep-merge 到 _mcp_native_kwargs（tools 数组保留，extra_body 解包）
                    if 'extra_body' in _native_p and isinstance(_native_p['extra_body'], dict):
                        _ex = _mcp_native_kwargs.setdefault('extra_body', {})
                        for _kk, _vv in _native_p['extra_body'].items():
                            if _kk == 'tools' and isinstance(_vv, list):
                                _ex['tools'] = list(_ex.get('tools') or []) + list(_vv)
                            else:
                                _ex[_kk] = _vv
                    elif 'tools' in _native_p and isinstance(_native_p['tools'], list):
                        _mcp_native_kwargs['tools'] = list(_mcp_native_kwargs.get('tools') or []) + list(_native_p['tools'])
            except Exception:
                _native_p = None

            # ====================================================================
            # 【榜单分析师专属·自动扫榜·流式响应期内执行（连接稳定=不超时）】
            # ====================================================================
            #   - 外层 _rank_analyst_snapshot.active = True 才进入；其他角色完全不沾
            #   - 先推一帧 roundtable_status（通用聊天也能显示），用户感知"正在抓榜"
            #   - 扫完把报告追加到 messages[0]（system prompt 末尾）→ LLM 立刻能基于风向回答
            #   - 100% try/except：失败 = 当没发生，榜单分析师正常回答（不拍脑袋即可），绝不崩溃原聊天
            # ====================================================================
            nonlocal system_prompt
            try:
                if _rank_analyst_snapshot.get('active'):
                    yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "🧾 榜单分析师正在扫描番茄/起点新书榜，整理市场风向…（约8-15秒，说起点即扫起点榜，默认番茄）"}}, ensure_ascii=False)}\n\n'
                    _a = _rank_analyst_snapshot
                    _rs_rank, _rs_sse = _auto_rank_scan_from_nl(
                        _a.get('message', ''),
                        fallback_concept=_a.get('fallback_concept', '') or '',
                        book_title=_a.get('book_title', '') or '',
                        explicit_rank_scan=True,
                    )
                    if _rs_rank:
                        def _md_report_gen(rp: dict) -> str:
                            lines: list[str] = []
                            lines.append(f"📈 本轮真实扫榜情报（平台：{rp.get('platform_label','番茄新书榜')}）")
                            if rp.get('scan_time'):  lines.append(f"· 扫榜时间：{rp['scan_time']}")
                            if rp.get('subcategory_label'): lines.append(f"· 命中赛道：{rp['subcategory_label']}")
                            if rp.get('books') and isinstance(rp['books'], list):
                                tops = rp['books'][:5]
                                lines.append(f"· TOP{len(tops)} 同类题材上榜书（书名+一句话钩子+作者）：")
                                for i, b in enumerate(tops, 1):
                                    parts = []
                                    if b.get('title'): parts.append(str(b['title']))
                                    if b.get('hook_1line'): parts.append(str(b['hook_1line']))
                                    if b.get('author'): parts.append(f"作者：{b['author']}")
                                    lines.append(f"  {i}. " + " ｜ ".join(parts) if parts else f"  {i}. {b}")
                            for key, zh in [('reader_buy_points', '读者买单要素（共性卖点）'),
                                            ('reader_abandon_points', '读者弃文毒点（共性避坑）'),
                                            ('title_formula_examples', '书名公式参考'),
                                            ('opening_hook_templates', '开篇钩子套路模板'),
                                            ('market_advice', '市场落地方向建议')]:
                                v = rp.get(key)
                                if isinstance(v, str) and v.strip():
                                    lines.append(f"\n【{zh}】\n{v.strip()}")
                                elif isinstance(v, list) and v:
                                    lines.append(f"\n【{zh}】")
                                    for it in v:
                                        lines.append(f"- {it}")
                            return "\n".join(lines).strip()
                        _report_gen = _md_report_gen(_rs_rank)
                        if _report_gen:
                            # 把扫榜报告追加到 system prompt 末尾（对榜单分析师 persona 再强化一次）
                            _appendix = (
                                "\n\n================================\n"
                                "【★★★ 本轮对话前置·系统已自动扫榜成功 ★★★】\n"
                                "下面是刚从番茄/起点新书榜抓回来的真实榜单风向情报（TOP5/买卖点/书名公式/钩子/建议）——"
                                "**你必须优先吸收：回答开头先给用户展示情报摘要，再基于这些情报回答，不拍脑袋。**\n\n"
                                + _report_gen + "\n================================\n"
                            )
                            system_prompt = system_prompt.rstrip() + _appendix
                            if messages and messages[0].get('role') == 'system':
                                messages[0]['content'] = system_prompt
                            # 把复杂 text 先提变量，彻底避免嵌套 f-string + json.dumps 里写 \"（Python 语法不允许在 f-string {} 内用 backslash）
                            _plat = _rs_rank.get('platform_label', '番茄新书榜') if isinstance(_rs_rank, dict) else '番茄新书榜'
                            _nb = len((_rs_rank.get('books') or []) if isinstance(_rs_rank, dict) else [])
                            _sse_meta_obj = {
                                'type': 'meta',
                                'kind': 'roundtable_status',
                                'info': {
                                    'text': f'✅ 扫榜完成：{_plat}｜命中 {_nb} 本TOP书，接下来基于风向构思。'
                                }
                            }
                            yield f'data: {json.dumps(_sse_meta_obj, ensure_ascii=False)}\n\n'
            except Exception:
                pass  # 扫榜失败 = 静默跳过，不打断任何主流程

            # 原生思考推理程度控制（智谱 GLM）：GLM-5.3 强制思考、思考与正文共享
            # max_tokens——无法"思考不计入消耗"，只能按 deep_think 下发 reasoning_effort
            # 控制思考深度，避免思考先占满 max_tokens 导致正文为空（配合 chat_stream 的
            # "思考耗尽自动翻倍 max_tokens"双重兜底）。
            try:
                _nk = _native_reasoning_kwargs(_mg, deep_think)
                if _nk:
                    _mcp_native_kwargs.update(_nk)
            except Exception:
                pass

            # 通用聊天 max_tokens 按模型能力"不限"：给足 _DIM_MAX_TOKENS(131072)，
            # 交由 llm_gateway._effective_max_tokens 按模型已知/自学习输出上限钳制（deepseek→8192、
            # glm-5.3→131072…），不再按 deep_think 分档缩小；思考型模型正文为空时还有
            # chat_stream 的"思考耗尽自动翻倍 max_tokens"兜底。
            # emit_reasoning=True：思考文本以 SSE meta(kind=reasoning) 单独推前端（可展开看，不混正文）。
            # 【思考切分】deep_think>=1 时额外用 _ThinkingSplitter 把提示词式"【推理】…【推理结束】"
            # 包裹的思考从正文剥离展示；与原生 reasoning_content 两条路径并存，结果 content/卡片/落盘均不含思考。
            _splitter = _ThinkingSplitter() if deep_think >= 1 else None
            for chunk in gw_stream_with_hb(gw, messages, emit_reasoning=True,
                                           temperature=({0: 0.7, 1: 0.5, 2: 0.3}.get(deep_think)), max_tokens=_DIM_MAX_TOKENS, **_mcp_native_kwargs):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                if _is_stream_retry(chunk):
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": chunk.info}, ensure_ascii=False)}\n\n'
                    continue
                if _is_reasoning_frame(chunk):
                    # 原生 reasoning_content 思考 → 单独透传，不 append 进 full_text（结果/卡片/落盘只含最终回复）
                    yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": chunk.text}, ensure_ascii=False)}\n\n'
                    continue
                # 深思考标记式：把正文流切成 body / reason 两部分分发
                for _pk, _pt in (_splitter.feed(chunk) if _splitter else [('body', chunk)]):
                    if _pk == 'reason':
                        yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _pt}, ensure_ascii=False)}\n\n'
                    else:
                        full_text.append(_pt)
                        yield f'data: {json.dumps({"type": "delta", "content": _pt}, ensure_ascii=False)}\n\n'
            # 流结束：冲刷深思考切分器缓存的尾巴（避免正文/思考尾部被丢弃）
            if _splitter is not None:
                for _fk, _ft in _splitter.finish():
                    if _fk == 'reason':
                        yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _ft}, ensure_ascii=False)}\n\n'
                    else:
                        full_text.append(_ft)
                        yield f'data: {json.dumps({"type": "delta", "content": _ft}, ensure_ascii=False)}\n\n'

            complete = ''.join(full_text)
            # ======= 节点设计师：流正常结束（含中途截断/没生成完）→ 更新续会 last_ch/volume_index =======
            if _nd_is_node_role and session and _nd_meta_for_closure:
                try:
                    _lc = _parse_last_chapter_from_text(complete)
                    _vi = _parse_volume_index_from_text(complete) or int(_nd_meta_for_closure.get('volume_index') or 1)
                    _cpv = max(10, int(_nd_meta_for_closure.get('cpv') or 50))
                    _new_state = dict(_nd_meta_for_closure)
                    _new_state['volume_index'] = _vi
                    _new_state['cpv'] = _cpv
                    if isinstance(_lc, int) and _lc > int(_new_state.get('last_ch') or 0):
                        _new_state['last_ch'] = _lc
                    _new_state['updated_at'] = datetime.now(timezone.utc).isoformat()
                    # 若本次 AI 输出里发现 CPV 信息（例如卷标题里"第X卷（共N章）"）→ 同步升级
                    try:
                        _mx_cpv = re.search(r'(?:共|全|每卷|chapter_count|cpv)\s*[=:：为是]*\s*(\d{1,3})\s*章', complete or '')
                        if _mx_cpv:
                            _cand = max(10, min(200, int(_mx_cpv.group(1))))
                            if _cand >= int(_new_state.get('last_ch') or 0):
                                _new_state['cpv'] = _cand
                    except Exception:
                        pass
                    _nd_save_state(session, db, _new_state)
                except Exception:
                    pass
            cards = parse_cards(complete)
            for c in cards:
                c['content'] = _clean_text_to_plain(c.get('content', ''))
                if c.get('title'):
                    c['title'] = _clean_text_to_plain(c['title'])
            # ======= 节点设计师：全卷已收尾(last_ch>=cpv) 但模型没吐完整全卷卡片 → 后端自动从历史所有分段卡片合并一张全卷统一采纳卡片 =======
            if _nd_is_node_role and _nd_meta_for_closure:
                try:
                    _vi = int(_nd_meta_for_closure.get('volume_index') or 1)
                    _cpv = max(10, int(_nd_meta_for_closure.get('cpv') or 50))
                    _final_lc = int(_parse_last_chapter_from_text(complete) or _nd_meta_for_closure.get('last_ch') or 0)
                    _is_full = _final_lc >= _cpv
                    # 若模型卡片的 nodes 不完整也同样兜底补齐：检查 cards 中 SAVE_PLOT 的总节点数
                    _existing_nodes_count = 0
                    for c in cards:
                        if c.get('type') != 'SAVE_PLOT':
                            continue
                        try:
                            arr = json.loads(c.get('content') or '[]')
                            if isinstance(arr, list):
                                for v in arr:
                                    if isinstance(v, dict) and isinstance(v.get('nodes'), list):
                                        _existing_nodes_count += len(v['nodes'])
                        except Exception:
                            pass
                    if _is_full and (_existing_nodes_count < _cpv or not cards):
                        # 收集所有历史分段卡片 + 本次 complete 里的卡片 → 合并成全卷统一卡片
                        all_vols, dvi, dcpv = _nd_collect_all_save_plot_volumes(history, complete)
                        vi_for_build = dvi or _vi
                        cpv_for_build = dcpv or _cpv
                        if all_vols or _is_full:
                            # 即使 all_vols 为空，只要 _is_full 并且 nodes 数 < cpv，也兜底：把 complete 里的节点再解析一次（走 _build_full_volume → A+C 修复自动补齐占位）
                            built = _nd_build_full_volume_card(all_vols, vi_for_build, cpv_for_build)
                            if built:
                                # 把这张兜底卡片追加到 cards（放在最末，前面模型给的卡片如果节点数不够也保留，不会冲突）
                                built['content'] = _clean_text_to_plain(built.get('content', ''))
                                if built.get('title'):
                                    built['title'] = _clean_text_to_plain(built['title'])
                                # 为避免重复：如果 cards 里已经有一张 SAVE_PLOT 节点数=cpv 并且 volume_index=vi_for_build → 不追加
                                _already_full = False
                                for c in cards:
                                    if c.get('type') != 'SAVE_PLOT':
                                        continue
                                    try:
                                        arr = json.loads(c.get('content') or '[]')
                                        if isinstance(arr, list) and len(arr) == 1 and isinstance(arr[0], dict):
                                            if int(arr[0].get('volume_index') or 0) == int(vi_for_build) and len(arr[0].get('nodes') or []) >= int(cpv_for_build):
                                                _already_full = True
                                                break
                                    except Exception:
                                        pass
                                if not _already_full:
                                    cards.append(built)
                except Exception:
                    pass
            for card in cards:
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            clean_text = _clean_text_to_plain(strip_cards(complete))
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending'} for c in cards]
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text,
                            'cards': persisted_cards})
            _safe_save_session_messages(session, history)
            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            import traceback
            traceback.print_exc()
            # ======= 节点设计师：异常退出（Render断连/超时/模型错误）→ 也要把已输出到的 last_ch 存起来，支持续会 =======
            if _nd_is_node_role and session and _nd_meta_for_closure:
                try:
                    partial = ''.join(full_text)
                    _lc = _parse_last_chapter_from_text(partial)
                    _vi = _parse_volume_index_from_text(partial) or int(_nd_meta_for_closure.get('volume_index') or 1)
                    _new_state = dict(_nd_meta_for_closure)
                    _new_state['volume_index'] = _vi
                    if isinstance(_lc, int) and _lc > int(_new_state.get('last_ch') or 0):
                        _new_state['last_ch'] = _lc
                    _new_state['updated_at'] = datetime.now(timezone.utc).isoformat()
                    _nd_save_state(session, db, _new_state)
                except Exception:
                    pass
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


@chat_collab_bp.route('/api/ai/chat/roundtable', methods=['POST'])
def chat_roundtable():
    """圆桌会议（多 Agent 轮询讨论，参考 AutoGen RoundRobinGroupChat 固定顺序轮流发言）：
    - 7个内置专家按固定顺序轮流发言：【榜单分析师（首位，先扫榜定风向）】→毒舌读者→剧情架构师→世界观策划→爆款编辑→润色编辑→深度采访
    - 完整走两轮，让交锋充分深入
    - 每步实时SSE推给前端，用户可以看到整个讨论过程
    - 讨论结束主持人做总结报告，输出共识+结论+落地步骤
    - body: { book_id?, session_id?, topic }  (book_id可选，绑定作品时注入维度资料)
    """
    from app import db, AISession, Book, BookBible, AIConfig
    from llm_gateway import LLMGateway, get_llm_config

    data = request.json or {}
    book_id = data.get('book_id')
    session_id = data.get('session_id')
    topic = (data.get('topic') or '').strip()
    # P0 深度思考级别：统一用标准思考，保证讨论质量
    deep_think = 1
    # P0 榜单风向：先扫榜再开会，把市场风向注入主持人/所有专家/总结报告
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None
    # 【rt-header右上角"继续"按钮专用】：前端点继续不新增用户气泡，传的 topic 仍是原始议题（不是"继续"）
    # 所以必须靠这个独立布尔位强制命中 resuming/append_mode，避免走到"全新会议"分支重开场（榜单分析师从头来）
    resume_from_checkpoint = bool(data.get('resume_from_checkpoint'))
    # ================== 【圆桌·自动扫榜增强·安全版】==================
    # 自动扫榜 = 只在"全新会议"里触发一次（generate() 内部的全新会议分支里执行）。
    # 绝对不在续会/追加/调整/创作阶段触发，因为：
    #   1) 续会时用户的 topic 是"继续"两字，用它去扫榜 = 扫出一堆无关的TOP书 = 垃圾数据
    #   2) 扫榜要8~20s 网络/LLM 调用 → 放 chat_roundtable 外层 = 任何"继续"都先卡8~20s = 用户感知"继续功能没了/卡死/断掉"
    #   3) 调整阶段用户 topic 是"对总结的修改意见"= 也不该扫，拿上次议题扫出来的用就行
    # 所以下面两个变量只做占位，真正赋值在 generate() 内部"全新会议"分支：
    _rank_ctx_global = _format_rank_context(_rank_scan)
    _rank_analyst_report: str = ''

    if not topic:
        return jsonify({'error': '缺少讨论话题'}), 400

    # book_id 为空 = 纯自由讨论；绑定则注入作品维度资料
    scope = 'roundtable_global' if not book_id else 'roundtable_per_book'
    book_title = ''
    bb_summary = ''
    book = None
    bb = None
    base_system = ''
    if book_id:
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        book_title = book.title or ''
        bb = BookBible.query.filter_by(book_id=book_id).first()
        from app import Chapter, parse_chapter_number
        recent_chapters: list = []
        next_chapter_num: int | None = None
        toc_block = ''
        try:
            ch_info = _get_latest_chapter_info(book_id)
            next_chapter_num = ch_info['next_num']
            recent_raw = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
            def _ck(c):
                n = parse_chapter_number(c.title or '')
                return n if isinstance(n, int) and n > 0 else (99999 + int(c.order_index or 0))
            recent_sorted = sorted(recent_raw, key=_ck)
            recent_chapters = [
                {
                    'title': ch.title or f'第{ch.order_index or 0}章',
                    'word_count': getattr(ch, 'word_count', 0) or 0,
                    'order_index': int(ch.order_index or 0),
                } for ch in recent_sorted[-5:]
            ]
        except Exception:
            recent_chapters = []
            next_chapter_num = None
        try:
            toc_block = _build_toc_block(book_id)
        except Exception:
            toc_block = ''
        try:
            from prompt_context_cache import PromptContextCache
            _cache = PromptContextCache.get_instance()
            _cache_key = f'general_chat_system:{book_id}'
            def _builder():
                return build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
            base_system = _cache.get_or_build(_cache_key, _builder, ttl_sec=900)
        except Exception:
            base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
        non_empty_fields = [f for f in [
            ('concept', '核心构思'), ('worldbuilding', '世界观'), ('key_rules', '核心规则'),
            ('character_profiles', '人物'), ('plot_design', '大纲'),
            ('timeline', '剧情线'), ('foreshadowing', '伏笔'),
            ('locations', '地点'), ('style_guide', '文风'),
        ] if getattr(bb, f[0], None) and str(getattr(bb, f[0])).strip()]
        bb_summary = '、'.join(nf[1] for nf in non_empty_fields) if non_empty_fields else '暂无已填充维度'

    # 会话创建/获取
    if not book_id:
        session = AISession.query.filter(
            AISession.scope == 'roundtable_global',
            AISession.title == '圆桌会议',
        ).order_by(AISession.updated_at.desc()).first()
        if not session:
            session = AISession(id=str(uuid.uuid4()), scope='roundtable_global',
                                title='圆桌会议', book_id=None,
                                messages_json='[]', created_at=datetime.now(timezone.utc),
                                updated_at=datetime.now(timezone.utc))
            db.session.add(session); db.session.commit()
        session_id = session.id
    else:
        session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=topic[:30])
        session_id = session.id

    # P1-1 模型配置解析（与 chat_general 完全一致，复用会话级模型配置逻辑）
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    session_cfg_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            session_meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            session_cfg_id = (session_meta.get('ai_config_id') or '').strip() or None
    except Exception:
        session_cfg_id = None
    chosen_cfg_id = req_ai_config_id or session_cfg_id
    cfg = AIConfig.get_by_id(chosen_cfg_id) if chosen_cfg_id else None
    if cfg and not cfg.api_key:
        cfg = None
    if cfg is None:
        cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    if chosen_cfg_id and chosen_cfg_id == cfg.id and session:
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict): meta = {}
            if meta.get('ai_config_id') != cfg.id:
                meta['ai_config_id'] = cfg.id
                session.meta_json = json.dumps(meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass

    # 模型URL/KEY解析（与 chat_general 完全一致）
    import os as _os_g
    from llm_gateway import _normalize_llm_base_url as _nlg
    import app as _modg
    try:
        _actg = _modg.AIConfig.get_active()
        _actg_id = getattr(_actg, 'id', None) if _actg else None
    except Exception:
        _actg_id = None
    _is_act_g = (_actg_id and chosen_cfg_id and _actg_id == chosen_cfg_id) or (not chosen_cfg_id)
    if _is_act_g:
        _bg, _kg, _mg = get_llm_config(_modg)
        if cfg.model and cfg.model != _mg:
            _mg = cfg.model
    else:
        _bg = _nlg(cfg.base_url or _os_g.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1'), cfg.model)
        _kg = cfg.api_key or _os_g.environ.get('USER_LLM_API_KEY', '')
        _mg = cfg.model or _os_g.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    # 运行时变量
    from datetime import datetime, timezone, timedelta
    _tz = timezone(timedelta(hours=8))
    _now = datetime.now(_tz)
    _var_ctx = {
        'date': _now.strftime('%Y-%m-%d'),
        'time': _now.strftime('%H:%M'),
        'current_book': book_title or '(未绑定作品)',
        'model_name': _mg,
    }
    def _var_replace(s: str) -> str:
        if not s: return s
        for k, v in _var_ctx.items():
            s = s.replace('{' + k + '}', str(v))
        return s

    # 加载历史（保存完整讨论过程供后续复盘）
    history = load_session_messages(session)

    def generate():
        yield ': ping-heartbeat-keepalive\n\n'
        nonlocal _rank_ctx_global, _rank_analyst_report, _rank_scan
        all_messages = []

        def _emit(gen, speaker_id):
            # 把 _rt_stream_turn 的 tag 流翻译成 SSE 帧；正文累积进外层 full_parts
            for _tag, _pay in gen:
                if _tag == 'hb':
                    yield f'{SSE_HEARTBEAT_COMMENT}'
                elif _tag == 'reason':
                    yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _pay}, ensure_ascii=False)}\n\n'
                elif _tag == 'retry':
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": _pay}, ensure_ascii=False)}\n\n'
                elif _tag == '__done__':
                    full_parts.append(_pay)
                else:
                    full_parts.append(_pay)
                    yield f'data: {json.dumps({"type": "delta", "speaker": speaker_id, "content": _pay}, ensure_ascii=False)}\n\n'

        try:
            N = len(_ROUNDTABLE_ORDER)
            default_rounds = 2
            # 【轮数可配】解析作者本轮要求的讨论轮数（如"讨论3轮"/"开4轮"/"谈个5轮"）。
            # 未命中则按默认(2轮)；命中则「全新会议」首轮按 {轮数}×6位专家 开完。
            _round_req = None
            _rm = re.search(r'(?:讨论|开会|开|谈|聊|辩|进行)\s*([1-9]\d?)\s*(?:轮|圈|回合)', topic)
            if _rm:
                _round_req = int(_rm.group(1))
            # 首帧meta：告诉前端这是圆桌模式（rounds 反映实际轮数：默认2轮；作者指定则按指定值）
            _hint_rounds = (_round_req if _round_req else default_rounds)
            yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_start", "info": {"rounds": _hint_rounds, "speakers": N}}, ensure_ascii=False)}\n\n'

            is_continue = _is_rt_continue(topic)
            state = _rt_load_state(session)

            # 会议已完成 + 用户发"继续/会议继续/追加一轮" → 交给 append_mode 追加新一轮
            # 会议已完成 + 用户发新话题（非续会指令）→ 自动落入下方"新会议"，不重复上一场

            # 模式判定：
            #  append_mode —— 已整体完成，用户说"继续/会议继续/追加一轮"→ 追加新一轮
            #  adjust_mode —— 已整体完成，用户发的是对结论的"自然意见/反馈"（非"继续"关键词）→ 调整阶段
            #                 （主持确认意见→专家逐一回应意见收敛修正→出调整结论+更新可采纳卡片）
            #  resuming    —— 开会中途断连/手动停止，接着剩余回合开完既定轮数
            #  其余         —— 全新会议（两轮）
            # 【创作模式】作者要求"按讨论结果创作各维度/某维度" → 直接产出可采纳卡片
            _create_dims = _rt_parse_create_dims(topic) if (state and state.get('completed')) else None
            create_mode = bool(_create_dims is not None)
            append_mode = bool(is_continue and state and state.get('completed'))
            # 已完成 + 用户发的不是"继续"/创作指令而是自然反馈 → 进入调整阶段，复用上次议题与讨论上下文
            adjust_mode = bool(not is_continue and not create_mode and state and state.get('completed') and (topic or '').strip())
            resuming = bool(is_continue and state and state.get('active') and not state.get('completed'))

            # ⭐【rt-header右上角绿色"继续"按钮强制续会覆盖】
            # 用户点击继续按钮，前端不传"继续"topic（传原始议题），所以上面 is_continue=False，
            # 必须在 resume_from_checkpoint=true 时强制 is_continue=True 并重算 append_mode/resuming。
            if resume_from_checkpoint:
                is_continue = True
                if state and isinstance(state, dict):
                    if state.get('completed'):
                        # 已完成一轮以上 → 追加新一轮（与 append_mode 原语义一致）
                        append_mode = True
                        resuming = False
                        adjust_mode = False
                    else:
                        # 任何中途未完成状态：active=True/False/缺失 → 一律进入 resuming，绝不再走全新会议
                        # （之前 active 标志可能被异常路径漏写，这里兜底强制续会）
                        append_mode = False
                        resuming = True
                        adjust_mode = False
                        state['active'] = True
                        state['completed'] = False
                        if not state.get('phase'):
                            state['phase'] = 'resumed'
                # state is None（极少：DB 清理 / 首次开会被截断在主持人开场前）→ 不设置任何模式，
                # 走 else 全新会议兜底，避免报错误死流程

            if create_mode:
                # ========== 创作模式：按讨论共识创作维度 → 产出标准可采纳卡片 ==========
                try:
                    from app import BookBible as _ModBookBible
                    _bb = _ModBookBible.query.filter_by(book_id=book_id).first() if book_id else None
                except Exception:
                    _bb = None
                # 讨论共识源：优先当前 state 全量讨论记录 + 最近落盘总结报告
                _consensus = (state.get('discussion_history') or f'【议题】\n{topic}\n\n')
                try:
                    _hist_sum = ''
                    for _m in reversed(history or []):
                        if isinstance(_m, dict) and _m.get('role') == 'assistant' and '总结报告' in str(_m.get('content', '')):
                            _hist_sum = str(_m.get('content', ''))[:8000]
                            break
                except Exception:
                    _hist_sum = ''
                if _hist_sum:
                    _consensus = f'{_consensus}\n\n【最终总结报告】\n{_hist_sum}'
                # 没有识别出具体维度 → 默认全部
                if not _create_dims:
                    _create_dims = list(_RT_CREATE_ALL)
                _gw_c = LLMGateway(_bg, _kg, _mg)
                _bb_existing = {}
                if _bb:
                    for _fk in _RT_CREATE_FIELD:
                        try:
                            _v = getattr(_bb, _RT_CREATE_FIELD[_fk], None)
                            if _v and str(_v).strip():
                                _bb_existing[_fk] = str(_v)
                        except Exception:
                            pass
                _iron = _core_params_iron_block(_bb, book) if (book and _bb) else ''
                for _dk in _create_dims:
                    _label, _ctype = _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))
                    # 注意：f-string 表达式中不能含反斜杠（Python<3.12/PEP701 之前），故把 \n 预计算成变量
                    _create_msg = "\n\n📌 正在按讨论结果创作【" + _label + "】…\n\n"
                    yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": _create_msg}, ensure_ascii=False)}\n\n'
                    _sys = _rt_create_dimension_system(_dk, book, _iron, _consensus, _bb_existing.get(_dk, ''))
                    if _rank_ctx_global:
                        _sys = _sys.rstrip() + '\n\n' + _rank_ctx_global
                    _cre_full = []
                    for _tk2, _tp2 in _rt_stream_turn(_gw_c, [
                        {'role': 'system', 'content': _var_replace(_sys)},
                        {'role': 'user', 'content': f'请按讨论结论创作《{"book_title" if book else "本书"}》的【{_label}】维度'},
                    ], 0.7, _dim_max_tokens(_dk), attempts=2):
                        # body 为正文增量；__done__ 是全文汇总，跳过避免重复
                        if _tp2 is None or _tk2 == '__done__':
                            continue
                        if _tk2 == 'body':
                            _cre_full.append(_tp2)
                        yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": _tp2}, ensure_ascii=False)}\n\n'
                    _content = ''.join(_cre_full).strip()
                    _content = _clean_text_to_plain(_content)
                    _card = {
                        'id': str(uuid.uuid4())[:8],
                        'type': _ctype,
                        'title': f'{_label}（按圆桌讨论创作）',
                        'content': _content,
                        'target': _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))[0],
                    }
                    _enrich_card_rank_meta(_card, _rank_scan)
                    yield f'data: {json.dumps({"type": "card", "card": _card, "session_id": session_id}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator_summary"}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
                return
            elif append_mode:
                # ========== 追加一轮：沿用上次议题与全部发言，继续开新一轮 ==========
                state['completed'] = False
                state['active'] = True
                topic_final = state.get('topic') or topic
                done = state.get('done', []) or []
                discussion_history = state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n'
                if state.get('moderator_open'):
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{state['moderator_open']}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                _nxt = 1 + len(done) // N
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0], "round": _nxt}}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": f"（已读到之前的完整讨论，现在追加一轮：第{_nxt}轮继续深挖…）"}, ensure_ascii=False)}\n\n'
                target_total = len(done) + N
                # 立即落盘一次（刷新即可见历史），随后继续发言
                _rt_persist_messages(session, history, topic_final, state.get('moderator_open', ''), done, '')
            elif adjust_mode:
                # ========== 调整阶段：作者对总结报告给出意见 → 专家逐一回应意见并收敛修正 ==========
                state['completed'] = False
                state['active'] = True
                state['phase'] = 'adjust'
                topic_final = state.get('topic') or topic
                mod_open = state.get('moderator_open', '')
                done = state.get('done', []) or []
                feedback = (topic or '').strip()
                # 把作者意见追加进讨论记录 → 后续专家发言都能读到并回应
                discussion_history = (state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n')
                if mod_open:
                    discussion_history += f"\n【上次主持人开场】\n{mod_open}\n\n"
                discussion_history += f"\n【作者意见（调整阶段）】\n{feedback}\n\n"
                if mod_open:
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{mod_open}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                # 主持人开场：复述作者意见，说明本轮要重新审视并收敛修正，把场子交给专家
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0], "round": (1 + len(done)//N), "phase": "adjust"}}, ensure_ascii=False)}\n\n'
                adj_system = _MODERATOR_ROLE[1] + f"""

【议题】{topic_final}

【作者意见】
{feedback}

【场景】这是圆桌会议总结报告出具后，作者对新结论提出了具体意见/调整方向，进入"调整阶段"。
【任务】你现在以主持人身份开场（3-4句话）：先复述作者意见的核心点，说明本轮专家将围绕该意见重新审视并收敛修正此前结论，然后点出你最想先听哪位专家回应，把场子交给专家。不要展开论证。
"""
                if book_id and base_system:
                    adj_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。\n\n" + adj_system
                adj_system = adj_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    adj_system = adj_system.rstrip() + '\n\n' + _rank_ctx_global
                adj_msgs = [{'role': 'system', 'content': _var_replace(adj_system)},
                            {'role': 'user', 'content': f'主持人开场，议题：{topic_final}，作者意见：{feedback}'}]
                gw_mod = LLMGateway(_bg, _kg, _mg)
                full_parts = []
                for f in _emit(_rt_stream_turn(gw_mod, adj_msgs, 0.6, _DIM_MAX_TOKENS), 'moderator'):
                    yield f
                adj_open = ''.join(full_parts)
                all_messages.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{adj_open}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator"}, ensure_ascii=False)}\n\n'
                state['moderator_open'] = adj_open
                state['discussion_history'] = discussion_history
                _rt_save_state(session, db, state)
                # 落盘一次（刷新可见历史 + 本轮作者意见）
                _rt_persist_messages(session, history, topic_final, adj_open, done, '')
            elif resuming:
                # ========== 续会：沿用上次的议题与进度，接着剩余回合开会 ==========
                topic_final = state.get('topic') or topic
                done = state.get('done', []) or []
                discussion_history = state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n'
                if state.get('moderator_open'):
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{state['moderator_open']}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                # ⚠️ 【关键修复】绝对不要再 yield roundtable_speaker(moderator)！
                #    之前的写法会触发前端切换"当前发言人=主持人"→追加一个主持人 speech 气泡
                #    → 用户观感：点继续后"又从主持人开始重新说了"。
                # 正确做法：静默给一条 roundtable_status 状态提示（不进 speech、不切发言人），
                # 然后直接 fall-through 到 while 循环，从 len(done) 对应的下一位专家继续。
                _len_done = len(done)
                if _len_done == 0 and not state.get('moderator_open'):
                    # 极端兜底：连主持人开场都没存上（例如用户在主持人 LLM 生成时就强制关流），
                    # 视为新会议重开，保证不空白卡死 / 不产出 0 条讨论记录
                    resuming = False
                    state = None
                else:
                    _next_round = 1 + _len_done // N
                    _next_idx = _len_done % len(_ROUNDTABLE_ORDER)
                    _next_id = _ROUNDTABLE_ORDER[_next_idx] if 0 <= _next_idx < len(_ROUNDTABLE_ORDER) else _ROUNDTABLE_ORDER[0]
                    _next_name = _PERSONAS[_next_id][0] if _next_id in _PERSONAS else _next_id
                    yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": f"⏯️ 续会：已保留 {_len_done} 位发言 · 第{_next_round}轮 · 下一位：{_next_name}（不重复开场、不重复榜单分析师）"}}, ensure_ascii=False)}\n\n'
                # 立即落盘一次（刷新即可见已完成的发言）
                _rt_persist_messages(session, history, topic_final, state.get('moderator_open', '') if isinstance(state, dict) else '', done, '')
            else:
                # ========== 全新会议：主持人开场 ==========
                # --- Step 0：【圆桌自动扫榜·只在全新会议触发】---
                # 续会/追加/调整/创作模式一律跳过，避免卡死用户"继续"按钮的响应。
                # 如果用户没传 preset rank_scan → 按"议题关键词决定平台"自动扫一次
                if not _rank_scan and topic:
                    try:
                        # 先给前端推一帧扫榜提示（用户感知到"正在抓榜"，不会以为卡死）
                        yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "🧾 榜单分析师正在扫描番茄/起点新书榜，整理市场风向…（约8-15秒）"}}, ensure_ascii=False)}\n\n'
                        _rt_bb_fc2 = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
                        _rt_fc2 = (_rt_bb_fc2.concept or _rt_bb_fc2.master_outline or '').strip() if _rt_bb_fc2 else ''
                        _auto_rs2, _auto_sse2 = _auto_rank_scan_from_nl(
                            topic, fallback_concept=_rt_fc2, book_title=book_title or '',
                            explicit_rank_scan=True
                        )
                        if _auto_rs2:
                            _rank_scan = _auto_rs2
                            # 重构两个全局供后续所有 expert/moderator/summary 使用
                            _rank_ctx_global = _format_rank_context(_rank_scan)

                            def _rt_report_md2(rp: dict) -> str:
                                lines: list[str] = []
                                lines.append(f"📈 圆桌开场前系统已自动扫榜成功｜平台：{rp.get('platform_label','番茄新书榜')}")
                                if rp.get('scan_time'):  lines.append(f"· 扫榜时间：{rp['scan_time']}")
                                if rp.get('subcategory_label'): lines.append(f"· 命中赛道：{rp['subcategory_label']}")
                                if rp.get('books') and isinstance(rp['books'], list):
                                    tops = rp['books'][:5]
                                    lines.append(f"· TOP{len(tops)} 同类题材上榜书（书名+一句话钩子+作者）：")
                                    for i, b in enumerate(tops, 1):
                                        parts = []
                                        if b.get('title'): parts.append(str(b['title']))
                                        if b.get('hook_1line'): parts.append(str(b['hook_1line']))
                                        if b.get('author'): parts.append(f"作者：{b['author']}")
                                        lines.append(f"  {i}. " + " ｜ ".join(parts) if parts else f"  {i}. {b}")
                                for key, zh in [('reader_buy_points', '读者买单要素·共性卖点'),
                                                ('reader_abandon_points', '读者弃文毒点·共性避坑'),
                                                ('title_formula_examples', '书名公式范例'),
                                                ('opening_hook_templates', '开篇钩子套路模板'),
                                                ('market_advice', '市场落地方向建议')]:
                                    v = rp.get(key)
                                    if isinstance(v, str) and v.strip():
                                        lines.append(f"\n【{zh}】\n{v.strip()}")
                                    elif isinstance(v, list) and v:
                                        lines.append(f"\n【{zh}】")
                                        for it in v:
                                            lines.append(f"- {it}")
                                return "\n".join(lines).strip()
                            _rank_analyst_report = _rt_report_md2(_rank_scan)
                            # 扫榜完成给用户一帧提示（可选）
                            # 提变量，避免嵌套 f-string + json.dumps 里 \" 导致 SyntaxError（Python 不允许 f-string {} 内有反斜杠）
                            _plat2 = _rank_scan.get('platform_label', '番茄新书榜') if isinstance(_rank_scan, dict) else '番茄新书榜'
                            _nb2 = len((_rank_scan.get('books') or []) if isinstance(_rank_scan, dict) else [])
                            _sse_meta_obj2 = {
                                'type': 'meta',
                                'kind': 'roundtable_status',
                                'info': {
                                    'text': f'✅ 扫榜完成：{_plat2}｜命中 {_nb2} 本TOP书，榜单分析师第一个发言会展示。'
                                }
                            }
                            yield f'data: {json.dumps(_sse_meta_obj2, ensure_ascii=False)}\n\n'
                    except Exception:
                        # 扫榜失败 = 当没发生，继续让主持人+后续7位专家正常讨论
                        pass
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0]}}, ensure_ascii=False)}\n\n'
                mod_system = _MODERATOR_ROLE[1] + f"""

【讨论议题】
{topic}

【规则】
- 你现在开场，简短说明讨论规则：7位专家分两轮依次发言，第一位「榜单分析师」会先给大家分享真实榜单风向（番茄/起点新书榜），随后每人就议题讲出自己的专业见解
- 鼓励交锋，允许不同意前面发言人的观点，必须碰撞出真实结论
- 开场只要说3-5句话点明规则和议题，不用展开，把场子交给专家就行
"""
                if book_id and base_system:
                    mod_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。讨论请以落地资料为准。\n\n" + mod_system
                mod_system = mod_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    mod_system = mod_system.rstrip() + '\n\n' + _rank_ctx_global
                mod_messages = [{'role': 'system', 'content': _var_replace(mod_system)}]
                mod_messages.append({'role': 'user', 'content': f'请开始开场，议题是：{topic}'})

                gw_mod = LLMGateway(_bg, _kg, _mg)
                full_parts = []
                for f in _emit(_rt_stream_turn(gw_mod, mod_messages, 0.6, _DIM_MAX_TOKENS), 'moderator'):
                    yield f
                mod_content = ''.join(full_parts)
                all_messages.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{mod_content}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator"}, ensure_ascii=False)}\n\n'

                topic_final = topic
                done = []
                discussion_history = f'【原始议题】\n{topic_final}\n\n【主持人开场】\n{mod_content}\n\n'
                # 开场完成即落一次进度 → 之后任何一步断掉都能续会
                # total_rounds_hint = 用户明确指定的轮数（_round_req），否则按默认 2；resuming/append/adjust 都读这个字段
                state = {'active': True, 'completed': False, 'phase': 'discussion',
                         'topic': topic_final, 'moderator_open': mod_content,
                         'done': [], 'discussion_history': discussion_history,
                         'total_rounds_hint': (_round_req if _round_req else default_rounds)}
                _rt_save_state(session, db, state)
                # 开场即落盘消息 → 刷新界面能看到开场
                _rt_persist_messages(session, history, topic_final, mod_content, [], '')

            # ========== 讨论：从断点/开头继续，按 target_total 轮×N位依次发言 ==========
            done_count = len(done)
            gw_sp0 = LLMGateway(_bg, _kg, _mg)
            # 计算 target_total 的统一公式（新会议/resuming/append/adjust 都复用）：
            #   · state.total_rounds_hint = 作者指定的总轮数 或 默认2轮
            #   · 全新会议 = (_round_req or default_rounds)，并且写入 state
            #   · resuming = 读 state.total_rounds_hint，不写死 2
            #   · adjust_mode 阶段 = 开一轮（N位专家逐一回应反馈），target_total = len(done) + N
            #   · append_mode 追加一轮 = len(done) + N（保持原有逻辑）
            if adjust_mode:
                target_total = len(done) + N
            else:
                # 解析应开的总轮数（按用户指定或state中保存的）
                _rounds_hint_cur = state.get('total_rounds_hint') if isinstance(state, dict) and state else None
                if _rounds_hint_cur is None:
                    _rounds_hint_cur = (_round_req if _round_req else default_rounds)
                try:
                    _rounds_hint_cur = max(1, min(99, int(_rounds_hint_cur)))
                except Exception:
                    _rounds_hint_cur = default_rounds
                target_total = _rounds_hint_cur * N
            # 如果用户进入 append_mode 时显式说"继续"但实际还差很远就追加到满轮 → 保持 append_mode 原语义"追加一轮"：
            if append_mode:
                target_total = len(done) + N
            # 把 total_rounds_hint 写入 state（供 resuming/下一次 append 使用）
            if isinstance(state, dict) and not state.get('total_rounds_hint') and not adjust_mode and not append_mode:
                state['total_rounds_hint'] = _rounds_hint_cur
                _rt_save_state(session, db, state)
            while done_count < target_total:
                seq_abs = done_count
                round_num = 1 + seq_abs // len(_ROUNDTABLE_ORDER)
                speaker_id = _ROUNDTABLE_ORDER[seq_abs % len(_ROUNDTABLE_ORDER)]
                sp_name, sp_system_prompt = _PERSONAS[speaker_id]
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": speaker_id, "speaker_name": sp_name, "round": round_num}}, ensure_ascii=False)}\n\n'

                sp_system = sp_system_prompt + f"""

【当前议题】
{topic_final}

【规则】
现在是圆桌会议第{round_num}轮讨论，你是{sp_name}，请严格按你的专业身份发言。
- {f'前面已有多位专家的发言，你必须先回应他们的观点，可以用不同意直接碰撞' if not (round_num == 1 and speaker_id == _ROUNDTABLE_ORDER[0]) else '第一轮开场，你第一个从你的专业视角破题'}
- {f'这是第{round_num}轮，请收敛：抓住前面几轮别人没讲透的坑，或直接反驳前面错误的结论，给出你这一轮的主张' if round_num > 1 else '这是第一轮，请从你的专业视角给出清晰的第一轮见解'}
- 不总结所有人，只说你自己的专业观点
- 字数控制在300-800字，观点鲜明、可直接落地，拒绝空话套话
"""
                # ==============================================
                # 【榜单分析师·专属增强】首位专家：把扫榜完整报告塞进他的 system prompt，
                # 让他第一轮第一个发言就给全桌摊开「真实榜单风向」（TOP书/卖点/毒点/书名公式/钩子）
                # ==============================================
                if speaker_id == 'rank_analyst' and _rank_analyst_report.strip():
                    sp_system += (
                        "\n\n================================\n"
                        "【★★★ 圆桌前置·系统已自动扫榜成功（你是第一个发言者，必须先把这份情报展示给全桌）★★★】\n"
                        "下面这份报告是刚从番茄/起点新书榜**真实抓下来的 TOP 书数据 + LLM 情报聚合**：\n\n"
                        + _rank_analyst_report.strip() +
                        "\n================================\n"
                        "【你的第一轮发言要求（只在第一轮且你第一个说话时执行）】：\n"
                        "1) 开场先给一张「📈 扫榜情报摘要」：平台+赛道+扫榜时间、TOP3 一句话钩子、共性卖点、共性毒点\n"
                        "2) 然后给出「🎯 市场落地方向」：基于榜单，对本次议题具体建议怎么切赛道、怎么取名、前3章钩子怎么埋\n"
                        "3) 最后给后续专家一个「📢 给全桌的定调」：明确告诉毒舌读者/架构师/世界观策划/爆款编辑/润色编辑/采访——他们讨论时应该优先吸收风向的哪些点、避开哪些坑\n"
                        "4) 不拍脑袋，每一条建议必须标注'参考榜上书XXX的套路'/'避开榜上书XXX的毒点'\n"
                    )
                if book_id and base_system:
                    sp_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。讨论请以落地资料为准。\n\n" + sp_system
                sp_system = sp_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    sp_system = sp_system.rstrip() + '\n\n' + _rank_ctx_global

                sp_messages = [{'role': 'system', 'content': _var_replace(sp_system)}]
                sp_messages.append({'role': 'user', 'content': (discussion_history + f"\n【轮次】第{round_num}轮 → 轮到【{sp_name}】发言，请开始：\n")[:12000]})

                full_parts = []
                for f in _emit(_rt_stream_turn(gw_sp0, sp_messages, 0.7, _DIM_MAX_TOKENS), speaker_id):
                    yield f
                sp_content = ''.join(full_parts)

                done = done + [{'round': round_num, 'speaker': speaker_id, 'name': sp_name, 'content': sp_content}]
                discussion_history += f"\n【第{round_num}轮 · {sp_name}】\n{sp_content}\n\n"
                all_messages.append({'role': 'assistant', 'content': f'【{sp_name}】\n{sp_content}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": speaker_id, "round": round_num}, ensure_ascii=False)}\n\n'

                # 每完成一人落一次进度 → 断连后说"继续"即可从下一位精准接上
                _mod_open = state.get('moderator_open', '') if isinstance(state, dict) else ''
                state = {'active': True, 'completed': False, 'phase': 'discussion',
                         'topic': topic_final, 'moderator_open': _mod_open,
                         'done': done, 'discussion_history': discussion_history}
                _rt_save_state(session, db, state)
                # 同时把已讨论内容落盘到会话 → 中途断连/手动停止后刷新也能看到
                _rt_persist_messages(session, history, topic_final, _mod_open, done, '')
                done_count += 1

            # ========== 总结报告 ==========
            yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator_summary", "speaker_name": "主持人·总结报告"}}, ensure_ascii=False)}\n\n'
            # 从议题关键词识别命中维度（构思/设定/世界观/大纲/人物/剧情/伏笔/文风等）
            _hit_dims = _detect_dim_from_text(topic_final)
            _hit_card_types = []
            _hit_labels = []
            for _k, _kw in _hit_dims:
                _ct = _DIM_KEY_CARD.get(_k)
                if _ct and _ct in CARD_REGISTRY:
                    _hit_card_types.append(_ct)
                    _hit_labels.append(CARD_REGISTRY[_ct]['label'])
            _sum_dim_note = ('；'.join(_hit_labels) if _hit_labels else '暂无明确命中')
            _sum_dim_types = ('/'.join(_hit_card_types) if _hit_card_types else 'SAVE_CONCEPT/SAVE_RULE/SAVE_WORLDSETTING/SAVE_OUTLINE_NODE/SAVE_PLOT/SAVE_CHARACTER/SAVE_FORESHADOW/SAVE_LOCATION/APPLY_STYLE')

            sum_system = _MODERATOR_ROLE[1] + f"""

【讨论议题】
{topic_final}

【完整讨论记录】
{discussion_history[:12000]}

【总结要求】
{f'''把讨论收束成一份清晰的总结报告（本次是【调整阶段】，请结合作者意见『{feedback[:120]}』，在上一版结论基础上说明：哪些做了修正、哪些维持、最终结论是否变化；「落地采纳建议」只针对调整后仍要采纳的维度）。结构必须是：

# 圆桌会议调整结论：{topic_final[:40]}''' if adjust_mode else f'''把讨论收束成一份清晰的总结报告，结构必须是：

# 圆桌会议总结：{topic_final[:40]}'''}

## 核心共识
列出大家都同意的结论，每条一句话

## 主要分歧
列出不同专家观点不一致的地方，点出各方理由

## 优化建议（按优先级排序）
1. 最优先改什么（必须具体可落地）
2. 次优先改什么
3. ...

## 落地采纳建议（是否采纳到各维度）
结合讨论结论，逐条给出本话题若落地应"要不要采纳"进哪些创作维度，每条格式：
- 维度：人物 / 大纲 / 世界观 / 剧情 / 伏笔 / 文风 / 设定 / 构思 / 地图……
- 结论：一两句说清该维度应做什么调整或新增
- 是否采纳：直接写"建议采纳"/"有条件采纳"/"暂不采纳"，并一句话说明理由

## 最终结论
一句话给作者拍板：这个点子能不能打，核心优势在哪，最大短板在哪

严格按这个结构输出，用markdown标题分级，结论要明确，别模棱两可。

【落地采纳建议的书写要求】
"落地采纳建议"小节除上述格式外，每条必须写"是否采纳"（建议采纳/有条件采纳/暂不采纳）；本小节用纯中文 markdown 输出，不要出现 [[CARD:...]] 这类标记，卡片另由系统整理。
本次议题已识别相关维度：{_sum_dim_note}
"""

            if book_id and base_system:
                sum_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。总结请以落地资料为准。\n\n" + sum_system
            sum_system = sum_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
            if _rank_ctx_global:
                sum_system = sum_system.rstrip() + '\n\n' + _rank_ctx_global
            sum_messages = [{'role': 'system', 'content': _var_replace(sum_system)}]
            sum_messages.append({'role': 'user', 'content': '请输出总结报告'})

            gw_sum = LLMGateway(_bg, _kg, _mg)
            full_parts = []
            for f in _emit(_rt_stream_turn(gw_sum, sum_messages, 0.5, _DIM_MAX_TOKENS), 'moderator_summary'):
                yield f
            sum_content = ''.join(full_parts)
            all_messages.append({'role': 'assistant', 'content': f'【总结报告】\n{sum_content}'})
            yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator_summary"}, ensure_ascii=False)}\n\n'

            # ========== 单独整理"落地采纳建议卡片"（不进讨论气泡，作为可采纳的 ActionCard 下发） ==========
            sum_cards = []
            try:
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "正在整理可落地的采纳建议…"}}, ensure_ascii=False)}\n\n'
                _card_sys = _MODERATOR_ROLE[1] + f"""

【任务】根据下面的圆桌会议总结，把"落地采纳建议"小节里判定为「建议采纳/有条件采纳」的维度整理成可落地的 Action Card。

【输出格式】只输出卡片标记，禁止一切解释/前言/后记，一个维度一张，格式严格如下：
[[CARD:卡片类型|标题|具体内容]]

【卡片类型对照】SAVE_CONCEPT=构思, SAVE_RULE=设定, SAVE_WORLDSETTING=世界观, SAVE_OUTLINE_NODE=大纲, SAVE_PLOT=剧情, SAVE_CHARACTER=人物, SAVE_FORESHADOW=伏笔, SAVE_LOCATION=地图, APPLY_STYLE=文风
【内容要求】卡片内容必须具体、可直接写入对应维度（如"采纳到人物"就写清楚姓名/性格/动机等）；拿不准的维度宁可不产，少于一行不要产。
"""
                _card_msg = [{'role': 'system', 'content': _var_replace(_card_sys)},
                             {'role': 'user', 'content': sum_content[:8000]}]
                _card_full = []
                for _tk, _tp in _rt_stream_turn(gw_sum, _card_msg, 0.3, 2048):
                    if _tk == 'body':
                        _card_full.append(_tp)
                _card_txt = ''.join(_card_full) or ''
                sum_cards = parse_cards(_card_txt)
            except Exception:
                sum_cards = []
            # 下发卡片；顺带清理总结正文里可能残留的卡片标记
            for _card in sum_cards:
                _enrich_card_rank_meta(_card, _rank_scan)
                yield f'data: {json.dumps({"type": "card", "card": _card, "session_id": session_id}, ensure_ascii=False)}\n\n'
            sum_content = strip_cards(sum_content or '').strip()
            all_messages[-1]['content'] = f'【总结报告】\n{sum_content}'

            # 落盘 + 标记会议完成（保留全量进度，供其后再"继续/追加一轮"）
            state['completed'] = True
            state['phase'] = 'done'
            _rt_save_state(session, db, state)

            # 把整场（含追加轮）的可复盘消息落盘 → 刷新界面不丢
            _mod_open = state.get('moderator_open', '') if isinstance(state, dict) else ''
            # 落盘的卡片也同步 enrich，保证后续复盘/续会仍保留风向来源
            for _c in sum_cards:
                _enrich_card_rank_meta(_c, _rank_scan)
            _rt_persist_messages(session, history, topic_final, _mod_open, done, sum_content, summary_cards=sum_cards)

            full_discussion = [
                {'speaker': m.get('content', '').split('】')[0].split('【')[-1] if m.get('role') == 'assistant' else '', 'content': m.get('content', '')}
                for m in all_messages
            ]
            yield f'data: {json.dumps({"type": "done", "session_id": session_id, "summary": sum_content}, ensure_ascii=False)}\n\n'

        except Exception as e:
            import traceback
            traceback.print_exc()
            # ======= 圆桌会议：异常退出（断连/超时/模型错误）也要存 state，支持"继续"断点续会 =======
            # 对齐节点设计师 L9778-L9790 异常存进度逻辑：
            # 把已经完整生成完毕并 append 到 done 的发言、discussion_history 全部存进 meta_json，
            # 防止"开到一半崩溃 → 用户说继续 → state 丢了 → 当成新会议/追加一轮"
            try:
                if session:
                    _st_save = dict(state) if isinstance(state, dict) else {}
                    # 异常前做一些兜底：把 discussion_history / done 字段都写全，缺的就用局部变量
                    if 'done' not in _st_save or not isinstance(_st_save.get('done'), list):
                        try: _st_save['done'] = list(locals().get('done') or [])
                        except Exception: _st_save['done'] = []
                    if 'discussion_history' not in _st_save or not isinstance(_st_save.get('discussion_history'), str):
                        try: _st_save['discussion_history'] = str(locals().get('discussion_history') or '')
                        except Exception: _st_save['discussion_history'] = ''
                    if 'topic' not in _st_save:
                        try: _st_save['topic'] = str(locals().get('topic_final') or topic or '')
                        except Exception: _st_save['topic'] = ''
                    if 'moderator_open' not in _st_save:
                        try: _st_save['moderator_open'] = str(locals().get('state', {}).get('moderator_open', '') if isinstance(state, dict) else '')
                        except Exception: pass
                    if 'active' not in _st_save: _st_save['active'] = True
                    if 'completed' not in _st_save: _st_save['completed'] = False
                    if 'phase' not in _st_save: _st_save['phase'] = 'interrupted'
                    _st_save['updated_at'] = datetime.now(timezone.utc).isoformat() if 'timezone' in dir() else __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                    _rt_save_state(session, db, _st_save)
                    # 同时落盘一次历史（刷新就能看到已完成的发言）
                    try:
                        _mod_for_save = _st_save.get('moderator_open', '') if isinstance(_st_save, dict) else ''
                        _topic_for_save = _st_save.get('topic', '') if isinstance(_st_save, dict) else ''
                        _done_for_save = _st_save.get('done', []) if isinstance(_st_save, dict) else []
                        _rt_persist_messages(session, history, _topic_for_save, _mod_for_save, _done_for_save, '')
                    except Exception:
                        pass
            except Exception:
                pass
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'
        finally:
            # ==============================================
            # ⭐【断点续会·终极兜底：finally 存 state】
            # ==============================================
            # 覆盖【用户点"停止"/刷新页面/关闭浏览器】场景：SSE 连接断 = Python 抛 GeneratorExit
            # GeneratorExit 是 BaseException 子类 ❗NOT Exception❗，所以上面的 except Exception 抓不到，
            # 之前就是这里漏了 → state 没存 → 下次点继续 state=None → 全新会议从主持人+榜单分析师重开场。
            # finally 在正常完成 / Exception / GeneratorExit（任何 BaseException）三条路径上都会执行，
            # 是目前 Python 中唯一能保证 SSE 断连场景下一定落地 state 的方案。
            try:
                if session is None or 'db' not in dir() or db is None:
                    pass  # 最早的初始化阶段报错（如 AIConfig 缺失）还没拿到 session/db → 跳过
                else:
                    # 读取所有运行时变量：优先 locals() 里的当前值，其次 state 字典，最后兜底空值
                    _fin_state = dict(state) if isinstance(state, dict) else {}
                    # —— done：已完整发言并 append 到 local.done 的专家记录，是续会最核心的数据 ——
                    try:
                        _local_done = locals().get('done')
                        if isinstance(_local_done, list) and _local_done:
                            _fin_state['done'] = list(_local_done)
                        elif 'done' not in _fin_state or not isinstance(_fin_state['done'], list):
                            _fin_state['done'] = []
                    except Exception:
                        if 'done' not in _fin_state: _fin_state['done'] = []
                    # —— discussion_history：上下文拼接给下一位专家作为前置摘要
                    try:
                        _local_hist = locals().get('discussion_history')
                        if isinstance(_local_hist, str) and _local_hist:
                            _fin_state['discussion_history'] = _local_hist
                    except Exception:
                        pass
                    # —— topic / moderator_open / rounds / book-level 元数据
                    try:
                        _local_topic = locals().get('topic_final') or _fin_state.get('topic') or topic or ''
                        if _local_topic: _fin_state.setdefault('topic', str(_local_topic))
                    except Exception:
                        pass
                    try:
                        if 'moderator_open' not in _fin_state or not _fin_state.get('moderator_open'):
                            try: _fin_state['moderator_open'] = str(locals().get('mod_content') or _fin_state.get('moderator_open') or '')
                            except Exception: pass
                    except Exception:
                        pass
                    try:
                        if not _fin_state.get('total_rounds_hint'):
                            try:
                                _rh = locals().get('_rounds_hint_cur') or _fin_state.get('total_rounds_hint') or 2
                                _fin_state['total_rounds_hint'] = max(1, min(99, int(_rh)))
                            except Exception:
                                _fin_state['total_rounds_hint'] = 2
                    except Exception:
                        _fin_state['total_rounds_hint'] = 2
                    # —— 标记位（除非真的完成了 completed=True，否则任何退出都视为可续会的中断）
                    if not _fin_state.get('completed'):
                        _fin_state['active'] = True
                        _fin_state['completed'] = False
                    if not _fin_state.get('phase'):
                        _fin_state['phase'] = 'interrupted' if not _fin_state.get('completed') else 'done'
                    # —— DB 持久化保存
                    _rt_save_state(session, db, _fin_state)
                    # —— 同时落盘历史消息（刷新界面即可见，用户看到确实保留了已发言内容）
                    try:
                        _h = history if 'history' in locals() else []
                        _h = _h if isinstance(_h, list) else []
                        _tp = str(_fin_state.get('topic') or topic or '')
                        _mo = str(_fin_state.get('moderator_open') or '')
                        _dn = list(_fin_state.get('done') or [])
                        _rt_persist_messages(session, _h, _tp, _mo, _dn, '')
                    except Exception:
                        pass
            except Exception:
                # finally 内任何存盘错误 = 静默吞，绝不能污染 SSE 流或造成 Python 生成器异常
                pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})
