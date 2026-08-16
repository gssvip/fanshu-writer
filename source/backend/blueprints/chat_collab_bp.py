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
# 统一去AI味规则（融合“默认去AI味规则”+“去AI味执行规则精简版”）
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

def load_session_messages(session) -> list[dict]:
    """从 AISession 加载消息（兼容旧 messages_json 和新 messages 关联）。"""
    msgs = []
    try:
        msgs = json.loads(session.messages_json or '[]')
    except Exception:
        msgs = []
    return msgs if isinstance(msgs, list) else []


# ============================================================================
# 会话历史瘦身 + 安全提交（解决 messages_json 150KB+ 导致 PG SSL 断连）
#
# 根因：ai_sessions.messages_json 把完整卡片内容（章节正文/timeline JSON 数组）
#       连同历史消息一起存进一个大 JSON 字符串，一次 UPDATE 动辄 100~300KB，
#       Render/Neon 的 PG 代理层对大包+SSL 非常敏感，直接掐断连接 → OperationalError。
#
# 解法：
#   1. 落盘前瘦身：卡片内容只保留元信息（id/type/title/target/status），不存正文；
#      单条消息截到 800 字；总轮次上限 12；总 JSON 上限约 48KB。
#   2. 提交捕获断连异常：rollback → 引擎 dispose 丢弃所有死连接 → 重查 session → 重试一次。
# ============================================================================

# 落盘前单条消息最大字符（正文写作单条 AI assistant 内容可能 6000+ 字，会超）
_PERSIST_MSG_MAX_CHARS = 800
# 落盘前卡片内容最大字符（卡片正文其实会落地到 Chapter/BookBible，session 里仅作回显，不需要完整）
_PERSIST_CARD_CONTENT_MAX_CHARS = 120
# 落盘前最多保留的"消息条数上限"（12 轮 = 24 条消息；一般够用）
_PERSIST_MAX_MSGS = 24
# 总 JSON 字符硬上限：超过就继续砍中间轮次，直到 ≤ 这个值 或 只剩最后 4 条
_PERSIST_TOTAL_MAX_CHARS = 48 * 1024


def _compact_history_for_persist(history: list) -> list:
    """落盘前瘦身：把历史消息压到 PG 小包安全线以内。

    规则（顺序执行）：
      1. 砍卡片 content：每条 cards[*].content 截断到 120 字（卡片正文已落地在 BookBible/Chapter，session 里不用冗余保存全文）
      2. 砍消息 content：每条 message.content 截断到 800 字
      3. 砍历史深度：只保留最后 24 条消息
      4. 若总 JSON 还超 48KB：循环砍中间消息，直到合规或只剩最后 4 条

    返回：新的 list（不原地修改传入 history，避免影响 SSE 正在发的卡片内容）
    """
    import copy
    if not history:
        return []
    # 深拷贝，防止改到 SSE 还在用的引用
    h = copy.deepcopy(history)
    if not isinstance(h, list):
        return []

    # Step1 + Step2：逐条瘦身
    for m in h:
        if not isinstance(m, dict):
            continue
        # 消息正文截断
        c = m.get('content')
        if isinstance(c, str) and len(c) > _PERSIST_MSG_MAX_CHARS:
            m['content'] = c[:_PERSIST_MSG_MAX_CHARS] + '\n…（会话历史超长已截断，完整内容以采纳落地后的维度/章节为准）'
        # 卡片列表内容截断（最关键，卡片 content 可能是 6000 字正文或 80KB timeline JSON）
        cards = m.get('cards')
        if isinstance(cards, list):
            for c2 in cards:
                if not isinstance(c2, dict):
                    continue
                cc = c2.get('content')
                if isinstance(cc, str) and len(cc) > _PERSIST_CARD_CONTENT_MAX_CHARS:
                    c2['content'] = cc[:_PERSIST_CARD_CONTENT_MAX_CHARS] + '…'

    # Step3：深度限制（保留最后 N 条，避免几十轮对话堆起来）
    if len(h) > _PERSIST_MAX_MSGS:
        h = h[-_PERSIST_MAX_MSGS:]

    # Step4：总字符兜底 —— 还超 48KB 就砍中间消息，保留首尾
    def _total_chars(xs):
        return len(json.dumps(xs, ensure_ascii=False))

    _safety = 0
    while _total_chars(h) > _PERSIST_TOTAL_MAX_CHARS and len(h) > 4 and _safety < 30:
        _safety += 1
        mid = len(h) // 2
        # 砍中间 2 条（一般是一对 user+assistant），加速收敛
        if mid - 1 >= 1:
            del h[mid - 1:mid + 1]
        else:
            del h[mid:mid + 1]
    return h


