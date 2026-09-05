"""作品/章节/人物/大纲 CRUD 蓝图 — 从 app.py 拆出（巨石外迁第 4 批：books 域）。

路由清单（24 个）：
  /api/books CRUD（GET/POST/PUT/DELETE）
  /api/books/<id>/chapters CRUD + reorder + ghost-suggest + rebin-volumes
  /api/books/<id>/chapters/<cid>/versions（列表/恢复）
  /api/books/<id>/characters CRUD
  /api/books/<id>/outlines CRUD
  /api/books/<id>/stats（GET/POST 刷新）

依赖方向（无循环）：
  - 顶层仅依赖 flask + auth_utils + 标准库；
  - 模型与跨域 helper（count_words/update_book_stats/...）在每个路由函数体内
    延迟导入，请求期 app 早已加载完毕（与 general_chat.py 同款模式）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from auth_utils import login_required, VIP_LIFETIME_PRICE

books_bp = Blueprint('books', __name__)


@books_bp.route('/api/books', methods=['GET'])
@login_required
def list_books():
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    books = Book.query.filter_by(user_id=request.current_user_id).order_by(Book.updated_at.desc()).all()
    return jsonify([b.to_dict() for b in books])

@books_bp.route('/api/books/<book_id>', methods=['GET'])
def get_book(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    return jsonify(book.to_dict())

@books_bp.route('/api/books', methods=['POST'])
@login_required
def create_book():
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    data = request.json
    user_id = request.current_user_id
    # ---- 创作数量限制（新用户/存量/VIP 分档）----
    # 规则：
    #   1) is_vip=True → 无限
    #   2) 否则：如果当前 books 数量 >= 1，就需要判断是否"grandfathered"
    #      —— grandfathered = 用户改造规则之前已经有多本书（book_count > 1），则保持无限
    #      —— book_count == 1 的普通用户：只能再建 0 本 → 提示开通永久会员
    user = User.query.get(user_id)
    if user is not None and not bool(getattr(user, 'is_vip', False)):
        book_count = Book.query.filter_by(user_id=user_id).count()
        # grandfathered: 之前已经有 >1 本的用户不受影响
        # 普通用户（book_count == 0 或 1）：超过 1 本就触发升级会员
        if book_count >= 1:
            # 只有 grandfathered（book_count > 1）允许；book_count == 1 不允许创建第 2 本
            if book_count > 1:
                pass  # grandfathered → 放行
            else:
                return jsonify({
                    'code': 'UPGRADE_REQUIRED',
                    'message': '开通网站永久会员即可无限创建新书',
                    'vip_price': VIP_LIFETIME_PRICE,
                    'vip_tier': 'lifetime',
                }), 402
    # 总卷数：长篇默认10，短篇默认1。卷数不设上限，由用户自行决定（不钳制）
    book_type = data.get('book_type', 'novel')
    default_vols = 1 if book_type == 'short_story' else 10
    total_volumes = data.get('total_volumes') or default_vols
    # 卷数校验：仅校验下限≥1，不设上限（用户填多少就是多少）
    try:
        total_volumes = int(total_volumes)
        total_volumes = max(1, total_volumes)
    except (ValueError, TypeError):
        total_volumes = default_vols
    # 风格流派：JSON 数组，最多3种
    novel_styles = data.get('novel_styles', [])
    if isinstance(novel_styles, list):
        novel_styles = novel_styles[:3]
    else:
        novel_styles = []
    book = Book(
        user_id=request.current_user_id,
        title=data.get('title', '新书'),
        author=data.get('author', ''),
        genre=data.get('genre', 'other'),
        book_type=book_type,
        synopsis=data.get('synopsis', ''),
        template_id=data.get('template_id', ''),
        target_words=data.get('target_words', 0),
        total_volumes=total_volumes,
        novel_styles=json.dumps(novel_styles, ensure_ascii=False),
        status='draft'
    )
    db.session.add(book)
    db.session.flush()

    if data.get('template_id'):
        template = Template.query.get(data['template_id'])
        if template and template.structure_json:
            structure = json.loads(template.structure_json)
            for i, s in enumerate(structure):
                ch = Chapter(
                    book_id=book.id, title=s.get('title', f'章节{i+1}'),
                    order_index=i, is_volume=s.get('is_volume', False),
                    parent_id=s.get('parent_id', '')
                )
                db.session.add(ch)
    db.session.commit()
    return jsonify(book.to_dict()), 201

@books_bp.route('/api/books/<book_id>', methods=['PUT'])
def update_book(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    data = request.json
    for field in ['title', 'author', 'genre', 'book_type', 'synopsis', 'status', 'target_words', 'metadata_json']:
        if field in data:
            if field == 'metadata_json' and isinstance(data[field], dict):
                setattr(book, field, json.dumps(data[field], ensure_ascii=False))
            else:
                setattr(book, field, data[field])
    # 总卷数 + 风格流派同步
    if 'total_volumes' in data:
        try:
            tv = int(data['total_volumes'])
            tv = max(1, tv)  # 仅校验下限≥1，不设上限
            book.total_volumes = tv
        except (ValueError, TypeError):
            pass
    if 'novel_styles' in data:
        ns = data['novel_styles']
        if isinstance(ns, list):
            ns = ns[:3]
        else:
            ns = []
        book.novel_styles = json.dumps(ns, ensure_ascii=False)
    # 同步到 BookBible（创作时从 bible 读取注入）
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if bb:
        if hasattr(book, 'total_volumes') and book.total_volumes:
            bb.total_volumes = book.total_volumes
        if hasattr(book, 'novel_styles'):
            bb.novel_styles = book.novel_styles
    db.session.commit()
    return jsonify(book.to_dict())

@books_bp.route('/api/books/<book_id>', methods=['DELETE'])
def delete_book(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    # 手动删除所有关联记录（兼容 PostgreSQL 外键约束 + SQLite）
    ChapterVersion.query.filter(ChapterVersion.chapter_id.in_(
        db.session.query(Chapter.id).filter_by(book_id=book_id)
    )).delete(synchronize_session=False)
    Chapter.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    Character.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    Outline.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DailyStats.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    AISession.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    StageContent.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    BookBible.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DynamicMemory.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DynamicReport.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    db.session.delete(book)
    db.session.commit()
    return jsonify({'success': True})

# ==== Chapters API ====

@books_bp.route('/api/books/<book_id>/chapters', methods=['GET'])
def list_chapters(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    # 自动修复孤儿数据：清空指向不存在卷的 parent_id（历史删除卷未清空子章节导致）
    vol_ids = {c.id for c in chapters if c.is_volume}
    repaired = False
    for c in chapters:
        if not c.is_volume and c.parent_id and c.parent_id not in vol_ids:
            c.parent_id = ''
            repaired = True
    if repaired:
        db.session.commit()
    return jsonify([c.to_dict(include_content=False) for c in chapters])

@books_bp.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['GET'])
def get_chapter(book_id, chapter_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    return jsonify(ch.to_dict(include_content=True))

@books_bp.route('/api/books/<book_id>/chapters', methods=['POST'])
def create_chapter(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    data = request.json
    max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
    ch = Chapter(
        book_id=book_id, title=data.get('title', '新章节'),
        content=data.get('content', ''), order_index=data.get('order_index', max_order + 1),
        is_volume=data.get('is_volume', False), parent_id=data.get('parent_id', ''),
        notes=data.get('notes', '')
    )
    if ch.content:
        ch.word_count = count_words(ch.content)
    db.session.add(ch)
    db.session.flush()
    update_book_stats(book_id)

    # 若新章节标题含章节号（第N章/第N章等），按章节号自动重排顺序（不改动卷归入）
    if not ch.is_volume and parse_chapter_number(ch.title or '') is not None:
        try:
            resort_chapters_by_title(book_id, rebin_volumes=False)
        except Exception:
            pass  # 重排失败不影响章节创建

    # 自动检测是否需要生成动态报告（每5章触发）
    auto_report = None
    if not ch.is_volume and ch.content:
        try:
            result = _check_and_auto_generate_report(book_id)
            if result and 'report' in result:
                auto_report = result['report']
        except Exception:
            pass  # 自动生成失败不影响章节创建
        # 【P0-3】每 20 章自动触发防遗忘检查（daemon 线程，不阻塞）
        try:
            _maybe_auto_trigger_anti_forget_check(book_id)
        except Exception:
            pass
        # 【P1-2】章节落库后统一钩子：事件抽取 + 伏笔本章清单 + 实体注册
        hook_meta = None
        try:
            hook_meta = _after_chapter_persisted(book_id, ch)
            db.session.commit()
        except Exception:
            db.session.rollback()

    resp = ch.to_dict(include_content=True)
    if auto_report:
        resp['auto_report'] = auto_report
    if hook_meta:
        resp['event_log'] = hook_meta
    return jsonify(resp), 201

@books_bp.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['PUT'])
def update_chapter(book_id, chapter_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    old_content = ch.content
    data = request.json

    has_content_change = 'content' in data and data['content'] != old_content
    if has_content_change:
        versions = ChapterVersion.query.filter_by(chapter_id=chapter_id).order_by(ChapterVersion.version_num.desc()).all()
        new_ver = (versions[0].version_num + 1) if versions else 1
        ver = ChapterVersion(chapter_id=chapter_id, content=old_content, version_num=new_ver)
        db.session.add(ver)

    for field in ['title', 'content', 'order_index', 'status', 'is_volume', 'parent_id', 'notes']:
        if field in data:
            setattr(ch, field, data[field])
    if 'content' in data:
        ch.word_count = count_words(data['content'])
    ch.updated_at = datetime.now(timezone.utc)
    db.session.flush()
    update_book_stats(book_id)

    # 自动检测是否需要生成动态报告（每5章触发，仅内容变更时）
    auto_report = None
    if has_content_change and not ch.is_volume and ch.content:
        try:
            result = _check_and_auto_generate_report(book_id)
            if result and 'report' in result:
                auto_report = result['report']
        except Exception:
            pass
        # 【P0-3】每 20 章自动触发防遗忘检查（daemon 线程，不阻塞）
        try:
            _maybe_auto_trigger_anti_forget_check(book_id)
        except Exception:
            pass
        # 【P1-2】内容变更落库后统一钩子：事件抽取 + 伏笔本章清单 + 实体注册
        hook_meta = None
        if has_content_change:
            try:
                hook_meta = _after_chapter_persisted(book_id, ch)
                db.session.commit()
            except Exception:
                db.session.rollback()

    resp = ch.to_dict(include_content=True)
    if auto_report:
        resp['auto_report'] = auto_report
    if hook_meta:
        resp['event_log'] = hook_meta
    return jsonify(resp)

@books_bp.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['DELETE'])
def delete_chapter(book_id, chapter_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    # 若删除的是卷，清空其下章节的 parent_id，避免章节变孤儿（指向已删除卷）而不可见
    if ch.is_volume:
        Chapter.query.filter_by(book_id=book_id, parent_id=chapter_id, is_volume=False).update({'parent_id': ''})
    # 先删除章节版本（兼容 PostgreSQL 外键约束）
    ChapterVersion.query.filter_by(chapter_id=chapter_id).delete(synchronize_session=False)
    db.session.delete(ch)
    db.session.flush()
    update_book_stats(book_id)
    return jsonify({'success': True})

@books_bp.route('/api/books/<book_id>/chapters/reorder', methods=['POST'])
def reorder_chapters(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    data = request.json
    order = data.get('order', [])
    for item in order:
        Chapter.query.filter_by(id=item['id'], book_id=book_id).update({'order_index': item['order_index']})
    db.session.commit()
    return jsonify({'success': True})

@books_bp.route('/api/books/<book_id>/chapters/ghost-suggest', methods=['POST'])
def chapter_ghost_suggest(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    """正文幽灵字续写：根据当前章节已写内容，返回一小段顺延的续写建议（幽灵字）。
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    前端在编辑器中以浅灰"幽灵字"形式展示，按 Tab 一键采纳。
    """
    ch = Chapter.query.filter_by(id=(request.json or {}).get('chapter_id', ''), book_id=book_id).first()
    content = (request.json or {}).get('content', '')
    tail = (content or '').strip()
    if len(tail) < 8:
        return jsonify({'suggestion': ''})  # 内容太短，不打扰
    # 只取末尾一段（约最近 600 字）作为续写上下文，避免每次全量发送
    ctx = tail[-600:]

    title = ch.title if ch else ''

    msgs = [
        {'role': 'system', 'content': '你是资深网文续写助手。用户正在写作，请紧贴其行文风格、人物口吻与情节走向，顺延写出一小段续写（一段话，60~120字，省略号或自然断在最合适的句子处）。只输出续写内容本身，不要任何前缀、解释或引号。'},
        {'role': 'user', 'content': f'章节标题：{title}\n\n当前已写内容（末尾）：\n{ctx}\n\n请从这段结尾处自然续写一小段。'}
    ]
    suggestion, err = _call_llm(
        msgs,
        max_tokens=120,
        temperature=0.85,
        task_type='creation',
        scene_label='ghost_continue',
        book_id=book_id,
        chapter_id=ch.id if ch else None,
    )
    if err or not suggestion:
        return jsonify({'suggestion': '', 'error': (err or '')[:200]})
    suggestion = suggestion.strip()
    # 去掉 AI 可能附加的引号/前缀
    if suggestion.startswith('"') and suggestion.endswith('"'):
        suggestion = suggestion[1:-1]
    return jsonify({'suggestion': suggestion})

@books_bp.route('/api/books/<book_id>/chapters/rebin-volumes', methods=['POST'])
@login_required
def rebin_volumes(book_id):
    """手动触发按 50 章/卷重新归入卷：先清空所有章节 parent_id，删除现有卷，再按章节号排序重新分卷。"""
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    if book.user_id != request.current_user_id:
        return jsonify({'error': '无权操作该作品'}), 403
    try:
        # 清空所有非卷章节的 parent_id
        Chapter.query.filter_by(book_id=book_id, is_volume=False).update({'parent_id': ''})
        # 删除所有现有卷
        old_vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).all()
        for v in old_vols:
            db.session.delete(v)
        db.session.flush()
        # 重新排序 + 分卷
        count = resort_chapters_by_title(book_id, rebin_volumes=True)
        update_book_stats(book_id)
        vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).count()
        return jsonify({'success': True, 'chapters': count, 'volumes': vols})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ==== Chapter Versions ====

@books_bp.route('/api/books/<book_id>/chapters/<chapter_id>/versions', methods=['GET'])
def list_chapter_versions(book_id, chapter_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    versions = ChapterVersion.query.filter_by(chapter_id=chapter_id).order_by(ChapterVersion.version_num.desc()).all()
    return jsonify([v.to_dict() for v in versions])

@books_bp.route('/api/books/<book_id>/chapters/<chapter_id>/versions/<version_id>/restore', methods=['POST'])
def restore_chapter_version(book_id, chapter_id, version_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    ver = ChapterVersion.query.filter_by(id=version_id, chapter_id=chapter_id).first()
    if not ch or not ver:
        return jsonify({'error': 'Not found'}), 404
    ch.content = ver.content
    ch.word_count = count_words(ver.content)
    ch.updated_at = datetime.now(timezone.utc)
    db.session.flush()
    update_book_stats(book_id)
    return jsonify(ch.to_dict(include_content=True))

# ==== Characters API ====

@books_bp.route('/api/books/<book_id>/characters', methods=['GET'])
def list_characters(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    chars = Character.query.filter_by(book_id=book_id).all()
    return jsonify([c.to_dict() for c in chars])

@books_bp.route('/api/books/<book_id>/characters', methods=['POST'])
def create_character(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    data = request.json
    char = Character(
        book_id=book_id, name=(data.get('name', '新角色') or '新角色')[:50],
        role=(data.get('role', 'supporting') or 'supporting')[:50], description=data.get('description', ''),
        appearance=data.get('appearance', ''), personality=data.get('personality', ''),
        background=data.get('background', '')
    )
    if 'relationships' in data:
        char.relationships_json = json.dumps(data['relationships'], ensure_ascii=False)
    db.session.add(char)
    db.session.commit()
    return jsonify(char.to_dict()), 201

@books_bp.route('/api/books/<book_id>/characters/<char_id>', methods=['PUT'])
def update_character(book_id, char_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    char = Character.query.filter_by(id=char_id, book_id=book_id).first()
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    data = request.json
    for field in ['name', 'role', 'description', 'appearance', 'personality', 'background']:
        if field in data:
            setattr(char, field, data[field])
    if 'name' in data:
        char.name = (data['name'] or '')[:50]
    if 'role' in data:
        char.role = (data['role'] or 'supporting')[:50]
    if 'relationships' in data:
        char.relationships_json = json.dumps(data['relationships'], ensure_ascii=False)
    db.session.commit()
    return jsonify(char.to_dict())

@books_bp.route('/api/books/<book_id>/characters/<char_id>', methods=['DELETE'])
def delete_character(book_id, char_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    char = Character.query.filter_by(id=char_id, book_id=book_id).first()
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    db.session.delete(char)
    db.session.commit()
    return jsonify({'success': True})

# ==== Outlines API ====

@books_bp.route('/api/books/<book_id>/outlines', methods=['GET'])
def list_outlines(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    tree = build_outline_tree(outlines)
    return jsonify({'flat': [o.to_dict() for o in outlines], 'tree': tree})

@books_bp.route('/api/books/<book_id>/outlines', methods=['POST'])
def create_outline(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    data = request.json
    max_order = db.session.query(db.func.max(Outline.order_index)).filter_by(book_id=book_id).scalar() or -1
    outline = Outline(
        book_id=book_id, title=data.get('title', '新节点'),
        content=data.get('content', ''), order_index=data.get('order_index', max_order + 1),
        level=data.get('level', 0), parent_id=data.get('parent_id', '')
    )
    db.session.add(outline)
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'item': outline.to_dict(), 'tree': build_outline_tree(outlines)}), 201

@books_bp.route('/api/books/<book_id>/outlines/<outline_id>', methods=['PUT'])
def update_outline(book_id, outline_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    outline = Outline.query.filter_by(id=outline_id, book_id=book_id).first()
    if not outline:
        return jsonify({'error': 'Outline not found'}), 404
    data = request.json
    for field in ['title', 'content', 'order_index', 'level', 'parent_id']:
        if field in data:
            setattr(outline, field, data[field])
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'item': outline.to_dict(), 'tree': build_outline_tree(outlines)})

@books_bp.route('/api/books/<book_id>/outlines/<outline_id>', methods=['DELETE'])
def delete_outline(book_id, outline_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    outline = Outline.query.filter_by(id=outline_id, book_id=book_id).first()
    if not outline:
        return jsonify({'error': 'Outline not found'}), 404
    db.session.delete(outline)
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'tree': build_outline_tree(outlines)})

# ==== Stats API ====

@books_bp.route('/api/books/<book_id>/stats', methods=['GET'])
def get_book_stats(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    stats = DailyStats.query.filter_by(book_id=book_id).order_by(DailyStats.date).all()
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    return jsonify({
        'daily': [s.to_dict() for s in stats],
        'chapters': [{'title': c.title, 'word_count': c.word_count, 'date': c.created_at.isoformat() if c.created_at else None} for c in chapters]
    })

@books_bp.route('/api/books/<book_id>/stats', methods=['POST'])
def add_daily_stats(book_id):
    from app import (db, Book, BookBible, Chapter, ChapterVersion, Character,
                 DailyStats, Outline, Template, User, AISession, DynamicMemory,
                 DynamicReport, StageContent, count_words,
                 update_book_stats, resort_chapters_by_title, parse_chapter_number,
                 build_outline_tree, _after_chapter_persisted,
                 _maybe_auto_trigger_anti_forget_check, _check_and_auto_generate_report,
                 _call_llm)  # 请求期延迟导入，避免循环依赖
    data = request.json
    today = datetime.now(timezone.utc).date()
    stat = DailyStats.query.filter_by(book_id=book_id, date=today).first()
    if stat:
        stat.words_written += data.get('words_written', 0)
        stat.time_spent_minutes += data.get('time_spent_minutes', 0)
        stat.chapters_completed += data.get('chapters_completed', 0)
        prev_words = stat.words_written
    else:
        stat = DailyStats(
            book_id=book_id, date=today,
            words_written=data.get('words_written', 0),
            time_spent_minutes=data.get('time_spent_minutes', 0),
            chapters_completed=data.get('chapters_completed', 0)
        )
        db.session.add(stat)
        prev_words = 0
    db.session.commit()
    return jsonify(stat.to_dict())

# ==== Templates API ====

# /api/health 已迁移到 blueprints/health_bp.py（Blueprint 示范）

