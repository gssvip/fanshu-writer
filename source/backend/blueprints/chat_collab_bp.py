"""聊天驱动创作 Blueprint：维度感知多轮对话 + Action Card 落地 + 进度引导。

把"表单填空"创作模式升级为"边聊边写"：
  - 聊天时自动注入当前书的相关 bible 维度（AI 真懂你的书）
  - AI 回复中可产出结构化「落地卡片」，用户点确认即写入对应维度
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
import re
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, Response, stream_with_context

chat_collab_bp = Blueprint('chat_collab', __name__)

# 滑窗上下文：保留最近 N 轮 + 系统提示，超出则保留首尾、中间摘要
MAX_HISTORY_ROUNDS = 8
# 单条消息最大字符（超长截断，防 token 爆炸）
MAX_MSG_CHARS = 2000

# ============================================================================
# 统一去AI味规则（融合「默认去AI味规则」+「去AI味执行规则精简版」）
# 适用：正文写作（continue）、正文修改（polish）、去AI Tab（smart_deai）
# 三处共用同一份规则，保证口径一致
# ============================================================================
DEAI_RULES = """【去AI味执行规则】
定位：去AI味操作手册。写作时主动规避，去AI时只做减法/替换，不做润色扩写。

【一、总则】
1. 去AI味 ≠ 润色。不改原意、不增剧情、不换风格。
2. 优先级：删 > 换 > 改写。能删半句解决的不重写整段。
3. 保留：事件顺序、因果关系、人物动机、设定信息、伏笔、作者已有语气、方言口癖、身份称呼、角色特有表达、清单步骤等整齐结构文本。
4. 禁止：不新增剧情/设定/观点/情绪/金句；不把所有句子改短句或写病句；不堆砌"嗯啊吧嘛"假装口语化；不把解释腔改文艺腔/抒情腔；不输出修改说明、自检清单、协作口吻。

【二、三大AI味识别与处理】
1. 解释腔（第一大AI味）
   - 识别关键词：这说明、这意味着、由此可见、换句话说、事实上、显然、本质上、归根结底、从某种意义上说、不得不说、毋庸置疑
   - 识别模式：动作后立刻旁白解释"他之所以这样，是因为…"；动作/表情已表情绪还要写"这让他感到…"；段尾升华成长/命运/人性/选择；同一因果讲两遍
   - 处理：①删无信息量解释句（换说法的=删）②必要因果压成短因果一句说完 ③交还行为：用动作/停顿/视线/语气/选择替代旁白 ④段尾总结改成具体后果/反应/环境变化
2. 对白AI味（第二大AI味）
   - 识别模式：角色像作者在解释剧情而非回应眼前人；自报动机/创伤/选择完整分析；对白硬塞世界观/规则/背景像说明书；冲突场景语气仍客气逻辑过顺；不同角色同句式同情绪；对白出现书面连接词"因此/然而/与此同时/换句话说"
   - 处理：①保留必须剧情事实、关系变化 ②删角色不该明说的心理分析、主题总结、背景说明 ③调语气符合角色身份与当下情绪（不强行现代口语化）④制造缺口：用半句话/停顿/反问/打断替代完整解释 ⑤长对白拆短，中间用动作/停顿/对方插话承接
   - 约束：角色越紧张/越隐瞒/越愤怒，越不该把话说完整漂亮；重要信息可留，但要像角色当下说出来的，不像作者塞的
3. 工整句式（第三大AI味）
   - 识别模式：连续多段长度接近像自动分块；连续句相同主谓结构/转折/因果；排比三连对仗过密；段尾总总结点题升华；路标词密集"然而/同时/此外/更重要的是/总而言之"；每段都"观点句+展开解释+段尾总结"模板
   - 处理：①保顺序：事件/论证顺序不能乱 ②破模板：连续三段形状相似时至少调一段开头/长度/收束 ③删路标：能不用连接词直接删，用动作/后果/场景变化承接 ④调长短：短段中段厚段按内容需要交替，不为整齐均分 ⑤弱收束：段尾停在细节/动作/未说完余波上

【三、词表速查】
- 必删词：一股、一抹、不由得、不禁、随即、旋即、仿佛、似乎、似乎在、缓缓、微微、淡淡、轻轻、静静地、默默地、不知不觉、若有所思、若有所悟
- 必删句式：不是……是……、不是……而是……、与其说……不如说……（"不是A是B"句式一律改写成自然陈述或删除）
- 必删路标词：然而、同时、此外、更重要的是、总而言之、换句话说
- 必删协作口吻：下面我们、希望这能帮助、作为AI
- 结尾禁用：总结性、升华性、点题性语句（成长、命运、人性、选择、未来、从此、那一刻等拔高词）

【四、执行流程（写完/改完后逐条过一遍）】
1. 扫关键词 → 删"这说明/这意味着/由此可见/换句话说/事实上/显然/本质上/归根结底/从某种意义上说" + 必删词
2. 扫"不是A是B/不是A而是B"句式 → 一律改写成自然陈述或删除
3. 扫段尾 → 段尾若是总结/升华/拔高，改成具体动作/画面/后果，或直接删
4. 扫结尾 → 章节结尾禁用总结性、升华性、点题性语句，停在动态动作或悬念上
5. 扫对白 → 角色替作者解释？心理/背景/设定完整说出？→ 拆、删、打断
6. 扫句式 → 连续三句同结构？连续三段同长度？排比三连？→ 打散其中一处
7. 扫路标 → 删不必要"然而/同时/此外/更重要的是/总而言之"
8. 自检读一遍 → 确认未新增内容、未换风格、只做减法