def _safe_save_session_messages(session, history: list) -> None:
    """会话消息落盘 + 处理 PG SSL 断连（OperationalError）重试。

    流程：
      1. 对 history 做瘦身（卡片/消息截断、深度限制、48KB 总上限）
      2. 设置 session.messages_json / updated_at 并 commit
      3. 命中 OperationalError（连接被掐断）时：
         - rollback → engine.dispose() 扔僵尸连接 → 重查 session → 再 commit 1 次
    """
    from sqlalchemy.exc import OperationalError as SAOperationalError
    from app import db as _db, app as _app

    slim_history = _compact_history_for_persist(history)
    session.messages_json = json.dumps(slim_history, ensure_ascii=False)
    session.updated_at = datetime.now(timezone.utc)

    def _do_commit(sess_obj):
        _db.session.add(sess_obj)
        _db.session.commit()

    try:
        _do_commit(session)
    except SAOperationalError as e1:
        try:
            _db.session.rollback()
        except Exception:
            pass
        # dispose 扔掉池中所有连接（彻底重置 SSL 管道）
        try:
            _db.get_engine(_app).dispose()
        except Exception:
            pass
        # 新连接重查 session，再提交一次
        try:
            from app import AISession
            sess2 = AISession.query.get(session.id)
            if sess2 is None:
                raise
            sess2.messages_json = json.dumps(_compact_history_for_persist(history), ensure_ascii=False)
            sess2.updated_at = datetime.now(timezone.utc)
            # 如果调用方还改了 session.title（例如 _persist_action_session），同步过去
            if getattr(session, 'title', None):
                sess2.title = session.title
            _do_commit(sess2)
            try:
                session.messages_json = sess2.messages_json
                session.updated_at = sess2.updated_at
                if getattr(sess2, 'title', None):
                    session.title = sess2.title
            except Exception:
                pass
        except Exception as e2:
            raise RuntimeError(
                f'Session 保存失败（首次 {type(e1).__name__}: {e1}；重试 {type(e2).__name__}: {e2}）'
            )


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

    # 获取或创建会话
    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope=scope, title=message[:30], messages_json='[]')
        db.session.add(session)
        db.session.commit()
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
        full_text = []
        try:
            # SSE 首帧 ①：核心创作参数同步结果（若用户这条消息触发了卷数/章数调整，先告诉前端已落地）
            if params_sync_notes:
                yield f'data: {json.dumps({"type": "meta", "kind": "params_sync", "info": {"notes": params_sync_notes}}, ensure_ascii=False)}\n\n'
            # SSE 首帧 ②：返回命中的章节/维度（用于前端回显"已定位并注入…"提示）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield f'data: {json.dumps({"type": "meta", "kind": "auto_context", "info": auto_ctx_info}, ensure_ascii=False)}\n\n'

            for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=4096):
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
            # 尝试解析 content 为 JSON 数组（可能被 _clean_text_to_plain 清理或被 markdown 包裹）
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

                # 按 volume_index upsert（覆盖同卷、追加新卷）
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
                            # 覆盖同卷
                            existing_vols[i] = nv
                            matched = True
                            break
                    if not matched:
                        existing_vols.append(nv)

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
        #  含人物卡(character_profiles JSON) / 剧情 timeline / 世界观 / 宗门 / 功法 / 地点……
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
            # 副驾首帧：参数同步说明（若有）
            if params_sync_notes_action:
                yield sse({'type': 'meta', 'kind': 'params_sync', 'info': {'notes': params_sync_notes_action}})
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

        # 注入核心创作参数铁律（批量设定也要遵守总卷数/题材/风格）
        core_iron = _core_params_iron_block(bb, book)
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》生成“{label}”设定。'
            f'\n\n{core_iron}'
            f'\n\n已有设定参考：\n{ctx_block}'
            f'\n用户补充要求：{instruction or "无"}'
            f'\n请直接输出该维度的设定内容（300-600字），不要寒暄，不要解释。'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
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
    from app import db, BookBible, Chapter, _get_total_volumes, _get_chapters_per_volume
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
                        ranked_parts.append(f'【本章剧情·精确命中】\n{c.content}\n（请严格按此情节节点推进本章剧情，不得跳过或偏移）')
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
            f'\n\n{core_iron}'
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
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
            f'\n\n请直接输出（标题+空行+正文），不要解释，不要在文末附加字数统计。'
        )
        user_msg = f'请润色第 {target_chapter_num} 章'
    else:
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》续写第 {target_chapter_num} 章正文。'
            f'\n\n{core_iron}'
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
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
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
        corrected, wc_note = _ensure_word_count(
            body_content, api_key=api_key, base_url=base_url,
            model=model, max_tokens=4096, chapter_num=target_chapter_num,
            count_fn=count_words)
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

