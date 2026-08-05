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
- SAVE_CHAPTER       章节正文（标题=章节名，内容=完整章节正文，可直接作为章节保存）

注意：
- 卡片内容必须具体、可直接写入设定库或作为正文保存，不要写"建议讨论XXX"这种空话
- SAVE_CHAPTER 仅在用户明确要求"写一章""接着写正文"时产出，内容必须是完整的章节正文（2000字以上）
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


@chat_collab_bp.route('/api/ai/chat/smart/apply-card', methods=['POST'])
def apply_card():
    """采纳 Action Card，落地到对应维度。

    SAVE_CHAPTER 模式：落地到 Chapter 表，作为新章节追加。
    返回 chapter_id 供前端跳转/确认。
    """
    from app import db, BookBible, Character, Chapter
    data = request.json or {}
    book_id = data.get('book_id')
    card = data.get('card', {})
    ctype = card.get('type', '')
    content = (card.get('content') or '').strip()
    title = card.get('title', '')

    if ctype not in CARD_REGISTRY or not content:
        return jsonify({'error': '无效的卡片或内容为空'}), 400

    spec = CARD_REGISTRY[ctype]
    result_extra = {}

    # 章节正文卡：落地到 Chapter 表
    if spec['mode'] == 'chapter':
        # 计算 order_index：当前书最大 order_index + 1
        max_idx = db.session.query(db.func.max(Chapter.order_index)) \
            .filter_by(book_id=book_id, is_volume=False).scalar() or 0
        # 计算字数（中英文混合估算）
        wc = len(content)
        ch = Chapter(
            book_id=book_id,
            title=title or f'第{max_idx + 1}章',
            content=content,
            order_index=max_idx + 1,
            word_count=wc,
            status='draft',
            is_volume=False,
            parent_id='',
        )
        db.session.add(ch)
        db.session.commit()
        result_extra = {
            'chapter_id': ch.id,
            'chapter_title': ch.title,
            'word_count': wc,
            'order_index': ch.order_index,
        }
        # bible 可能不存在，但 progress 仍要返回
        bb = BookBible.query.filter_by(book_id=book_id).first()
        return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                        'progress': build_progress_map(bb),
                        **result_extra})

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

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
                    'progress': build_progress_map(bb),
                    **result_extra})


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
                                           target_chapter_num, prev_chapter_content, mode='continue')
            elif action == 'polish':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='polish')
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
            ctx_parts.append(f'【{_DIM_LABELS.get(k, k)}】\n{v[:600]}')
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
        yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话
    yield from _persist_action_session(session, f'批量生成设定：{instruction or "默认五维度"}',
                                       generated, dims)


def _action_chapter(book, session, instruction, gw, sse, target_chapter_num, prev_chapter_content, mode):
    """续写/润色本章正文：产 SAVE_CHAPTER 卡。"""
    from app import db, BookBible, Chapter
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # 确定当前章号 + 上一章内容
    if not target_chapter_num:
        max_idx = db.session.query(db.func.max(Chapter.order_index)) \
            .filter_by(book_id=book_id, is_volume=False).scalar() or 0
        target_chapter_num = max_idx + 1
    if not prev_chapter_content:
        prev = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .filter(Chapter.order_index < target_chapter_num) \
            .order_by(Chapter.order_index.desc()).first()
        prev_chapter_content = (prev.content or '')[:2000] if prev else ''

    # bible 上下文摘要
    bible_ctx = ''
    if bb:
        parts = []
        for f in ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design']:
            v = (getattr(bb, f, '') or '').strip()
            if v:
                parts.append(f'【{_DIM_LABELS.get(f, f)}】\n{v[:400]}')
        bible_ctx = '\n\n'.join(parts)

    mode_label = '续写' if mode == 'continue' else '润色'
    yield sse({'type': 'delta', 'content': f'正在{mode_label}第 {target_chapter_num} 章…\n\n'})

    if mode == 'polish':
        # 润色：需要有原文
        cur = Chapter.query.filter_by(book_id=book_id, is_volume=False,
                                       order_index=target_chapter_num).first()
        if not cur or not (cur.content or '').strip():
            yield sse({'type': 'error', 'error': f'第 {target_chapter_num} 章无正文，无法润色'})
            return
        sys_prompt = (
            f'你是资深网文润色编辑。请润色《{book.title}》第 {target_chapter_num} 章正文。'
            f'\n要求：保持剧情和人物不变，优化文笔节奏，去除AI味，提升画面感。'
            f'\n用户要求：{instruction or "无"}'
            f'\n\n【原文】\n{cur.content}'
            f'\n\n请直接输出润色后的完整正文（含章节标题），不要解释。'
        )
        user_msg = f'请润色第 {target_chapter_num} 章'
    else:
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》续写第 {target_chapter_num} 章正文。'
            f'\n题材：{book.genre or "未指定"}，类型：{book.book_type or "未指定"}。'
            f'\n\n【设定参考】\n{bible_ctx or "（暂无设定）"}'
            f'\n\n【上一章结尾】\n{prev_chapter_content or "（第一章）"}'
            f'\n用户要求：{instruction or "自然推进剧情"}'
            f'\n请直接输出完整章节正文（含标题，2400字左右），不要解释。'
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

    card = {
        'id': str(uuid.uuid4())[:8],
        'type': 'SAVE_CHAPTER',
        'title': f'第 {target_chapter_num} 章（AI{mode_label}）',
        'content': content.strip(),
        'target': '章节正文',
    }
    yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话
    yield from _persist_action_session(session, f'{mode_label}第{target_chapter_num}章：{instruction or ""}',
                                       {f'chapter_{target_chapter_num}': content},
                                       [f'chapter_{target_chapter_num}'])