【五、网文创作硬约束（与人味注入一并执行）】
1. 极致模仿人的写作习惯，写得自然，没有AI味。以写事为主，景物一笔带过，非必要不用比喻/拟人等修辞。
2. 句子阅读感强、读起来顺畅，长短句自然交替，不刻意整齐。
3. 章节间剧情连贯、逻辑清晰，前后呼应不跳戏。
4. 环境描写不超过15%，重点刻画人物动作、微表情、矛盾心理；人物全员有瑕疵、会纠结、口是心非，不许完美人设，禁止OOC。
5. 结尾停在动态动作或悬念上，禁止抒情总结升华；对话有潜台词，不说大道理；所有出场人物有名有姓；章节无缝衔接不跳时间。
6. 没有纯反派，所有角色行为都有合理动机；爽点靠信息差和布局，不开上帝视角；感情线绑定主线、无工业糖精。
7. 绝对不能出现：顿悟式成长、大段景物抒情、人物语气同质化、行为逻辑割裂。
8. 人味注入：加入不完美细节（结巴/重复/打断）、感官碎片、口语化表达，删除冗余形容词。

【六、输出契约】
- 只输出处理后的正文，不解释改了什么、为什么改，不要在文末附加字数统计。
- 优先级铁律：人味 > 克制 > 流畅。""".strip()


# ============================================================================
# Action Card 协议
# ============================================================================

# 卡片类型 → 目标维度字段 + 落地方式（append 覆盖/追加, character 走独立表, chapter 走章节表）
CARD_REGISTRY = {
    'SAVE_WORLDSETTING': {'field': 'worldbuilding', 'mode': 'append', 'label': '世界观'},
    'SAVE_CHARACTER':    {'field': 'character_profiles', 'mode': 'character', 'label': '人物'},
    'SAVE_FORESHADOW':   {'field': 'foreshadowing', 'mode': 'append', 'label': '伏笔'},
    'SAVE_OUTLINE_NODE': {'field': 'plot_design', 'mode': 'append', 'label': '大纲'},
    'SAVE_PLOT':         {'field': 'timeline', 'mode': 'append', 'label': '剧情线'},
    'SAVE_LOCATION':     {'field': 'locations', 'mode': 'append', 'label': '地点'},
    'SAVE_RULE':         {'field': 'key_rules', 'mode': 'append', 'label': '核心规则'},
    'APPLY_STYLE':       {'field': 'style_guide', 'mode': 'append', 'label': '文风'},
    'SAVE_CONCEPT':      {'field': 'concept', 'mode': 'append', 'label': '核心构思'},
    'SAVE_CHAPTER':      {'field': 'chapter', 'mode': 'chapter', 'label': '章节正文'},
}

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


def build_chat_system_prompt(book, bb, recent_chapters: list = None, next_chapter_num: int = None) -> str:
    """构建维度感知的聊天 system_prompt。

    注入当前书的全部 bible 维度 + 最近章节 + Action Card 使用说明 + 创作进度。
    维度内容完整注入，不做截断（避免信息缺失导致错乱）。
    recent_chapters: 最近章节列表（dict: title/word_count/order_index），由 chat_smart 注入
    next_chapter_num: 下一章应使用的章节号（与写作/修改/去AI统一口径），用于约束 SAVE_CHAPTER 卡片标题
    """
    parts = [
        '你是一位资深网文创作副驾，正在和作者协作创作一部小说。你的职责：',
        '1. 像同行一样讨论创作问题（人物、剧情、世界观、文风）',
        '2. 当讨论中形成明确结论时，主动产出「落地卡片」让作者一键采纳',
        '3. 感知创作进度，主动引导下一步该做什么',
        '',
        f'【当前作品】《{book.title or "未命名"}》',
    ]

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
            f'产出 SAVE_CHAPTER 卡片时，新章节标题必须用「第{next_chapter_num}章」开头'
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

    # Action Card 使用说明
    parts.append(_CARD_INSTRUCTIONS)

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

def load_session_messages(session) -> list[dict]:
    """从 AISession 加载消息（兼容旧 messages_json 和新 messages 关联）。"""
    msgs = []
    try:
        msgs = json.loads(session.messages_json or '[]')
    except Exception:
        msgs = []
    return msgs if isinstance(msgs, list) else []


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
# 解决：AI 输出"标题+空行+正文"被整体存入 card.content，导致
#   1) 章节正文里混入标题行
#   2) 字数统计 len(content) 含标题/空行，与 prompt 要求的"纯正文含标点"口径不一致
# 统一：card.content / chapter.content 只存纯正文；字数用 _count_cn_chars（去空白含标点）
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

    # 获取或创建会话
    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope=scope, title=message[:30], messages_json='[]')
        db.session.add(session)
        db.session.commit()
    session_id = session.id

    # 构建 system_prompt + 上下文
    system_prompt = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num)
    history = load_session_messages(session)
    messages = build_context_messages(system_prompt, history, message)

    # 获取 LLM 配置 + gateway
    cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    import app as app_module
    base_url, api_key, model = get_llm_config(app_module)
    gw = LLMGateway(base_url, api_key, model)

    def generate():
        full_text = []
        try:
            for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=4096):
                full_text.append(chunk)
                yield f'data: {json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False)}\n\n'

            # 解析卡片
            complete = ''.join(full_text)
            cards = parse_cards(complete)
            for card in cards:
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            # 持久化对话（剥离卡片标记后存历史，cards 单独存以便历史会话恢复）
            clean_text = strip_cards(complete)
            # 卡片持久化时标记为 pending，前端历史会话加载后可继续采纳
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending'} for c in cards]
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text,
                            'cards': persisted_cards})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()

            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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
            session.messages_json = json.dumps(msgs, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
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
    result_extra = {}

    # 章节正文卡：落地到 Chapter 表（覆盖同章节号/同标题，自动分卷排序）
    if spec['mode'] == 'chapter':
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

        db.session.commit()
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
# 支持的动作：
#   action=master_create  批量生成设定（调 ai-master-create 维度生成，每维度产一张卡）
#   action=continue       续写本章正文（调 ai-continue/stream，产 SAVE_CHAPTER 卡）
#   action=polish         润色本章正文（调 ai-continue/stream + 润色指令，产 SAVE_CHAPTER 卡）
#
# SSE 输出统一为副驾协议：
#   data: {"type":"delta","content":"..."}\n\n          流式正文
#   data: {"type":"card","card":{...},"session_id":"..."}\n\n  落地卡片
#   data: {"type":"done","session_id":"..."}\n\n
#   data: {"type":"error","error":"..."}\n\n

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

    body: { book_id, action, session_id?, instruction?, target_chapter_num?, prev_chapter_content? }
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

    if not book_id or action not in ('master_create', 'continue', 'polish'):
        return jsonify({'error': '参数无效，action 必须为 master_create/continue/polish'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404

    # 复用或创建会话（动作也走会话，便于历史回看）
    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        title_map = {'master_create': '批量生成设定', 'continue': '续写本章', 'polish': '润色本章'}
        session = AISession(book_id=book_id, scope='general', title=title_map.get(action, 'AI动作'),
                            messages_json='[]')
        db.session.add(session)
        db.session.commit()
    session_id = session.id

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
            if action == 'master_create':
                yield from _action_master_create(book, session, instruction, gw, sse)
            elif action == 'continue':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='continue',
                                           base_url=base_url, api_key=api_key, model=model)
            elif action == 'polish':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='polish',
                                           base_url=base_url, api_key=api_key, model=model)
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})
        finally:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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

        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》生成「{label}」设定。'
            f'题材：{book.genre or "未指定"}，类型：{book.book_type or "未指定"}。'
            f'\n已有设定参考：\n{ctx_block}'
            f'\n用户补充要求：{instruction or "无"}'
            f'\n请直接输出该维度的设定内容（300-600字），不要寒暄，不要解释。'
        )
        if existing:
            sys_prompt += f'\n\n已有内容（可在其基础上补充完善，不要简单重复）：\n{existing[:400]}'

        messages = [{'role': 'system', 'content': sys_prompt},
                    {'role': 'user', 'content': f'请生成{label}'}]
        content = ''
        try:
            for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=1500):
                content += chunk
                yield sse({'type': 'delta', 'content': chunk})
        except Exception as e:
            yield sse({'type': 'error', 'error': f'{label}生成失败：{e}'})
            continue

        generated[dim] = content
        # 产出落地卡片
        card = {
            'id': str(uuid.uuid4())[:8],
            'type': card_type,
            'title': f'{label}（AI生成）',
            'content': content.strip(),
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


def _filter_foreshadowing_for_chapter(foreshadow_raw, target_chapter_num):
    """伏笔维度过滤：注入伏笔但加防剧透指令。
    纯文本伏笔难以按章节精确过滤，采用"注入+强约束"策略：
    只允许AI呼应POV已察觉的线索，严禁揭示未到回收时机的谜底。
    """
    if not foreshadow_raw or not foreshadow_raw.strip():
        return ''
    return foreshadow_raw.strip()


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




def _action_chapter(book, session, instruction, gw, sse, target_chapter_num, prev_chapter_content, mode,
                    base_url=None, api_key=None, model=None):
    """续写/润色本章正文：产 SAVE_CHAPTER 卡。
    视点感知注入（第三人称有限视角）：只给AI看POV角色能知道的信息，减少token + 防剧透。
    - 人物：只注入POV + POV关系网 + 主角
    - 剧情：只注入当前卷及之前卷（当前卷内只注入已发生节点）
    - 动态报告：只注入target章之前的报告
    - 伏笔：注入但加强约束（严禁揭示未到回收时机的谜底）
    - 世界观/规则/文风/地点/构思：全量注入（写作基础，不剧透）
    """
    from app import db, BookBible, Chapter
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # 统一口径：从章节表提取最新章节号（写作/修改/去AI共用）
    ch_info = _get_latest_chapter_info(book_id)
    # 确定当前章号 + 上一章内容
    if not target_chapter_num:
        # 续写用「最新章节号+1」，润色用「最新章节号」
        target_chapter_num = ch_info['next_num'] if mode == 'continue' else ch_info['latest_num']
    if not prev_chapter_content:
        prev = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .filter(Chapter.order_index < target_chapter_num) \
            .order_by(Chapter.order_index.desc()).first()
        # 回退：若按 order_index 取不到，用统一口径的最新章节
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
        bb.foreshadowing if bb else '', target_chapter_num)

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

    # 组装上下文块
    ctx_blocks = []
    if static_ctx:
        ctx_blocks.append(static_ctx)
    if char_ctx:
        pov_note = f'（本章视点人物：{pov_name}，第三人称有限视角，只写{pov_name}能感知到的事物）' if pov_name else '（第三人称有限视角）'
        ctx_blocks.append(f'【人物档案·视点感知】{pov_note}\n{char_ctx}')
    if chapter_plot_ctx:
        ctx_blocks.append(f'【本章剧情·精确命中】\n{chapter_plot_ctx}\n（请严格按此情节节点推进本章剧情，不得跳过或偏移）')
    if timeline_ctx:
        trunc_note = '\n（注：后续卷剧情已省略，防剧透）' if timeline_truncated else ''
        ctx_blocks.append(f'【本卷及过往剧情脉络】{trunc_note}\n{timeline_ctx}')
    if foreshadow_ctx:
        ctx_blocks.append(f'【伏笔线索】\n{foreshadow_ctx}')
    if dynamic_reports_ctx:
        ctx_blocks.append(f'【近期动态文件（已发生事件摘要）】\n{dynamic_reports_ctx}')
    bible_ctx = '\n\n'.join(ctx_blocks) or '（暂无设定）'

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
        # 润色：按章节号定位原文（与 apply_card 覆盖口径一致）
        from app import parse_chapter_number
        cur = None
        candidates = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        for c in candidates:
            if parse_chapter_number(c.title or '') == target_chapter_num:
                cur = c
                break
        # 回退：按 order_index
        if not cur:
            cur = Chapter.query.filter_by(book_id=book_id, is_volume=False,
                                           order_index=target_chapter_num).first()
        if not cur or not (cur.content or '').strip():
            yield sse({'type': 'error', 'error': f'第 {target_chapter_num} 章无正文，无法润色'})
            return
        cur_len = len((cur.content or '').strip())
        sys_prompt = (
            f'你是资深网文润色编辑。请润色《{book.title}》第 {target_chapter_num} 章正文。'
            f'\n要求：保持剧情和人物不变，优化文笔节奏，提升画面感。'
            f'\n用户要求：{instruction or "无"}'
            f'\n\n【输出格式】第一行输出章节标题（如"第{target_chapter_num}章 标题"，标题前不要加 # 等 markdown 标记），第二行空行，第三行起输出纯正文。'
            f'\n【字数绝对铁律】纯正文（不含标题行）必须严格控制在 2400字±100（即 2300-2500 字区间）。'
            f'字数统计口径：中文字符+中文标点（全角标点如，。！？：；""均计入，半角标点如,.!?:;不计入，英文按单词、数字按串）。'
            f'请务必用全角中文标点写作。当前原文 {cur_len} 字：若不足 2300 字须扩写场景细节补足；若超过 2500 字须精简删减；'
            f'落在区间内则保持篇幅不变。这是不可违反的硬约束。'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【原文】\n{cur.content}'
            f'{anti_spoiler_rule}'
            f'\n\n{DEAI_RULES}'
            f'\n\n请直接输出（标题+空行+正文），不要解释，不要在文末附加字数统计。'
        )
        user_msg = f'请润色第 {target_chapter_num} 章'
    else:
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》续写第 {target_chapter_num} 章正文。'
            f'\n题材：{book.genre or "未指定"}，类型：{book.book_type or "未指定"}。'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【上一章结尾】\n{prev_chapter_content or "（第一章）"}'
            f'\n用户要求：{instruction or "自然推进剧情"}'
            + ('\n\n【写作铁律】上方【本章剧情·精确命中】已给出本章应写的情节节点，必须严格按该节点推进剧情，不得跳过节点、不得偏移到其他节点的剧情。' if chapter_plot_ctx else '')
            + f'\n\n【输出格式】第一行输出章节标题（如"第{target_chapter_num}章 标题"，标题前不要加 # 等 markdown 标记），第二行空行，第三行起输出纯正文。'
            f'\n【字数绝对铁律】纯正文（不含标题行）必须严格控制在 2400字±100（即 2300-2500 字区间）。'
            f'字数统计口径：中文字符+中文标点（全角标点如，。！？：；""均计入，半角标点如,.!?:;不计入，英文按单词、数字按串）。'
            f'请务必用全角中文标点写作。低于 2300 字=内容不足，须扩展场景细节、对话和心理描写补足；'
            f'超过 2500 字=冗余，须精简枝节删减。这是不可违反的硬约束，优先级高于所有其他要求。'
            f'{anti_spoiler_rule}'
            f'\n\n{DEAI_RULES}'
            f'\n\n请直接输出（标题+空行+正文），不要解释，不要在文末附加字数统计。'
        )
        user_msg = f'请续写第 {target_chapter_num} 章'

    messages = [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': user_msg}]
    content = ''
    try:
        for chunk in gw.chat_stream(messages, temperature=0.85, max_tokens=4096):
            content += chunk
            yield sse({'type': 'delta', 'content': chunk})
    except Exception as e:
        yield sse({'type': 'error', 'error': f'{mode_label}失败：{e}'})
        return

    # 剥离标题行：card.content 只存纯正文，card.title 用 AI 生成的章节名（去 # 标记）
    extracted_title, body_content = _strip_chapter_title(
        content, fallback_title=f'第{target_chapter_num}章')

    # 【字数铁律】用 count_words 校验纯正文字数（与章节保存/列表显示口径一致）
    # AI 自数字数往往偏高（把半角标点/空白也算进去），实际 count_words 常偏低约 200 字
    # 不在 2300-2500 区间则调 _ensure_word_count 重写补正
    from app import count_words, _ensure_word_count
    draft_wc = count_words(body_content)
    if (draft_wc < 2300 or draft_wc > 2500) and api_key and base_url and model:
        yield sse({'type': 'delta', 'content': f'\n\n[字数校验] 初稿 {draft_wc} 字，正在修正至 2400±100…'})
        corrected, wc_note = _ensure_word_count(
            body_content, api_key=api_key, base_url=base_url,
            model=model, max_tokens=4096, chapter_num=target_chapter_num,
            count_fn=count_words)
        if corrected and corrected.strip() and count_words(corrected) != draft_wc:
            # 修正后字数更接近目标，采用修正版（再剥一次标题防御）
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
    session.messages_json = json.dumps(history, ensure_ascii=False)
    session.updated_at = datetime.now(timezone.utc)
    if not session.title or session.title == 'AI动作':
        session.title = title[:30]
    db.session.commit()
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
# AI 智驾：四Tab（设定/正文/去AI/校审）统一接口
# 整合原 AI副驾 + AI总创作 + 章节AI创作 能力，统一入口
# ============================================================================

# 维度定义：用户可见的9个维度子按钮（设定Tab下）
SMART_DIMENSIONS = [
    {'key': 'concept',            'label': '构思',       'field': 'concept',            'card': 'SAVE_CONCEPT',      'icon': '💡', 'hint': '一句话讲清故事核：主角是谁、要什么、最大的阻碍'},
    {'key': 'key_rules',          'label': '设定',       'field': 'key_rules',          'card': 'SAVE_RULE',         'icon': '⚙️', 'hint': '能力体系/修炼体系/科技树，硬规则'},
    {'key': 'worldbuilding',      'label': '世界观',     'field': 'worldbuilding',      'card': 'SAVE_WORLDSETTING', 'icon': '🌍', 'hint': '故事发生的世界，独特规则或设定（生成中会提取世界地图架构到「地图」维度）'},
    {'key': 'plot_design',        'label': '大纲',       'field': 'plot_design',        'card': 'SAVE_OUTLINE_NODE', 'icon': '📋', 'hint': '主线走向，三幕式或起承转合'},
    {'key': 'timeline',           'label': '剧情',       'field': 'timeline',           'card': 'SAVE_PLOT',         'icon': '📖', 'hint': '关键剧情节点的时间顺序'},
    {'key': 'character_profiles', 'label': '人物',       'field': 'character_profiles', 'card': 'SAVE_CHARACTER',    'icon': '👤', 'hint': '主角和核心配角的动机、性格、关系网'},
    {'key': 'foreshadowing',      'label': '伏笔',       'field': 'foreshadowing',      'card': 'SAVE_FORESHADOW',   'icon': '🔮', 'hint': '长线伏笔的埋设与回收计划'},
    {'key': 'locations',          'label': '地图',       'field': 'locations',          'card': 'SAVE_LOCATION',     'icon': '🗺️', 'hint': '故事中的地点、势力分布、世界地图架构'},
    {'key': 'style_guide',        'label': '文风',       'field': 'style_guide',        'card': 'APPLY_STYLE',       'icon': '🎨', 'hint': '叙事风格、语言调性、节奏把控'},
]

# 通用聊天：不属于任何维度，自由讨论小说/剧情分析，通过触发关键词填入各维度
SMART_GENERAL_KEY = 'general'

_DIM_KEY_TO_SPEC = {d['key']: d for d in SMART_DIMENSIONS}


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


def _character_profiles_to_text(json_str):
    """把 character_profiles JSON 数组转为自然语言文本，避免 AI 模仿 JSON 格式输出。
    输入：[{"name":"姜辰","identity":"...","personality":"..."}, ...]
    输出：
      姓名：姜辰
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

    body: { book_id, message, history?, skill_pack_ids?, session_id? }
    返回 SSE：delta / card / done / error
    """
    from app import db, AISession, Book, BookBible
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

    # 构建上下文：所有维度完整注入（不截断，避免信息缺失导致错乱）
    ctx_parts = []
    if bb:
        for d in SMART_DIMENSIONS:
            v = (getattr(bb, d['field'], '') or '').strip()
            if v:
                # 人物维度：character_profiles 是 JSON 数组，注入前转自然语言，避免 AI 模仿 JSON
                if d['key'] == 'character_profiles' and v.startswith('['):
                    v = _character_profiles_to_text(v)
                ctx_parts.append(f'【{d["label"]}】\n{v}')
    ctx = '\n\n'.join(ctx_parts) if ctx_parts else '（暂无设定）'

    # 检测关键词，决定是否产卡片
    detected = _detect_dim_from_text(message)

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='agent') or ''
    except Exception:
        pass

    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            title='通用聊天', messages_json='[]')
        db.session.add(session)
        db.session.commit()
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    dim_hint = ''
    if detected:
        dim_labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k, _ in detected)
        dim_hint = f'\n\n【关键词触发】用户讨论涉及维度：{dim_labels}。若你的回复中产出了可落地的设定内容，请用卡片标记输出（每个维度一张）：\n[[CARD:卡片类型|标题|内容]]\n卡片类型对照：SAVE_CONCEPT=构思, SAVE_RULE=设定, SAVE_WORLDSETTING=世界观, SAVE_OUTLINE_NODE=大纲, SAVE_PLOT=剧情, SAVE_CHARACTER=人物, SAVE_FORESHADOW=伏笔, SAVE_LOCATION=地图, APPLY_STYLE=文风。无则不输出卡片。'
        # 涉及人物维度时，强制约束卡片内容为自然语言，禁止 JSON 符号
        if any(k == 'character_profiles' for k, _ in detected):
            dim_hint += '\n\n【人物卡片内容格式·铁律】绝对禁止 JSON 符号 [ ] { } " : 和英文字段名。卡片内容必须用纯中文，按「姓名：xxx\\n身份：xxx\\n性格：xxx\\n动机：xxx\\n背景：xxx\\n关系：xxx\\n能力：xxx」分行输出，每字段至少30字。'

    sys_prompt = f"""你是资深网文创作智驾，正在与作者自由讨论《{book.title or "未命名"}》。