NARRATIVE_CRAFT_RULES = """
【叙事工艺铁律·平台级约束·所有输出必须遵守】

0. 总则

· 所有输出（正文、大纲、设定、人物、世界观、伏笔）必须遵守本规则。
· 每章2300-2500字（中文汉字，含标点）。写事为主，景一笔带过，非必要不用比喻/拟人。
· 段落句式：短句为主，手机端阅读不超过两行；多用对话推动剧情，对话独立成段，动作与神态紧跟对话。
· 去AI味≠润色：不改原意、不增剧情、不换风格；优先删，其次换，最后改写。
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
· 正确行为：暴雨中把伞递给仇人、对敌人微笑递刀、恨她却替她挡劫。
· 水面行为下埋可脑补动机：前世欠她、伞里有追踪器、不再让她挡刀。
· 背景只露一角：在反常行为出现的瞬间露出，让读者追更求解。
· 有瑕疵、会纠结、口是心非；禁完美人设，禁OOC；无纯反派，所有行为有合理动机。

3. 去AI味与行文规范

3.1 解释腔
· 禁信号词：这说明、这意味着、由此可见、换句话说、事实上、显然、本质上、归根结底、从某种意义上说、不得不说、毋庸置疑。
· 识别模式：动作后跟旁白解释；段尾升华（成长/命运/人性）；同一因果换词说两遍。
· 处理优先级：
  1）删——无信息量解释句直接删。
  2）压——必要因果压成短句，一句说完。
  3）交还行为——用动作、停顿、视线、语气、选择替代旁白。
  4）落回场景——段尾总结改为具体后果、反应或环境变化。

3.2 对白
· 禁：角色替作者解释剧情；自我剖析完整；硬塞世界观/背景；不同角色语气同质；冲突中仍客气过顺；使用书面连接词（因此/然而/与此同时/换句话说）。
· 处理：
  1）保留信息——确认必须留下的剧情事实、关系变化。
  2）删讲解——心理分析、主题总结、背景说明直接删。
  3）调语气——句子符合角色身份和当前情绪，不强行口语化。
  4）造缺口——用半句话、停顿、反问、打断替代完整解释。
  5）拆长对白——拆成两三次回应，中间插动作、停顿或对方插话。
· 额外约束：角色越紧张/越隐瞒/越愤怒，越不该把话说完整漂亮；重要信息像角色当下说的，不像作者塞的。

3.3 工整句式
· 禁：多段等长或同构；排比/三连过密；段尾总在总结/点题/升华；路标词密集（然而/同时/此外/更重要的是/总而言之）；观点句+解释+段尾总结模板。
· 处理：
  1）保顺序——事件顺序不能乱。
  2）破模板——连续三段形状相似时，至少调整一段开头、长度或收束。
  3）删路标——能用动作、后果、场景变化承接就不加连接词。
  4）调长短——短、中、厚段按内容需要交替。
  5）弱收束——段尾停在细节、动作或未说完的余波上。

3.4 段落与句式
· 段落手机端不超过3行；对话/动作独立成段；心理描写一句话。
· 禁连续3句以上主谓结构；五种句式交替：动作/神态前置、名词/称号前置、环境/拟声词前置、连动词串联。

3.5 禁词与口语化

禁词/短语（出现即删，仅保留“一股杀气”“一抹笑意”）
一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为、甚为、极为、缓缓、淡淡、轻轻、微微、毫无疑问、毋庸置疑、不言而喻、显而易见、因此、然而、由此可见、总而言之、综上所述、深吸一口气、眼中闪过一丝、心中暗想、心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、淡漠、漠然、眸子、嘴角微微上扬、如同、宛如、犹如、周身、周遭、气息、威压、那道身影、说话间、话音未落、当即、顿时、瞬时、有意思、深深一眼、不是A是B结构。

推荐口语词
合着、整半天、好家伙、说白了、不是……你、得了吧、拉倒吧、至于么、啥玩意、啥情况、搁这、没跑了、差不离、差不多得了、说实话、说真的、怎么说呢、你别说、还真别说。

语气词：啊、嘛、呗、呢、嗷、哇、咧、哒、喽。
标点：？？？震惊/无语；！！！愤怒/激动；……无语/沉默/欲言又止。

3.6 开头与结尾
· 开头第一句必须为：时间、动作、对话、状态、事件之一。
· 绝对禁止开头：环境描写、心理描写、世界观说明、评价性开场。
· 结尾必须有钩子（悬念/冲突/危机/转折）。
· 绝对禁止结尾：总结性话语、评价性话语、升华性话语。

3.7 对话驱动
· 对话三功能：推进剧情（做决定、传信息、造冲突）；塑造性格（不同角色说不同话）；制造爽感（嘴炮、吐槽、互怼、装逼）。
· 自检：删掉这句话会影响剧情吗？不会就删。

4. 情绪与描写

· 情绪直给：只写外在表现（表情/动作/语言），不写内心感受；写了动作就不写感受。
· 环境描写不超过正文总量15%；重点刻画动作、微表情、矛盾心理。
· 人味注入：不合逻辑但真实细节；思维跳跃；角色互怼/嘴炮；荒诞逻辑/情绪波动。

5. 情节、伏笔与世界观

· 情节结尾停在动态动作或悬念上，禁抒情总结升华。
· 对话有潜台词，不说大道理；出场人物有名有姓；章节无缝衔接。
· 爽点靠信息差和布局，不开上帝视角；感情线绑定主线。
· 禁顿悟式成长、大段景物抒情、人物语气同质化、行为逻辑割裂。
· 伏笔晒宝：三章内让读者意识到伏笔；水上情节节奏明快；水下伏笔在爽点事件中露出异常细节；三章内兑现，让读者恍然大悟。
· 世界观冰山：开篇不铺三千字设定，只展示当前层级；更高层级从他人惊恐、古籍、禁地传说、大佬失态中露一两个字；让读者自己补全。

6. 自检清单

· 设定一致：人物行为/性格/语言与大纲一致；势力数量/分布/关系一致；事件按细纲时间线；战力不超设定；物品/技能不超前；关系转变有铺垫。禁越级、战力崩坏、信息泄露、时间穿越、数量膨胀、关系跳级。
· 章节连贯：人物状态延续；时间线衔接；事件承接；地理连续；新人物有铺垫；物品/信息延续。
· AI味特征：出现总结升华、排比抒情、精确比喻超过1个、过度心理描写、场景过渡不硬切、评价旁白、对称结构、三连排、系统太干净、旁白太统一、解释性叙述、标注式情绪、不是A是B结构，即砍。
· 开头结尾：第一句为时间/动作/对话/状态/事件之一；结尾有钩子，无总结/评价/升华。
""".strip()

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
  - main_plot 本卷主线剧情
  - core_conflict 核心冲突
  - ending_hook 卷尾钩子
  - nodes[].title 节点标题
  - nodes[].summary 节点概要
  - nodes[].cool_type 爽感类型