def _persist_action_session(session, title, generated, dims):
    """动作执行完后持久化会话消息。"""
    from app import db
    history = load_session_messages(session)
    history.append({'role': 'user', 'content': title})
    # 动作产出的卡片也存进会话，便于历史回看
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
    {'key': 'worldbuilding',      'label': '世界观',     'field': 'worldbuilding',      'card': 'SAVE_WORLDSETTING', 'icon': '🌍', 'hint': '故事发生的世界，独特规则或设定'},
    {'key': 'plot_design',        'label': '大纲',       'field': 'plot_design',        'card': 'SAVE_OUTLINE_NODE', 'icon': '📋', 'hint': '主线走向，三幕式或起承转合'},
    {'key': 'timeline',           'label': '剧情',       'field': 'timeline',           'card': 'SAVE_PLOT',         'icon': '📖', 'hint': '关键剧情节点的时间顺序'},
    {'key': 'character_profiles', 'label': '人物及关系', 'field': 'character_profiles', 'card': 'SAVE_CHARACTER',    'icon': '👤', 'hint': '主角和核心配角的动机、性格、关系网'},
    {'key': 'foreshadowing',      'label': '伏笔',       'field': 'foreshadowing',      'card': 'SAVE_FORESHADOW',   'icon': '🔮', 'hint': '长线伏笔的埋设与回收计划'},
    {'key': 'locations',          'label': '地图',       'field': 'locations',          'card': 'SAVE_LOCATION',     'icon': '🗺️', 'hint': '故事中的地点、势力分布'},
]

_DIM_KEY_TO_SPEC = {d['key']: d for d in SMART_DIMENSIONS}


def _build_dim_context(book, bb, dim_key, with_self=True, self_limit=800, other_limit=400):
    """构建指定维度的上下文：其他已填维度作为参考 + 当前维度已有内容。"""
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
                parts.append(f'【{d["label"]}】\n{_smart_truncate(v, other_limit)}')
    ctx = '\n\n'.join(parts)
    self_content = ''
    if bb and with_self:
        self_content = (getattr(bb, target['field'], '') or '').strip()
        if self_content:
            self_content = _smart_truncate(self_content, self_limit)
    return ctx, self_content


@chat_collab_bp.route('/api/ai/smart/dimensions', methods=['GET'])
def smart_dimensions():
    """返回 AI 智驾支持的维度列表（供前端渲染子按钮）。"""
    return jsonify({'dimensions': SMART_DIMENSIONS})


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

    # 注入构思类技能包
    skill_note = ''
    try:
        from app import _get_skill_prompts_by_category
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', mode='single') or ''
    except Exception:
        pass

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》的「{spec['label']}」维度生成 3-5 个差异化的创意方案供作者选择。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

【已有设定参考】
{ctx or "（暂无）"}

{("【当前维度已有内容（可在其基础上补充完善）】\n" + self_content) if self_content else ""}

