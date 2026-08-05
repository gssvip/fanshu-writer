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
# Action Card 协议
# ============================================================================

# 卡片类型 → 目标维度字段 + 落地方式（append 覆盖/追加, character 走独立表）
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


def build_chat_system_prompt(book, bb, recent_chapters: list = None) -> str:
    """构建维度感知的聊天 system_prompt。

    注入当前书的相关 bible 维度 + 最近章节 + Action Card 使用说明 + 创作进度。
    总量控制在 ~4000 字以内，避免 token 爆炸。
    recent_chapters: 最近章节列表（dict: title/word_count/order_index），由 chat_smart 注入
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

    # 注入 bible 维度（每维度截断，控制总量）
    if bb:
        dims = [
            ('核心构思', 'concept', 500),
            ('世界观', 'worldbuilding', 800),
            ('核心规则', 'key_rules', 600),
            ('人物档案', 'character_profiles', 1000),
            ('大纲', 'plot_design', 800),
            ('剧情时间线', 'timeline', 500),
            ('伏笔', 'foreshadowing', 400),
            ('文风指南', 'style_guide', 300),
        ]
        filled = []
        empty = []
        for label, field, limit in dims:
            val = (getattr(bb, field, '') or '').strip()
            if val:
                parts.append(f'\n【已设定·{label}】\n{_smart_truncate(val, limit)}')
                filled.append(label)
            else:
                empty.append(label)

        if not filled:
            parts.append('\n【创作状态】这是一本新书，所有维度都还空白，需要从头讨论设定。')
        else:
            parts.append(f'\n【创作进度】已完成维度：{"、".join(filled)}')
            if empty:
                parts.append(f'待补充维度：{"、".join(empty)}（可引导作者讨论这些）')
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

注意：
- 卡片内容必须具体、可直接写入设定库，不要写"建议讨论XXX"这种空话
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

    # P4：加载最近 5 章标题（让 AI 懂作者正在写哪一章）
    recent_chapters = []
    try:
        recent = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .order_by(Chapter.order_index.desc()).limit(5).all()
        recent_chapters = [{'title': c.title, 'word_count': c.word_count or 0,
                            'order_index': c.order_index} for c in reversed(recent)]
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
    system_prompt = build_chat_system_prompt(book, bb, recent_chapters)
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

            # 持久化对话（剥离卡片标记后存历史）
            clean_text = strip_cards(complete)
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text})
            session.messages_json = json.dumps(history, ensure_ascii=False)
            session.updated_at = datetime.now(timezone.utc)
            db.session.commit()

            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@chat_collab_bp.route('/api/ai/chat/smart/apply-card', methods=['POST'])
def apply_card():
    """采纳 Action Card，落地到对应维度。"""
    from app import db, BookBible, Character
    data = request.json or {}
    book_id = data.get('book_id')
    card = data.get('card', {})
    ctype = card.get('type', '')
    content = (card.get('content') or '').strip()
    title = card.get('title', '')

    if ctype not in CARD_REGISTRY or not content:
        return jsonify({'error': '无效的卡片或内容为空'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    spec = CARD_REGISTRY[ctype]

    if spec['mode'] == 'character':
        # 人物卡走独立 Character 表：解析"姓名|身份|性格|背景"格式
        lines = content.split('\n')
        name = title or (lines[0].split('|')[0].strip() if lines else '未命名')
        # 尝试结构化解析
        parts = [p.strip() for p in content.replace('\n', '|').split('|') if p.strip()]
        char = Character(
            book_id=book_id,
            name=parts[0] if len(parts) > 0 else name,
            role='protagonist' if '主角' in title or '主角' in content else 'supporting',
            description=parts[1] if len(parts) > 1 else '',
            personality=parts[2] if len(parts) > 2 else '',
            background=parts[3] if len(parts) > 3 else content,
        )
        db.session.add(char)
        # 同时追加到 character_profiles 文本字段（兼容老逻辑）
        existing = (bb.character_profiles or '').strip()
        bb.character_profiles = f'{existing}\n\n【{title}】\n{content}'.strip() if existing else f'【{title}】\n{content}'
    else:
        field = spec['field']
        existing = (getattr(bb, field, '') or '').strip()
        entry = f'【{title}】\n{content}' if title else content
        setattr(bb, field, f'{existing}\n\n{entry}'.strip() if existing else entry)

    db.session.commit()
    return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                    'progress': build_progress_map(bb)})


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