以上字段文本内容必须遵守：

【0. 总则】
· 写事为主，景一笔带过；短句为主，多用对话推进，动作与神态紧跟对话。
· 爽点直白清晰，深层动机让读者脑补；转折来自已埋伏笔或人物动机，禁天降巧合。
· 核心口诀：行动往上浮，动机往下潜；先让读者爽，再让他细思极恐。

【1. 冰山与结构】
· 水上1/8（情节/爽点/打脸/升级/赚钱）清晰直白，直接喂给读者；
· 水下7/8（动机/伏笔/创伤/执念/世界观深层）让读者能脑补，不写成设定说明书。
· 每卷 ending_hook 必须是动态悬念/冲突/转折，禁抒情总结升华。
· nodes[] 中的情节节点必须能被后续章节回收（契诃夫之枪原则）。

【2. 人物】
· main_plot / nodes[].summary 中禁止贴"冷酷/温柔/腹黑"这类标签，须用反常行为刻画；
· 水面行为下埋可脑补动机；背景只露一角；人物有瑕疵/纠结/口是心非；禁完美人设。

【3. 去 AI 味】
· 禁信号词：这说明/这意味着/由此可见/换句话说/事实上/显然/本质上/归根结底/不得不说/毋庸置疑/因此/然而/与此同时/总而言之/综上所述；
· 禁工整句式：多段排比三连/段尾总总结升华/路标词密集/观点句+解释+段尾总结模板；
· 禁典型 AI 短语：一股杀气/一抹笑意/不由得/不禁/随即/与此同时/缓缓/淡淡/微微/眼中闪过一丝/心中暗想/心念电转/恍然大悟/面无表情/淡漠/眸子/嘴角微微上扬/如同/宛如/犹如/周身/威压/那道身影/话音未落/当即/顿时；
· 推荐口语化表达：合着/整半天/好家伙/说白了/得了吧/啥情况/搁这/没跑了/差不离/差不多得了/说实话；
· main_plot/ending_hook/summary 结尾停在动态动作或悬念，禁总结升华句；
· 爽感类型（cool_type）用精确分类名（实力碾压/智商碾压/扮猪吃虎/打脸装逼/信息差爽感/情感爆发/悬念反转…），不说空话。

