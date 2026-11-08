"""资料库 Blueprint（协同创作链路资产沉淀层）。

工具页「资料库」Tab 的后端，承接 榜单风向 → 拆书分析 → 拆书模板 → 小说创意 → 智驾生成
工作流中各环节的产出物：

1. 拆书档案（book_analyses）：拆书分析结果落库，不再"看完即丢"
2. 拆书模板（templates + source_json）：拆书套路一键生成结构模板，带来源溯源
3. 创意工坊（idea_cards）：基于拆书模板的差异化再创作（防抄袭：换金手指/人设/世界观/钩子，
   只沿用节拍结构），生成时输出差异清单自检
4. 创作包一键建书：创意卡 → 新作品（模板骨架 + 差异清单注入 BookBible，provenance 入
   metadata.concept_pack），供智驾全程感知创作来源

使用方：Fanshu 工具页「资料库」Tab、拆书分析 Tab「生成拆书模板」按钮。
"""
from __future__ import annotations

import json
import re

from flask import Blueprint, jsonify, request

from auth_utils import login_required

library_bp = Blueprint('library', __name__)


# ---------------------------------------------------------------------------
# 拆书档案（book_analyses）
# ---------------------------------------------------------------------------

def _summary_from_result(result: dict) -> dict:
    return {
        'logline': (result.get('logline') or '')[:200],
        'genre_tags': result.get('genre_tags') or [],
        'positioning': (result.get('positioning') or '')[:200],
    }