题材：{book.genre or "未指定"}  类型：{book.book_type or "未指定"}

【已有设定参考】
{ctx}

{("【技能包指引】\n" + skill_note) if skill_note else ""}

请与作者自然对话：讨论剧情、分析人物、推演走向、解答创作疑问。回复简洁有洞察力。
若作者明确要求生成某维度设定，或讨论中形成了可落地的设定内容，可输出对应卡片。
{dim_hint}
"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': message}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        full = []
        try:
            for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=2500):
                full.append(chunk)
                yield sse({'type': 'delta', 'content': chunk})
            content = ''.join(full).strip()
            # 解析卡片标记
            cards = _parse_card_markers(content)
            clean_content = _strip_card_markers(content)
            # 人物卡片兜底：若内容仍为 JSON 数组，转成自然语言
            for card in cards:
                if card.get('type') == 'SAVE_CHARACTER':
                    c = (card.get('content') or '').strip()
                    if c.startswith('[') or c.startswith('{'):
                        card['content'] = _character_profiles_to_text(c) if c.startswith('[') else _character_profiles_to_text('[' + c + ']')
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_content,
                            'cards': [{**c, 'status': 'pending'} for c in cards] if cards else None})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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

    if not book_id or dim_key not in _DIM_KEY_TO_SPEC:
        return jsonify({'error': '缺少 book_id 或 dimension 无效'}), 400

    spec = _DIM_KEY_TO_SPEC[dim_key]
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    ctx, self_content = _build_dim_context(book, bb, dim_key)

    # 注入核心创作参数（卷数/题材/风格），让大纲/剧情等维度严格按全书卷数规划
    core_params = ''
    try:
        from app import _build_core_params_block
        core_params = _build_core_params_block(bb, book) or ''
    except Exception:
        pass

    # 注入构思类技能包
    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='single') or ''
    except Exception:
        pass

    # 大纲维度额外注入五幕模型说明，确保按卷数生成五幕式结构
    outline_extra = ''
    if dim_key == 'plot_design':
        try:
            from app import _get_total_volumes
            tv = _get_total_volumes(bb, book)
            outline_extra = f'\n\n【大纲维度专属要求】请生成「五幕式总纲」，全书严格 {tv} 卷，每卷约50章12万字。五幕模型：立身(1-5%)/立足(5-25%)/立势(25-50%)/立威(50-75%)/立命(75-100%)。为每卷输出：卷号卷名、所属幕、本卷核心目标、主要冲突、关键转折点(2-3个)、卷尾高潮与悬念。只输出总纲文本，不输出详细情节节点。'
            try:
                from app import _cultivation_dimension_hint
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

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》的「{spec['label']}」维度生成 3-5 个差异化的创意方案供作者选择。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