【4. JSON 兼容排版约束】
· 为保持 JSON 语法合法，所有字符串值内：
  1）绝对禁止出现未转义的反斜杠 \\ ；
  2）绝对禁止出现未转义的双引号 " ；
  3）绝对禁止 Markdown 符号 * 、 # 、 行首 - 、 > 引用、 ``` 代码块；
  4）列表/条目用"一、二、三、"或"1）2）3）"或"其一其二"，不要 1. 2. 3. 编号；
  5）强调用书名号《》或中文引号，不要 **加粗** 不要 *斜体* 。
· 直接写干净中文短句，段落感用中文标点自然体现。

【5. 自检清单】
· 设定一致：人物行为/性格/语言与大纲一致；势力数量/分布/关系一致；战力不超设定；物品/技能不超前；关系转变有铺垫。
· 卷间连贯：第N卷 ending_hook 与第N+1卷开头严格衔接；各卷 nodes 连续编号不重叠。
· AI 味特征：总结升华/排比抒情/精确比喻超1个/评价旁白/对称结构/三连排/解释性叙述/不是A是B结构 → 即砍。
· 情节节点：每个 node 的 title+summary 必须是一个具体可写章节的情节推进，不是空话。
""").strip()

PLAIN_TEXT_LAYOUT_RULES = """
【纯文字排版铁律·平台级约束·所有输出必须遵守】
（本条对正文、大纲、设定、人物、世界观、伏笔等所有内容生效；Action Card 内的卡片标题和卡片内容也必须遵守）

""".strip() + "\n\n" + NARRATIVE_CRAFT_RULES + """