def _collect_titles(obj, out, depth=0):
    """防御式从拆书 JSON 中抽取节拍/章节标题（字段结构可能随 schema 演进）。"""
    if len(out) >= 24 or depth > 4:
        return
    if isinstance(obj, str):
        t = obj.strip().split('\n')[0][:40]
        if t:
            out.append(t)
    elif isinstance(obj, dict):
        for key in ('title', 'name', 'beat', 'chapter', 'label', 'point', 'function'):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                out.append(v.strip().split('\n')[0][:40])
                return
        for v in obj.values():
            _collect_titles(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_titles(v, out, depth + 1)


def _text_from(value) -> str:
    """任意拆书字段 → 可读文本（list/dict 递归展开）。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=1)
    except Exception:
        return str(value)


@library_bp.route('/api/library/analyses', methods=['GET'])
@login_required
def list_analyses():
    from app import BookAnalysis
    rows = (BookAnalysis.query.filter_by(user_id=request.current_user_id)
            .order_by(BookAnalysis.created_at.desc()).all())
    return jsonify([a.to_dict() for a in rows])


@library_bp.route('/api/library/analyses', methods=['POST'])
@login_required
def save_analysis():
    from app import db, BookAnalysis
    data = request.json or {}
    result = data.get('result') or {}
    if not isinstance(result, dict) or not result:
        return jsonify({'error': '拆书结果为空，无法存档'}), 400
    row = BookAnalysis(
        user_id=request.current_user_id,
        title=(data.get('title') or '拆书档案')[:200],
        source_label=(data.get('source_label') or '')[:300],
        platform=(data.get('platform') or '')[:50],
        mode=(data.get('mode') or 'competitor')[:20],
        result_json=json.dumps(result, ensure_ascii=False),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@library_bp.route('/api/library/analyses/<analysis_id>', methods=['GET'])
@login_required
def get_analysis(analysis_id):
    from app import BookAnalysis
    row = BookAnalysis.query.filter_by(id=analysis_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': '拆书档案不存在'}), 404
    return jsonify(row.to_dict(include_result=True))


@library_bp.route('/api/library/analyses/<analysis_id>', methods=['DELETE'])
@login_required
def delete_analysis(analysis_id):
    from app import db, BookAnalysis
    row = BookAnalysis.query.filter_by(id=analysis_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': '拆书档案不存在'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 拆书档案 → 拆书模板（结构骨架落库，只留套路不留原文）
# ---------------------------------------------------------------------------

@library_bp.route('/api/library/analyses/<analysis_id>/to-template', methods=['POST'])
@login_required
def analysis_to_template(analysis_id):
    from app import db, Template, BookAnalysis
    row = BookAnalysis.query.filter_by(id=analysis_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': '拆书档案不存在'}), 404
    result = json.loads(row.result_json or '{}')
    data = request.json or {}
    name = (data.get('name') or f'{row.title or "拆书"}·拆书模板').strip()[:200]

    # 结构骨架：黄金三章 + 节拍表 → 章节节点（仅标题/功能，不含对标书原文）
    titles: list[str] = []
    for key in ('golden_three', 'plot_beats', 'opening_patterns'):
        _collect_titles(result.get(key), titles)
    seen, structure = set(), []
    for i, t in enumerate(titles):
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        structure.append({'title': t, 'is_volume': False, 'parent_id': '', 'order_index': len(structure)})

    # 提示词：可复制套路 / 可学点 / 钩子体系 / 节奏（再创作与智驾生成时注入）
    prompts = {}
    for src_key, dst_key in (
        ('replicable_template', 'replicable_template'),
        ('learnable_points', 'learnable_points'),
        ('hook_system', 'hook_system'),
        ('pacing', 'pacing'),
        ('golden_lines', 'golden_lines'),
        ('satisfaction_points', 'satisfaction_points'),
        ('copy_plan', 'copy_plan'),
    ):
        txt = _text_from(result.get(src_key))
        if txt:
            prompts[dst_key] = txt

    template = Template(
        name=name,
        description=f"由拆书档案「{row.title or '未命名'}」生成的结构骨架模板（只含节拍套路，不含原文表达）",
        genre=data.get('genre') or 'other',
        book_type='novel',
        structure_json=json.dumps(structure, ensure_ascii=False),
        prompts_json=json.dumps(prompts, ensure_ascii=False),
        source_json=json.dumps({
            'type': 'teardown',
            'analysis_id': row.id,
            'book_title': row.title,
            'source_label': row.source_label,
            'platform': row.platform,
        }, ensure_ascii=False),
    )
    db.session.add(template)
    db.session.commit()
    return jsonify(template.to_dict()), 201


# ---------------------------------------------------------------------------
# 创意工坊（idea_cards）：基于拆书模板的差异化再创作
# ---------------------------------------------------------------------------

_IDEA_SYSTEM_PROMPT = """你是资深网文创意策划师，服务于番茄小说/起点中文网。
用户正在基于一份「拆书模板」（从对标书提炼的结构骨架）再创作一个全新的小说创意。

【防抄袭红线·最高优先级】
1. 只允许沿用模板的节拍结构/节奏骨架，绝不允许复刻对标书的具体表达。
2. 必须更换以下要素：金手指/能力体系、主角人设与人物关系、世界观具体设定、开篇钩子、核心事件设计。
3. 对标书的书名、人名、专有名词、原文句子一律不得出现在你的输出中。
4. 你必须在 difference_check 中逐项说明与对标书的差异；若某项无法形成差异，主动调整创意直到形成差异。

严格按 JSON 格式输出，不要任何其他文字：
{
  "title": "新书名（不要与对标书雷同）",
  "idea": {
    "logline": "一句话故事核（50字内）",
    "selling_points": ["核心卖点1", "核心卖点2", "核心卖点3"],
    "worldview": "世界观与金手指设定（100字内，必须与对标书不同）",
    "conflict": "核心冲突与主线走向（100字内）",
    "characters": [{"name": "主角名", "role": "身份", "desc": "人设（必须与对标书不同）"}],
    "opening_hook": "开篇钩子设计（80字内）",
    "outline_sketch": "前5章骨架走向（每章一行，只写事件功能不写细节）"
  },
  "difference_check": [
    {"aspect": "金手指", "benchmark": "对标书常见做法", "ours": "我们的差异点", "passed": true},
    {"aspect": "人设", "benchmark": "...", "ours": "...", "passed": true},
    {"aspect": "世界观", "benchmark": "...", "ours": "...", "passed": true},
    {"aspect": "开篇钩子", "benchmark": "...", "ours": "...", "passed": true}
  ],
  "plagiarism_risk": "low",
  "risk_note": "风险说明（如有）"
}"""


@library_bp.route('/api/library/ideas/generate', methods=['POST'])
@login_required
def generate_idea():
    from app import (Template, BookAnalysis, _call_llm)

    data = request.json or {}
    template_id = (data.get('template_id') or '').strip()
    analysis_id = (data.get('analysis_id') or '').strip()
    brief = (data.get('brief') or '').strip()

    template = Template.query.get(template_id) if template_id else None
    if not template:
        return jsonify({'error': '请先选择一个拆书模板'}), 400

    analysis = None
    if analysis_id:
        analysis = BookAnalysis.query.filter_by(id=analysis_id, user_id=request.current_user_id).first()
    source_label = ''
    analysis_ctx = ''
    if analysis:
        result = json.loads(analysis.result_json or '{}')
        source_label = analysis.source_label or analysis.title or ''
        parts = [f'对标书：《{analysis.title or "未命名"}》']
        for key, label in (('logline', '对标故事核'), ('core_conflict', '对标核心冲突'),
                           ('golden_finger', '对标金手指'), ('market_position', '市场定位'),
                           ('strengths', '对标优势'), ('weaknesses', '对标短板')):
            txt = _text_from(result.get(key))
            if txt:
                parts.append(f'{label}：{txt[:600]}')
        chars = result.get('characters')
        if chars:
            parts.append(f'对标人物（仅供避开）：{_text_from(chars)[:400]}')
        analysis_ctx = '\n'.join(parts)

    structure_titles = [s.get('title', '') for s in (template.structure or []) if isinstance(s, dict)]
    tpl_prompts = template.prompts or {}
    template_ctx = '\n'.join([
        f'【结构骨架】（{len(structure_titles)} 个节拍节点）：' + ' / '.join(structure_titles[:24]),
        f'【可复制套路】{str(tpl_prompts.get("replicable_template", ""))[:600]}',
        f'【可学点】{str(tpl_prompts.get("learnable_points", ""))[:400]}',
    ])

    system_prompt = f"""{_IDEA_SYSTEM_PROMPT}

{f'【对标书信息】（仅供差异化参照，禁止复刻）\n{analysis_ctx}' if analysis_ctx else '（未关联拆书档案，请在 difference_check 中以同题材常见套路为基准说明差异）'}

【拆书模板（可沿用的结构骨架）】
{template_ctx}

{f'【用户补充方向】{brief}' if brief else ''}"""

    messages = [
        {'role': 'system', 'content': system_prompt.strip()},
        {'role': 'user', 'content': '请基于拆书模板的结构骨架，再创作一个通过防抄袭红线的全新小说创意。'},
    ]
    content, error = _call_llm(messages, max_tokens=4000, temperature=0.9,
                               task_type='creation', scene_label='library_idea_generate')
    if error or not content:
        return jsonify({'error': f'AI 生成失败：{error or "空响应"}'}), 500

    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', content.strip(), flags=re.M)
    try:
        payload = json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            return jsonify({'error': 'AI 输出不是合法 JSON，请重试'}), 500
        try:
            payload = json.loads(m.group(0))
        except Exception:
            return jsonify({'error': 'AI 输出 JSON 解析失败，请重试'}), 500

    return jsonify({
        'title': payload.get('title') or '未命名创意',
        'idea': payload.get('idea') or {},
        'diff': payload.get('difference_check') or [],
        'plagiarism_risk': payload.get('plagiarism_risk') or 'unknown',
        'risk_note': payload.get('risk_note') or '',
        'source_label': source_label,
    })


@library_bp.route('/api/library/ideas', methods=['GET'])
@login_required
def list_ideas():
    from app import IdeaCard
    rows = (IdeaCard.query.filter_by(user_id=request.current_user_id)
            .order_by(IdeaCard.created_at.desc()).all())
    return jsonify([i.to_dict() for i in rows])


@library_bp.route('/api/library/ideas', methods=['POST'])
@login_required
def save_idea():
    from app import db, IdeaCard
    data = request.json or {}
    idea = data.get('idea') or {}
    if not isinstance(idea, dict) or not idea:
        return jsonify({'error': '创意内容为空'}), 400
    row = IdeaCard(
        user_id=request.current_user_id,
        title=(data.get('title') or '未命名创意')[:200],
        idea_json=json.dumps(idea, ensure_ascii=False),
        diff_json=json.dumps(data.get('diff') or [], ensure_ascii=False),
        template_id=(data.get('template_id') or '')[:36],
        analysis_id=(data.get('analysis_id') or '')[:36],
        source_label=(data.get('source_label') or '')[:300],
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@library_bp.route('/api/library/ideas/<idea_id>', methods=['DELETE'])
@login_required
def delete_idea(idea_id):
    from app import db, IdeaCard
    row = IdeaCard.query.filter_by(id=idea_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': '创意卡不存在'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 创作包一键建书（创意卡 → 新作品 → 智驾）
# ---------------------------------------------------------------------------

@library_bp.route('/api/library/ideas/<idea_id>/create-book', methods=['POST'])
@login_required
def create_book_from_idea(idea_id):
    from app import db, Book, BookBible, Template, IdeaCard

    row = IdeaCard.query.filter_by(id=idea_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': '创意卡不存在'}), 404

    idea = json.loads(row.idea_json or '{}')
    diff = json.loads(row.diff_json or '[]')
    template = Template.query.get(row.template_id) if row.template_id else None
    structure_titles = [s.get('title', '') for s in (template.structure or []) if isinstance(s, dict)] if template else []
    tpl_name = template.name if template else ''

    logline = (idea.get('logline') or row.title or '未命名创意')
    selling = idea.get('selling_points') or []

    concept_text = '\n'.join([
        f'【故事核】{logline}',
        f'【核心卖点】{"；".join(str(s) for s in selling) if selling else "（待补充）"}',
        f'【世界观/金手指】{idea.get("worldview") or "（待补充）"}',
        f'【核心冲突】{idea.get("conflict") or "（待补充）"}',
        f'【开篇钩子】{idea.get("opening_hook") or "（待补充）"}',
    ])
    diff_text = '\n'.join(
        f'- {d.get("aspect", "维度")}：{d.get("ours", "")}（对标做法：{d.get("benchmark", "")}）'
        for d in diff if isinstance(d, dict)
    )
    plot_text = '\n'.join(filter(None, [
        f'【结构骨架】（来自拆书模板「{tpl_name}」，写作时沿用节拍节奏）：\n' + ' / '.join(structure_titles[:24]) if structure_titles else '',
        f'【前5章骨架】\n{idea.get("outline_sketch") or ""}',
        f'【差异化约束·防抄袭红线】以下要素已被明确要求与对标书不同，正文生成时严禁回退到对标书做法：\n{diff_text}' if diff_text else '',
    ]))

    # provenance：完整溯源链入 metadata，供智驾上下文注入与后续追溯
    concept_pack = {
        'idea_id': row.id,
        'idea_title': row.title,
        'source_label': row.source_label,
        'template_id': row.template_id,
        'template_name': tpl_name,
        'analysis_id': row.analysis_id,
        'diff': diff,
        'created_from': 'idea_card',
    }

    book = Book(
        user_id=request.current_user_id,
        title=row.title or '未命名创意',
        genre=(template.genre if template else 'other'),
        book_type='novel',
        synopsis=logline,
        template_id=row.template_id or '',
        metadata_json=json.dumps({'concept_pack': concept_pack}, ensure_ascii=False),
    )
    db.session.add(book)
    db.session.flush()

    bb = BookBible(book_id=book.id, concept=concept_text, plot_design=plot_text,
                   generated_summary=f'本作品由拆书模板「{tpl_name}」+ 差异化创意生成。' if tpl_name else '')
    db.session.add(bb)

    row.book_id = book.id
    db.session.commit()
    return jsonify({'book_id': book.id, 'book': book.to_dict()}), 201


# ---------------------------------------------------------------------------
# 编辑（拆书档案 / 创意卡）
# ---------------------------------------------------------------------------

@library_bp.route('/api/library/analyses/<analysis_id>', methods=['PUT'])
@login_required
def update_analysis(analysis_id):
    from app import db, BookAnalysis
    row = BookAnalysis.query.filter_by(id=analysis_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'title' in data:
        row.title = (data.get('title') or '').strip()[:200]
    if 'source_label' in data:
        row.source_label = (data.get('source_label') or '').strip()[:300]
    if 'content' in data:
        try:
            result = json.loads(row.result_json or '{}')
        except Exception:
            result = {}
        result['raw'] = data.get('content') or ''
        row.result_json = json.dumps(result, ensure_ascii=False)
        row.summary_json = json.dumps(_summary_from_result(result), ensure_ascii=False)
    db.session.commit()
    return jsonify(row.to_dict())


@library_bp.route('/api/library/ideas/<idea_id>', methods=['PUT'])
@login_required
def update_idea(idea_id):
    from app import db, IdeaCard
    row = IdeaCard.query.filter_by(id=idea_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'title' in data:
        row.title = (data.get('title') or '').strip()[:200]
    if isinstance(data.get('idea'), dict):
        try:
            idea = json.loads(row.idea_json or '{}')
        except Exception:
            idea = {}
        idea.update(data['idea'])
        row.idea_json = json.dumps(idea, ensure_ascii=False)
    db.session.commit()
    return jsonify(row.to_dict())


# ---------------------------------------------------------------------------
# 素材笔记（library_notes）
# ---------------------------------------------------------------------------

@library_bp.route('/api/library/notes', methods=['GET'])
@login_required
def list_notes():
    from app import LibraryNote
    rows = LibraryNote.query.filter_by(user_id=request.current_user_id) \
        .order_by(LibraryNote.updated_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@library_bp.route('/api/library/notes', methods=['POST'])
@login_required
def create_note():
    from app import db, LibraryNote
    data = request.json or {}
    row = LibraryNote(
        user_id=request.current_user_id,
        title=(data.get('title') or '').strip()[:200],
        content=data.get('content') or '',
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@library_bp.route('/api/library/notes/<note_id>', methods=['PUT'])
@login_required
def update_note(note_id):
    from app import db, LibraryNote
    row = LibraryNote.query.filter_by(id=note_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'title' in data:
        row.title = (data.get('title') or '').strip()[:200]
    if 'content' in data:
        row.content = data.get('content') or ''
    db.session.commit()
    return jsonify(row.to_dict())


@library_bp.route('/api/library/notes/<note_id>', methods=['DELETE'])
@login_required
def delete_note(note_id):
    from app import db, LibraryNote
    row = LibraryNote.query.filter_by(id=note_id, user_id=request.current_user_id).first()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 加载到智驾：把资料库条目作为一条消息追加进所选作品的智驾会话
# ---------------------------------------------------------------------------

@library_bp.route('/api/library/load-to-chat', methods=['POST'])
@login_required
def load_to_chat():
    from datetime import datetime, timezone

    from auth_utils import get_owned_book
    from app import db, AISession
    from session_persist import _archive_replace_all

    data = request.json or {}
    book_id = (data.get('book_id') or '').strip()
    content = (data.get('content') or '').strip()
    title = (data.get('title') or '').strip()
    if not book_id or not content:
        return jsonify({'error': '缺少 book_id 或内容'}), 400
    if get_owned_book(book_id) is None:
        return jsonify({'error': 'Book not found'}), 404

    # 优先续用该作品最近的智驾会话（smart_setting），否则 general，再没有就新建
    session = (AISession.query.filter_by(book_id=book_id, scope='smart_setting')
               .order_by(AISession.updated_at.desc()).first()
               or AISession.query.filter_by(book_id=book_id, scope='general')
               .order_by(AISession.updated_at.desc()).first())
    if not session:
        session = AISession(book_id=book_id, scope='smart_setting',
                            user_id=request.current_user_id, title=title[:30] or '智驾')
        db.session.add(session)
        db.session.flush()

    try:
        msgs = json.loads(session.messages_json or '[]')
        if not isinstance(msgs, list):
            msgs = []
    except Exception:
        msgs = []
    msgs.append({'role': 'user', 'content': content})
    _archive_replace_all(session.id, msgs)
    session.messages_json = json.dumps(msgs, ensure_ascii=False)
    session.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'session_id': session.id})