{core_params}

【已有设定参考】
{ctx or "（暂无）"}

{("【当前维度已有内容（可在其基础上补充完善）】\n" + self_content) if self_content else ""}

【作者需求】
{requirement or f"请帮我生成{spec['label']}的设定"}
{outline_extra}{af_alerts_suggest}

{("【技能包指引】\n" + skill_note) if skill_note else ""}

请输出 3-5 个不同切入角度的方案。严格按以下 JSON 格式输出（不要任何其他内容、不要 Markdown 代码块）：
{{
  "suggestions": [
    {{"title": "方案标题（10字内）", "preview": "方案简介（80-150字，说清核心思路和亮点）"}}
  ]
}}"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请生成{spec["label"]}的多选方案'}]

    from app import _call_llm
    content, err = _call_llm(messages, max_tokens=2000, temperature=0.85, task_type='creation')
    if err:
        return jsonify({'error': f'生成方案失败：{err}'}), 500

    suggestions = []
    try:
        m = re.search(r'\{[\s\S]*\}', content or '')
        if m:
            parsed = json.loads(m.group(0))
            suggestions = parsed.get('suggestions', []) or []
    except Exception:
        pass

    # 兜底：按段落切分
    if not suggestions:
        lines = [l.strip() for l in (content or '').split('\n') if l.strip() and not l.strip().startswith('```')]
        for i, line in enumerate(lines[:5]):
            # 去掉前导序号
            clean = re.sub(r'^[\d一二三四五1-5\.、\)\s]+', '', line)
            if clean:
                suggestions.append({'title': f'方案{i + 1}', 'preview': clean[:150]})

    for i, s in enumerate(suggestions):
        s.setdefault('title', f'方案{i + 1}')
        s.setdefault('preview', '')
        s['id'] = f'sug_{i + 1}'

    if not suggestions:
        return jsonify({'error': 'AI 未返回有效方案，请重试或调整需求'}), 500

    return jsonify({
        'suggestions': suggestions,
        'dimension': dim_key,
        'dimension_label': spec['label'],
        'requirement': requirement,
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

    if not book_id or dim_key not in _DIM_KEY_TO_SPEC or not suggestion:
        return jsonify({'error': '参数无效：需要 book_id/dimension/suggestion'}), 400

    spec = _DIM_KEY_TO_SPEC[dim_key]
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    ctx, self_content = _build_dim_context(book, bb, dim_key)

    # 注入核心创作参数（卷数/题材/风格），让大纲/剧情等维度严格按全书卷数规划
    core_params = ''
    try:
        from app import _build_core_params_block
        core_params = _build_core_params_block(bb, book) or ''
    except Exception:
        pass

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='agent') or ''
    except Exception:
        pass

    # 大纲维度额外注入五幕模型说明，确保按卷数生成五幕式结构
    outline_extra = ''
    if dim_key == 'plot_design':
        try:
            from app import _get_total_volumes
            tv = _get_total_volumes(bb, book)
            outline_extra = f'\n\n【大纲维度专属要求】请生成「五幕式总纲」，全书严格 {tv} 卷，每卷约50章12万字。五幕模型：立身(1-5%)/立足(5-25%)/立势(25-50%)/立威(50-75%)/立命(75-100%)。为每卷输出：卷号卷名、所属幕、本卷核心目标、主要冲突、关键转折点(2-3个)、卷尾高潮与悬念。只输出总纲文本，不输出详细情节节点。'
            try:
                from app import _cultivation_dimension_hint
                outline_extra += _cultivation_dimension_hint('plot_design', book, bb)
            except Exception:
                pass
        except Exception:
            pass

    # 剧情维度额外注入全部卷剧情约束，确保按全部卷创作
    timeline_extra = ''
    if dim_key == 'timeline':
        try:
            from app import _get_total_volumes
            tv = _get_total_volumes(bb, book)
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
            timeline_extra = f'\n\n【剧情维度专属要求】全书严格 {tv} 卷，每卷约50章12万字。请基于五幕式总纲生成全部 {tv} 卷的剧情，各卷剧情连贯、卷间衔接（ending_hook与下一卷开头承接），每卷剧情须支撑50章12万字容量。'
            if existing_volumes:
                timeline_extra += f'\n\n【已有卷剧情（须保持连贯，可在其基础上完善）】\n{existing_volumes}'
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
   [{"name":"姜辰","identity":"...","personality":"..."}]
4. 正确格式（必须这样输出，纯中文，每字段一行）：
   姓名：姜辰
   身份：大胤北境边军遗孤，玄骨宗弃徒
   性格：外表沉静寡言，行事果断
   动机：查清蒙冤真相，建立立足之地
   背景：黑骨矿场矿奴出身
   关系：与镇骨营弟兄为生死之交
   能力：骨纹修行，近身搏杀
   物品：残骨刀
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

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》生成「{spec['label']}」维度的完整设定内容。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

{core_params}

【已有设定参考】
{ctx or "（暂无）"}

{("【当前维度已有内容（可在此基础上完善，不要简单重复）】\n" + self_content) if self_content else ""}

【作者需求】
{requirement or "无"}

【选中方案】
{suggestion}
{outline_extra}{timeline_extra}{character_extra}{af_alerts_gen}

{("【技能包指引】\n" + skill_note) if skill_note else ""}

请直接输出该维度的完整设定内容（300-800字），不要寒暄，不要解释，不要加 Markdown 标题。"""

    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            title=f'{spec["label"]}生成', messages_json='[]')
        db.session.add(session)
        db.session.commit()
    session_id = session.id

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    gw = LLMGateway(base_url, api_key, model)

    # 世界观维度：生成后额外产出地图卡片，便于提取到「地图」维度
    if dim_key == 'worldbuilding':
        sys_prompt += '\n\n另外，若世界观中包含地理/势力分布信息，请在正文之后追加一张「地图」卡片，格式：\n[[CARD:SAVE_LOCATION|世界地图架构|在此输出主要地理区域、势力分布、关键地点的简要架构]]'

    # 设定维度：文风已并入设定，额外产出一张「文风」卡片，便于落地到 style_guide
    if dim_key == 'key_rules':
        sys_prompt += '\n\n另外，请基于本书题材与设定，提炼出适配的文风指南（叙事风格、语言调性、节奏把控），在正文之后追加一张「文风」卡片，格式：\n[[CARD:APPLY_STYLE|文风指南|在此输出叙事风格、语言调性、节奏把控等文风约束]]'

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请生成{spec["label"]}的完整内容'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        full = []
        try:
            for chunk in gw.chat_stream(messages, temperature=0.85, max_tokens=2000):
                full.append(chunk)
                yield sse({'type': 'delta', 'content': chunk})
            content = ''.join(full).strip()
            # 人物维度兜底：若 AI 仍输出 JSON 数组，转成自然语言，避免带符号英文内容
            if dim_key == 'character_profiles' and content.lstrip().startswith('['):
                content = _character_profiles_to_text(content)
            # 解析卡片标记（世界观会额外产出地图卡片）
            extra_cards = _parse_card_markers(content)
            clean_content = _strip_card_markers(content) if extra_cards else content
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': spec['card'],
                'title': f'{spec["label"]}（AI智驾生成）',
                'content': clean_content,
                'target': _CARD_TARGET.get(spec['card'], spec['label']),
            }
            yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            for ec in extra_cards:
                yield sse({'type': 'card', 'card': ec, 'session_id': session_id})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'生成{spec["label"]}：{requirement or suggestion[:50]}'})
            history.append({'role': 'assistant', 'content': clean_content,
                            'cards': [{**c, 'status': 'pending'} for c in [card] + extra_cards]})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='agent') or ''
    except Exception:
        pass

    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            title=f'{spec["label"]}修改', messages_json='[]')
        db.session.add(session)
        db.session.commit()
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
        character_extra = '\n\n【人物维度输出格式·绝对铁律】绝对禁止 JSON 符号 [ ] { } " : 和英文字段名。必须用纯中文，按「姓名：xxx\\n身份：xxx\\n性格：xxx\\n动机：xxx\\n背景：xxx\\n关系：xxx\\n能力：xxx」分行输出，每字段至少30字。'

    sys_prompt = f"""你是资深网文创作智驾。请根据作者的修改意见，修订《{book.title or "未命名"}》的「{spec['label']}」维度内容。

【其他维度参考】
{ctx or "（暂无）"}

【当前维度原文】
{current_content or "（暂无）"}

【作者修改意见】
{edit_request}

{("【技能包指引】\n" + skill_note) if skill_note else ""}
{character_extra}

请直接输出修订后的完整内容（保留原文中合理的部分，按修改意见调整），不要寒暄，不要解释，不要加 Markdown 标题。"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请修订{spec["label"]}内容'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        full = []
        try:
            for chunk in gw.chat_stream(messages, temperature=0.7, max_tokens=2000):
                full.append(chunk)
                yield sse({'type': 'delta', 'content': chunk})
            content = ''.join(full).strip()
            # 人物维度兜底：若 AI 仍输出 JSON 数组，转成自然语言
            if dim_key == 'character_profiles' and content.lstrip().startswith('['):
                content = _character_profiles_to_text(content)
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': spec['card'],
                'title': f'{spec["label"]}（AI智驾修订）',
                'content': content,
                'target': _CARD_TARGET.get(spec['card'], spec['label']),
            }
            yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'修订{spec["label"]}：{edit_request}'})
            history.append({'role': 'assistant', 'content': content,
                            'cards': [{**card, 'status': 'pending'}]})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='agent') or ''
    except Exception:
        pass

    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            title=f'批量生成{len(dims)}维度', messages_json='[]')
        db.session.add(session)
        db.session.commit()
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
        generated = {}
        try:
            for dim_key in dims:
                spec = _DIM_KEY_TO_SPEC[dim_key]
                label = spec['label']
                yield sse({'type': 'delta', 'content': f'\n\n正在生成【{label}】…\n\n'})

                ctx_parts = []
                for k, v in generated.items():
                    ctx_parts.append(f'【{_DIM_KEY_TO_SPEC[k]["label"]}】\n{v[:500]}')
                ctx_block = '\n\n'.join(ctx_parts) if ctx_parts else '（暂无）'

                existing = ''
                if bb:
                    existing = (getattr(bb, spec['field'], '') or '').strip()

                sys_prompt = (
                    f'你是资深网文创作智驾。请为《{book.title or "未命名"}》生成「{label}」设定。'
                    f'\n题材：{book.genre or "未指定"}，类型：{book.book_type or "未指定"}。'
                    f'\n\n【已生成维度参考】\n{ctx_block}'
                    f'\n\n【作者补充要求】\n{requirement or "无"}'
                    f'{(chr(10) + chr(10) + "【技能包指引】" + chr(10) + skill_note) if skill_note else ""}'
                    f'\n\n请直接输出该维度的设定内容（300-600字），不要寒暄，不要解释。'
                )
                if existing:
                    sys_prompt += f'\n\n【已有内容（可补充完善，不要简单重复）】\n{existing[:400]}'

                messages = [{'role': 'system', 'content': sys_prompt},
                            {'role': 'user', 'content': f'请生成{label}'}]
                content = ''
                try:
                    for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=1500):
                        content += chunk
                        yield sse({'type': 'delta', 'content': chunk})
                except Exception as e:
                    yield sse({'type': 'error', 'error': f'{label}生成失败：{e}'})
                    continue

                content = content.strip()
                generated[dim_key] = content
                card = {
                    'id': str(uuid.uuid4())[:8],
                    'type': spec['card'],
                    'title': f'{label}（AI智驾生成）',
                    'content': content,
                    'target': _CARD_TARGET.get(spec['card'], label),
                }
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})

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
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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

    # 注入去AI味技能包（review 类，限定 deai 相关 prompt_keys）
    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(
            skill_pack_ids, 'review',
            ['tomato_deai', 'de_ai_flavor', 'polish', 'consistency_check'],
            mode='agent'
        ) or ''
    except Exception:
        pass

    # 无技能包时使用统一去AI味规则（与正文写作/修改共用）
    if not skill_note:
        skill_note = DEAI_RULES

    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_deai',
                            title=f'去AI味·{chapter.title}', messages_json='[]')
        db.session.add(session)
        db.session.commit()
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