八、纯文字排版补充约束
1. 绝对禁止任何 Markdown 标记符号，包括：
   一）禁止 # 开头的标题（不要写 # 标题、## 二级标题这类形式）
   二）禁止 * 作为强调/列表/斜体/粗体（不要写 *xxx*、**xxx**、行首 * 列表）
   三）禁止行首 - 短横线列表（" - xxx" / "- xxx" 都不允许）
   四）禁止行首 > 引用块
   五）禁止 ``` 代码块
   六）禁止用 1. / 2. / (1) 这类编号列表符号
2. 正确的纯文字排版形式：
   一）分节标题：直接写成“第一幕：XXX”“本卷目标”“第3卷·XX卷”“姜辰”等，前后各空一行即可（不要加#、不要加*）
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
    {'key': 'concept',            'label': '构思',       'field': 'concept',            'card': 'SAVE_CONCEPT',      'icon': '💡', 'hint': '一句话讲清故事核：主角是谁、要什么、最大的阻碍'},
    {'key': 'key_rules',          'label': '设定',       'field': 'key_rules',          'card': 'SAVE_RULE',         'icon': '⚙️', 'hint': '能力体系/修炼体系/科技树，硬规则'},
    {'key': 'worldbuilding',      'label': '世界观',     'field': 'worldbuilding',      'card': 'SAVE_WORLDSETTING', 'icon': '🌍', 'hint': '故事发生的世界，独特规则或设定（生成中会提取世界地图架构到“地图”维度）'},
    {'key': 'plot_design',        'label': '大纲',       'field': 'plot_design',        'card': 'SAVE_OUTLINE_NODE', 'icon': '📋', 'hint': '主线走向，三幕式或起承转合'},
    {'key': 'character_profiles', 'label': '人物',       'field': 'character_profiles', 'card': 'SAVE_CHARACTER',    'icon': '👤', 'hint': '主角和核心配角的动机、性格、关系网'},
    {'key': 'timeline',           'label': '剧情',       'field': 'timeline',           'card': 'SAVE_PLOT',         'icon': '📖', 'hint': '关键剧情节点的时间顺序'},
    {'key': 'foreshadowing',      'label': '伏笔',       'field': 'foreshadowing',      'card': 'SAVE_FORESHADOW',   'icon': '🔮', 'hint': '长线伏笔的埋设与回收计划'},
    {'key': 'locations',          'label': '地图',       'field': 'locations',          'card': 'SAVE_LOCATION',     'icon': '🗺️', 'hint': '故事中的地点、势力分布、世界地图架构'},
    {'key': 'style_guide',        'label': '文风',       'field': 'style_guide',        'card': 'APPLY_STYLE',       'icon': '🎨', 'hint': '叙事风格、语言调性、节奏把控'},
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
    # timeline 是 JSON 数组：至少 1 卷且每卷有 main_plot
    if dim_key == 'timeline':
        try:
            vols = json.loads(val)
            return isinstance(vols, list) and len(vols) > 0 and \
                   all(isinstance(v, dict) and (v.get('main_plot') or '').strip() for v in vols)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    # character_profiles 可能是 JSON 数组或纯文本：至少有 1 个人物
    if dim_key == 'character_profiles':
        if val.startswith('['):
            try:
                chars = json.loads(val)
                return isinstance(chars, list) and len(chars) > 0
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return len(val) >= 50
    # 其他维度：至少 50 字
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

    # ===== 技能包 =====
    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='agent') or ''
    except Exception:
        pass

    # ===== 会话 =====
    session = None
    if session_id:
        session = AISession.query.get(session_id)
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            title='通用聊天', messages_json='[]')
        db.session.add(session)
        db.session.commit()
    session_id = session.id

    # ===== 复用 chat_smart 的 system prompt + TOC + 定位铁律（核心）=====
    toc_block = _build_toc_block(book_id)
    base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)

    # 通用聊天专属追加：关键词命中卡片产出提示 + 技能包指引 + 增强索要资料禁令（第二保险）
    extra_parts = []
    if skill_note:
        extra_parts.append(f'\n【技能包指引】\n{skill_note}')
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
        full = []
        try:
            # SSE 首帧 meta：命中章节/维度（前端提示"已定位并注入"）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield sse({'type': 'meta', 'kind': 'auto_context', 'info': auto_ctx_info})

            for chunk in gw.chat_stream(messages, temperature=0.8, max_tokens=4096):
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

    # 构思类技能包注入
    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='single') or ''
    except Exception:
        pass

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

【重要·方案卡格式】请输出 3-5 个不同切入角度的方案。严格按以下 JSON 格式输出（不要任何其他内容、不要 Markdown 代码块）：
{{
  "suggestions": [
    {{"title": "方案标题（10字内）", "preview": "方案简介（80-150字，说清核心思路和亮点）"}}
  ]
}}

【最终自检（在输出 JSON 之前必须做完）】
1. 检查每条 preview 里提到的卷数（如果提到）是否等于用户设定的实际卷数（={tv_for_suggest if (tv_for_suggest and tv_for_suggest>=1) else '未设定，可省略'}）；
2. 禁止出现"十卷、五卷、5-8 卷、十余卷"等默认数字；
3. 若本任务属于大纲/剧情/构思维度，每条 preview 必须显式包含"{tv_for_suggest if (tv_for_suggest and tv_for_suggest>=1) else '__'}卷"字样；
4. JSON 合法，无多余逗号，suggestions 长度 3-5。

{PLAIN_TEXT_LAYOUT_RULES}"""

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

    if not book_id or dim_key not in _DIM_KEY_TO_SPEC or not suggestion:
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
                timeline_extra = f'\n\n【剧情维度专属要求】全书严格 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章。请基于五幕式总纲生成全部 {tv} 卷的剧情，各卷剧情连贯、卷间衔接（ending_hook与下一卷开头承接），每卷剧情须支撑 {cpv} 章容量。'
                if existing_volumes:
                    timeline_extra += f'\n\n【已有卷剧情（须保持连贯，可在其基础上完善）】\n{existing_volumes}'
                # JSON 数组格式铁律（与 ai_master_create 保持一致，落地端按 volume_index upsert）
                timeline_extra += f'''

【分卷铁律·必读】**全书共 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章**。卷序号从 1 开始连续递增到 {tv}。卷名格式"第N卷 副标题"。必须覆盖全部 {tv} 卷，不得多不得少。

【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；各卷 chapters 全书连续编号。

【分卷章节分配】全书 {total_chapters} 章 → {tv} 卷（每卷 {cpv} 章）：第1卷 1-{cpv}、第2卷 {cpv + 1}-{cpv * 2}、... 第{tv}卷 {(tv - 1) * cpv + 1}-{total_chapters}；每卷 nodes 章节连续不重叠。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹在 markdown 代码块中，不要任何解释性文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "main_plot": "本卷主线剧情（100-200字）",
    "core_conflict": "本卷核心冲突",
    "ending_hook": "本卷卷尾钩子具体内容",
    "nodes": [
      {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "实力碾压"}}
    ]
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。'''
            else:
                # 作者未指定总卷数（tv=0）：让 LLM 先给出建议 N 卷，再按 N 卷输出 JSON
                # 禁止任何默认十卷/五卷/十二卷/5-8卷的数字；JSON 结构与 tv 明确时完全一致
                timeline_extra = f'\n\n【剧情维度专属要求】作者尚未指定总卷数。请你先自行确定一个合理的分卷规模 N（N≥2，禁止擅自默认十卷/五卷/十二卷/十余卷/5-8卷等固定值），再按 N 卷生成完整剧情，每卷剧情须支撑约 {cpv} 章容量，卷间衔接（ending_hook 与下一卷开头承接）。'
                # JSON 数组格式铁律（同上，卷数改成"N卷/第N卷"占位规则）
                timeline_extra += f'''

【分卷铁律·必读】方案建议 N 卷、每卷约 {cpv} 章、全书约 N×{cpv} 章（N 就是你方案里确定的卷数，禁止擅自写死 10）。卷序号从 1 开始连续递增到 N，卷名格式"第N卷 副标题"。必须覆盖全部 N 卷，不得多不得少。

【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；各卷 chapters 全书连续编号。

【分卷章节分配】N×{cpv} 章 → N 卷（每卷 {cpv} 章）：第1卷 1-{cpv}、第2卷 {cpv + 1}-{cpv * 2}、...、第N卷 {(cpv * (N - 1)) + 1}-{cpv * N}；每卷 nodes 章节连续不重叠。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹在 markdown 代码块中，不要任何解释性文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "main_plot": "本卷主线剧情（100-200字）",
    "core_conflict": "本卷核心冲突",
    "ending_hook": "本卷卷尾钩子具体内容",
    "nodes": [
      {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "实力碾压"}}
    ]
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。'''
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

    # 【P0修复】timeline 维度输出按卷 JSON 数组，末尾不附加"300-800字纯文本"规则
    # 否则 AI 会输出纯文本大纲，落地时无法按卷 upsert。
    # 但剧情维度 main_plot/ending_hook/nodes.summary 等字段是自然语言文本，
    # 仍必须遵守叙事工艺铁律，注入 JSON 兼容的专用版。
    if dim_key == 'timeline':
        _tail_rule = f'\n\n{TIMELINE_NARRATIVE_RULES}'
    else:
        _tail_rule = f'\n\n请直接输出该维度的完整设定内容（300-800字），不要寒暄，不要解释，不要加 Markdown 标题。\n\n{PLAIN_TEXT_LAYOUT_RULES}'

    # 预拼接块（Python 3.11 禁止 f-string 表达式内含反斜杠，故先算好再引用）
    _self_content_block = ("【当前维度已有内容（可在此基础上完善，不要简单重复）】\n" + self_content) if self_content else ""
    _skill_note_block = ("【技能包指引】\n" + skill_note) if skill_note else ""

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
{suggestion}
{outline_extra}{timeline_extra}{character_extra}{af_alerts_gen}

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

            # timeline 维度需要更多 token（全书各卷 JSON 输出较长）
            max_tok = 6000 if dim_key == 'timeline' else 2000

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
            retry_done = False
            max_attempts = 4  # 首次 + 最多 3 次重试（用户反馈经常空，再提高一次保障率）
            _EMPTY_FALLBACK_LEN = 30  # 兜底阈值从 80 降到 30，人物/文风/伏笔这类短内容维度也能触发
            for _attempt in range(max_attempts):
                full = []
                # 重试策略：初始温度从 0.7 起步（原 0.85 太高，容易天马行空或拒答），重试再微增
                _temp = 0.7 + min(_attempt * 0.08, 0.2)
                _max_tok = max_tok
                if _attempt == 1:
                    _max_tok = min(int(max_tok * 1.5), 8000)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), 8000)
                # 从第二次开始进入"精简模式"——截断过长 system/铁律，防 prompt 溢出 → 模型直接拒答吐空
                _msgs_for_this_call = cur_messages
                if _attempt >= 1:
                    _msgs_for_this_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                for chunk in gw.chat_stream(_msgs_for_this_call, temperature=_temp, max_tokens=_max_tok):
                    full.append(chunk)
                    yield sse({'type': 'delta', 'content': chunk})
                raw_joined = ''.join(full)
                # ====== 【EMPTY_OUTPUT 兜底增强版】 ======
                # Step1：先全局剥离 think 标签（R1 系列模型最多的问题）
                raw_no_think = _strip_think_tags(raw_joined)
                cleaned = raw_no_think.strip()
                if dim_key == 'timeline':
                    # timeline：仅 fence 清理 + JSON 规整（原逻辑）
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
                    # 保守清理
                    cleaned = _clean_text_to_plain(cleaned)
                    # 人物维度：JSON 数组转自然语言
                    if dim_key == 'character_profiles' and cleaned.lstrip().startswith('['):
                        cleaned = _character_profiles_to_text(cleaned)
                # EMPTY_OUTPUT 二次保护：清理后仍空 但 raw(去think后) 内容有明显字数 → 用原始仅去 fence/html 的版本
                if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                    # 仅剥离最外层围栏 + HTML 换行标签，保留一切内容，宁可脏也不要空
                    fallback = raw_no_think.strip()
                    m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                    if m:
                        fallback = m.group(1).strip()
                    fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                    fallback = re.sub(r'</?p>', '\n', fallback)
                    if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                        cleaned = fallback.strip()
                # EMPTY_OUTPUT 三次保护：客套/拒答检测（"好的没问题"这种内容也当空）
                if cleaned and _is_refusal_or_fluff(cleaned):
                    cleaned = ''
                content = cleaned
                # 自检校验（raw 内容长度用去 think 后版本，更准确反映模型真实吐字量）
                issues = validator.validate(dim_key, content, raw_length_hint=len((raw_no_think or '').strip()))
                _log_validation_issues(bb, dim_key, issues)
                if not validator.should_retry(issues) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                # 需要重试：带错误反馈重新生成（附带 attempt info 给前端显示"已自动重试 N"）
                retry_hint = validator.build_retry_hint(issues)
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
            # 解析卡片标记（世界观会额外产出地图卡片）
            extra_cards = _parse_card_markers(content)
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
            # 【P0改进】卡片下发时附带自检结果，前端可展示
            card_meta = {'validation': validation_meta} if validation_meta else None
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
            max_tok = 6000 if dim_key == 'timeline' else 2000
            max_attempts = 4
            _EMPTY_FALLBACK_LEN = 30
            for _attempt in range(max_attempts):
                full = []
                _temp = 0.7 + min(_attempt * 0.08, 0.2)
                _max_tok = max_tok
                if _attempt == 1:
                    _max_tok = min(int(max_tok * 1.5), 8000)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), 8000)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                for chunk in gw.chat_stream(_msgs_call, temperature=_temp, max_tokens=_max_tok):
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
                if not validator.should_retry(issues) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                retry_hint = validator.build_retry_hint(issues)
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
                    max_tok = 6000 if _is_tl else 1500
                    # 自检重试循环（首次 + 最多 3 次重试，think 剥离 + 客套检测 + 低阈值兜底）
                    max_attempts = 4
                    _EMPTY_FALLBACK_LEN = 30
                    for _attempt in range(max_attempts):
                        raw_chunks = []
                        # 初始温度 0.7（原 0.8 偏高），重试时微增
                        _temp = 0.7 + min(_attempt * 0.08, 0.2)
                        _max_tok = max_tok
                        if _attempt == 1:
                            _max_tok = min(int(max_tok * 1.5), 8000)
                        elif _attempt >= 2:
                            _max_tok = min(int(max_tok * 2), 8000)
                        _msgs_call = cur_messages
                        if _attempt >= 1:
                            _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key)
                        for chunk in gw.chat_stream(_msgs_call, temperature=_temp, max_tokens=_max_tok):
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
                        if not validator.should_retry(issues) or _attempt >= max_attempts - 1:
                            validation_meta = validator.to_meta(issues)
                            break
                        retry_hint = validator.build_retry_hint(issues)
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

{core_iron}

{skill_note}

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
                full = []
                # 去AI味温度保持偏低：0.5 起步（忠实原文），重试微增到 0.58/0.66，避免高温乱改
                _temp = 0.5 + min(_attempt * 0.08, 0.2)
                _max_tok = max_tok
                if _attempt == 1:
                    _max_tok = min(int(max_tok * 1.5), 8000)
                elif _attempt >= 2:
                    _max_tok = min(int(max_tok * 2), 8000)
                _msgs_call = cur_messages
                if _attempt >= 1:
                    _msgs_call = _downgrade_prompt_for_retry(cur_messages, keep_dim='chapter_deai')
                for chunk in gw.chat_stream(_msgs_call, temperature=_temp, max_tokens=_max_tok):
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

    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'review', mode='agent') or ''
    except Exception:
        pass

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

        skill_note = ''
        try:
            from app import _get_skill_prompts_by_category
            skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'review', mode='agent') or ''
        except Exception:
            pass

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