【作者需求】
{requirement or f"请帮我生成{spec['label']}的设定"}

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

    sys_prompt = f"""你是资深网文创作智驾。请为《{book.title or "未命名"}》生成「{spec['label']}」维度的完整设定内容。

题材：{book.genre or "未指定"}
类型：{book.book_type or "未指定"}

【已有设定参考】
{ctx or "（暂无）"}

{("【当前维度已有内容（可在此基础上完善，不要简单重复）】\n" + self_content) if self_content else ""}

【作者需求】
{requirement or "无"}

【选中方案】
{suggestion}

{("【技能包指引】\n" + skill_note) if skill_note else ""}

请直接输出该维度的完整设定内容（300-800字），不要寒暄，不要解释，不要加 Markdown 标题。"""

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
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': spec['card'],
                'title': f'{spec["label"]}（AI智驾生成）',
                'content': content,
                'target': _CARD_TARGET.get(spec['card'], spec['label']),
            }
            yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'生成{spec["label"]}：{requirement or suggestion[:50]}'})
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

    sys_prompt = f"""你是资深网文创作智驾。请根据作者的修改意见，修订《{book.title or "未命名"}》的「{spec['label']}」维度内容。

【其他维度参考】
{ctx or "（暂无）"}

【当前维度原文】
{current_content or "（暂无）"}

【作者修改意见】
{edit_request}

{("【技能包指引】\n" + skill_note) if skill_note else ""}

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
    """
    from app import Chapter
    book_id = request.args.get('book_id')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    latest = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
        .order_by(Chapter.order_index.desc()).first()
    if latest:
        return jsonify({
            'latest': {
                'id': latest.id,
                'title': latest.title,
                'order_index': latest.order_index,
                'word_count': latest.word_count or 0,
                'status': latest.status,
            },
            'next_chapter_num': latest.order_index + 1,
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

    # 无技能包时使用默认去AI味规则
    if not skill_note:
        skill_note = """【默认去AI味规则】
【必删清单】一股、一抹、不由得、不禁、随即、旋即、仿佛、似乎、似乎在、缓缓、微微、淡淡、轻轻、静静地、默默地、不知不觉、若有所思、若有所悟
【人味注入】加入不完美细节（结巴/重复/打断）、感官碎片、口语化表达、删除冗余形容词
【硬性约束】保留原章节剧情走向和钩子，只改文风不改剧情，字数与原文相近"""

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
        for f in ['character_profiles', 'key_rules', 'worldbuilding']:
            v = (getattr(bb, f, '') or '').strip()
            if v:
                parts.append(f'【{_DIM_LABELS.get(f, f)}】\n{v[:300]}')
        bible_ctx = '\n\n'.join(parts)

    sys_prompt = f"""你是番茄去AI味审查员。请对以下章节正文做去AI味审校，按规则修改后只输出修改后的正文。

{skill_note}

【设定参考】
{bible_ctx or "（暂无）"}

【优先级铁律】人味>克制>流畅。
【硬性约束】修改后字数与原文相近（±10%），保留原章节的剧情走向和钩子，只改文风不改剧情。

请直接输出修改后的完整正文（含章节标题），不要解释，不要加 Markdown 代码块。"""

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
            card = {
                'id': str(uuid.uuid4())[:8],
                'type': 'SAVE_CHAPTER',
                'title': chapter.title,
                'content': content,
                'target': '章节正文',
            }
            yield sse({'type': 'card', 'card': card, 'session_id': session_id,
                       'meta': {'chapter_id': chapter_id, 'replace': True}})
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': f'去AI味：{chapter.title}'})
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
            with current_app.test_request_context(
                f'/api/books/{book_id}/ai-anti-forget-check',
                method='POST',
                json={'scope': 'reports', 'volume_ids': volume_ids, 'skill_pack_ids': skill_pack_ids}
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
    """
    from app import Chapter
    book_id = request.args.get('book_id')
    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
        .order_by(Chapter.order_index.asc()).all()
    return jsonify({'chapters': [
        {'id': c.id, 'title': c.title, 'order_index': c.order_index,
         'word_count': c.word_count or 0, 'status': c.status}
        for c in chs
    ]})


@chat_collab_bp.route('/api/ai/smart/chapter-replace', methods=['POST'])
def smart_chapter_replace():
    """用去AI味后的内容替换原章节正文（落地）。

    body: { book_id, chapter_id, content }
    返回: { ok, chapter_id, word_count }
    """
    from app import db, Chapter
    data = request.json or {}
    book_id = data.get('book_id')
    chapter_id = data.get('chapter_id')
    content = (data.get('content') or '').strip()

    if not book_id or not chapter_id or not content:
        return jsonify({'error': '参数无效'}), 400

    chapter = Chapter.query.get(chapter_id)
    if not chapter or chapter.book_id != book_id:
        return jsonify({'error': '章节不存在'}), 404

    chapter.content = content
    chapter.word_count = len(content)
    chapter.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'ok': True, 'chapter_id': chapter_id, 'word_count': chapter.word_count})