{skill_note}

【全文设定参考】
{bible_ctx or "（暂无）"}

【硬性约束】
1. 只输出纯正文，不要输出章节标题（标题由系统保留，不要重复输出）。
2. 修改后纯正文字数与原文 {orig_wc} 字相近（±10%），保留原章节的剧情走向和钩子，只改文风不改剧情。
   字数统计口径：中文字符+中文标点（全角标点计入，半角标点不计入，英文按单词、数字按串）。请用全角中文标点。
3. 不要加 Markdown 代码块，不要解释，不要在文末附加字数统计。"""

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请审校以下章节正文：\n\n{raw_content}'}]

    def sse(payload):
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    def generate():
        full = []
        try:
            yield sse({'type': 'delta', 'content': f'正在为《{chapter.title}》去AI味…\n\n'})
            for chunk in gw.chat_stream(messages, temperature=0.5, max_tokens=4096):
                full.append(chunk)
                yield sse({'type': 'delta', 'content': chunk})
            content = ''.join(full).strip()
            # 去掉可能的 Markdown 代码块包裹
            content = re.sub(r'^```[a-zA-Z]*\n', '', content)
            content = re.sub(r'\n```$', '', content).strip()
            # 防御性剥离标题行：保证 card.content 为纯正文（与落地字数口径一致）
            _, body_content = _strip_chapter_title(content, fallback_title=chapter.title or '')
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': 'SAVE_CHAPTER',
                'title': chapter.title,
                'content': body_content,
                'target': '章节正文',
            }
            yield sse({'type': 'card', 'card': card, 'session_id': session_id,
                       'meta': {'chapter_id': chapter_id, 'replace': True}})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'去AI味：{chapter.title}'})
            history.append({'role': 'assistant', 'content': body_content,
                            'cards': [{**card, 'status': 'pending'}]})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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
    from app import db, Book, BookBible, AIConfig
    from llm_gateway import get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    report_id = data.get('report_id')
    skill_pack_ids = data.get('skill_pack_ids') or []

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
    diag_parts = []
    for key, label in [('violations', '一致性违规'), ('pending_foreshadowing', '待回收伏笔'),
                       ('narrative_debt', '叙事债务'), ('suggestions', '改进建议'),
                       ('character_cognition_issues', '角色认知问题')]:
        items = target_report.get(key) or []
        if isinstance(items, list) and items:
            diag_parts.append(f'■ {label}（{len(items)}项）')
            # 诊断项截断：避免报告过大导致 LLM 上下文超限或响应极慢
            limit = 8 if key == 'violations' else 5
            for it in items[:limit]:
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
                    diag_parts.append(line)
                else:
                    diag_parts.append(f'  - {str(it)[:120]}')
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
            dim_now_parts.append(f'【{label}·当前内容】\n{val[:800]}')
    dim_now_text = '\n\n'.join(dim_now_parts) or '（各维度暂无内容）'

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'review', mode='agent') or ''
    except Exception:
        pass

    system_prompt = f"""你是资深网文设定修正师。任务：基于防遗忘检查报告的诊断，对小说设定维度内容生成修正方案。

【防遗忘检查报告诊断要点】
{diag_text}

【各维度当前内容】
{dim_now_text}
{chr(10) + chr(10) + '【技能包指引】' + chr(10) + skill_note if skill_note else ''}

【你的任务】
针对报告诊断出的问题，逐维度生成「修正方案」。每个维度一个修正项，包含：
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
