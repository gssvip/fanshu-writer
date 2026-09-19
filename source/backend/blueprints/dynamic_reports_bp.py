"""动态报告域蓝图 — 从 app.py 拆出（巨石外迁：dynamic-reports 域）。

路由清单（11 个）：
  /api/books/<book_id>/dynamic-reports CRUD（GET/POST/batch-generate/batch-delete）
  /api/books/<book_id>/dynamic-reports/<report_id>（PUT/DELETE/regenerate）
  /api/books/<book_id>/dynamic-reports/auto-check（章节保存后自动生成）
  /api/books/<book_id>/dynamic-reports/context（AI 创作注入上下文）

依赖方向（无循环）：
  - 顶层仅依赖 flask + auth_utils + 标准库；
  - 模型与跨域 helper（db/_get_skill_prompts_by_category/_get_genre_label/
    build_auth_headers/_get_volume_chapters_ordered/...）在每个函数体内延迟导入，
    请求期 app 早已加载完毕（与 books_bp.py 同款模式）。

说明：本模块同时承载被多个蓝图复用的动态报告服务函数
  （_check_and_auto_generate_report / _auto_backfill_dynamic_reports_async 等），
  app.py / books_bp.py / export_bp.py 均在请求期从此模块延迟导入。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from auth_utils import login_required

dynamic_reports_bp = Blueprint('dynamic_reports', __name__)

DYNAMIC_REPORT_INTERVAL = 5  # 每5章生成一份报告


def _generate_dynamic_report_content(book_id, chapter_start, chapter_end, skill_pack_ids=None):
    """内部函数：调用AI生成动态报告内容。
    skill_pack_ids: 可选，技能包ID列表，用于注入提示词（P0-2修复）"""
    from app import (Book, AIConfig, BookBible, Chapter, build_auth_headers,
                     _get_skill_prompts_by_category, _get_genre_label)

    book = Book.query.get(book_id)
    if not book:
        return None, 'Book not found'

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return None, '请先配置 AI 模型 API Key'

    bible = BookBible.query.filter_by(book_id=book_id).first()
    # 获取指定范围的章节（按order_index排序，非卷标）
    all_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    # chapter_start/chapter_end 是1-based的章号
    target_chapters = all_chapters[chapter_start - 1:chapter_end] if chapter_start <= len(all_chapters) else []

    if not target_chapters:
        return None, '指定范围内无章节'

    # P0-2: 注入技能包提示词（narrative_debt/foreshadow_register 用于动态摘要生成）
    skill_note = ''
    if skill_pack_ids:
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['narrative_debt', 'foreshadow_register', 'lock_facts'], mode='agent')

    # 拼接章节内容（每章截取前800字，控制总量）
    chapters_text = []
    total_chars = 0
    max_chars = 6000
    for ch in target_chapters:
        snippet = f'【{ch.title}】\n{(ch.content or "")[:800]}'
        if total_chars + len(snippet) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 100:
                snippet = snippet[:remaining]
                chapters_text.append(snippet)
            break
        chapters_text.append(snippet)
        total_chars += len(snippet)

    chapters_content = '\n\n'.join(chapters_text)

    system_prompt = f"""你是专业的小说分析师。请仔细阅读以下第{chapter_start}章到第{chapter_end}章的内容，汇总成一份简洁的报告。

报告必须包含以下要素（如果出现的话），总字数不超过500字：

1. 【人物】本章新出场或有关键表现的角色及其状态变化
2. 【事件】关键剧情事件和转折
3. 【时间】故事内时间线推进
4. 【地点】涉及的重要地点
5. 【势力】出现或变动的势力/组织
6. 【伏笔】埋设或回收的伏笔
7. 【境界】角色境界/实力变化
8. 【关系】人物关系变化
9. 【物资】主要角色当前拥有的重要物品、装备、功法、丹药等（按角色列出）

格式要求：
- 用简洁的条目式写法，每类1-3条
- 只记录关键信息，不展开描述
- 不要写废话和过渡句
- 直接输出报告内容，不要加标题和前后缀

{skill_note}"""

    user_content = f"""作品：{book.title}
题材：{_get_genre_label(book)}

第{chapter_start}章到第{chapter_end}章内容：
{chapters_content}

请生成动态报告（≤500字）："""

    # P2-8：动态报告失败重试（指数退避，最多2次重试），全失败则降级为章节摘要拼接
    import time as _time
    max_retries = 2
    last_error = None
    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(f'{base}/chat/completions',
                    headers=build_auth_headers(api_key),
                    json={
                        'model': model,
                        'messages': [
                            {'role': 'system', 'content': system_prompt},
                            {'role': 'user', 'content': user_content}
                        ],
                        'temperature': 0.3,
                        'max_tokens': 1200,
                    },
                    timeout=120)
                result = resp.json()
                content = result['choices'][0]['message']['content'].strip()
                if content and len(content) > 50:
                    return content, None
                last_error = f'LLM 返回内容过短（{len(content)}字）'
            except Exception as e:
                last_error = str(e)
            # 指数退避：1s, 2s
            if attempt < max_retries:
                _time.sleep(2 ** attempt)
        # 全部重试失败，降级为章节摘要拼接
        fallback_parts = [f'【降级摘要·第{chapter_start}-{chapter_end}章】（LLM生成失败，自动拼接）']
        for ch in target_chapters:
            snippet = (ch.content or '')[:120].replace('\n', ' ').strip()
            if snippet:
                fallback_parts.append(f'- {ch.title}：{snippet}')
        fallback_content = '\n'.join(fallback_parts)[:800]
        return fallback_content, None
    except Exception as e:
        # 连降级都失败的极端情况
        return None, f'{str(e)} | last_error={last_error}'


def _check_and_auto_generate_report(book_id, max_intervals=1, revise_async=True):
    """检查并自动生成动态报告（每5章一份，按顺序覆盖全部5章区间）。

    【dyn5】导入小说与平台生成统一自动通道，用户无需手动生成：
    - max_intervals=1（默认，章节保存的增量场景）：从最早的缺失区间按顺序补1个，控制请求耗时；
    - max_intervals=None（导入后的全量回填场景）：从第1个区间按顺序补齐所有缺失报告；
    - 每生成一份报告后同步创建状态快照，并触发【P2 逆向通道】维度增量修订
      （revise_async=True 时后台线程执行，导入回填线程内传 False 串行执行避免并发打爆 LLM）。
    返回 {'report': 最后一份新报告} / {'error': ...} / None（无需生成）。
    """
    from app import db, DynamicReport, Chapter, app

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    chapter_count = len(chapters)
    if chapter_count == 0:
        return None

    # 完整区间数：5章 -> 1-5；10章 -> 1-5和6-10（尾巴不足5章的暂不生成）
    total_intervals = chapter_count // DYNAMIC_REPORT_INTERVAL
    if total_intervals == 0:
        return None

    # 已有报告的区间集合
    existing_reports = DynamicReport.query.filter_by(book_id=book_id).all()
    existing_keys = {(r.chapter_start, r.chapter_end) for r in existing_reports}

    # 按顺序找缺失区间（1-5、6-10、…）
    missing = []
    for k in range(1, total_intervals + 1):
        cs = (k - 1) * DYNAMIC_REPORT_INTERVAL + 1
        ce = k * DYNAMIC_REPORT_INTERVAL
        if (cs, ce) not in existing_keys:
            missing.append((cs, ce))
    if not missing:
        return None

    # 增量场景从最早的缺失区间按顺序补（缺多个时每次保存补齐一个，逐步追平）；
    # 全量回填（导入后）传 None 一次性按顺序补齐
    if max_intervals:
        missing = missing[:int(max_intervals)]

    last_report = None
    last_error = None
    for (cs, ce) in missing:
        content, error = _generate_dynamic_report_content(book_id, cs, ce)
        if error:
            last_error = error
            continue
        report = DynamicReport(
            book_id=book_id, title=f'动态-({cs}-{ce}章)', content=content or '',
            chapter_start=cs, chapter_end=ce, auto_generated=True
        )
        db.session.add(report)
        db.session.commit()
        last_report = report

        # 借鉴 PlotPilot 检查点快照：每5章自动备份 BookBible + DynamicMemory 关键状态
        try:
            _create_state_snapshot(book_id, ce)
        except Exception as snap_err:
            try:
                app.logger.warning(f'快照创建失败（不影响主流程）: {snap_err}')
            except Exception:
                pass

        # 【P2 逆向通道】章节→维度增量修订：按这5章正文修订人物/伏笔/地点维度
        try:
            if revise_async:
                _revise_dimensions_from_chapters_async(book_id, cs, ce)
            else:
                _revise_dimensions_from_chapters(book_id, cs, ce)
        except Exception:
            pass  # 修订失败不影响报告生成

    if last_report is not None:
        return {'report': last_report.to_dict()}
    if last_error:
        return {'error': last_error}
    return None


def _revise_dimensions_from_chapters(book_id, chapter_start, chapter_end):
    """【P2 逆向通道】章节→维度增量修订：写完章节后（每5章），基于该区间正文与维度现有内容，
    用模型做差异修订，把新出场人物/新地点/新埋伏笔以"增量补丁"形式追加到对应维度。
    只追加不改写既有内容（append-only，可随时人工编辑回滚），避免污染作者已定稿的设定。"""
    from app import db, Book, BookBible, Chapter, AIConfig, build_auth_headers

    book = Book.query.get(book_id)
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not book or not bb:
        return None
    config = AIConfig.get_active()
    if not config or not config.api_key:
        return None
    api_key = config.api_key
    base_url = (config.base_url or os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')).rstrip('/')
    if not base_url.endswith('/v1'):
        base_url += '/v1'
    model = config.model or os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    # 识别类任务用识别模型（便宜快），为空回退主模型
    model = (config.get_model_for_task('recognition') if config else None) or model

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    target = chapters[chapter_start - 1:chapter_end]
    if not target:
        return None
    chapters_text = '\n\n'.join(f'【{c.title or ""}】\n{(c.content or "")[:1200]}' for c in target)[:9000]

    # 维度现有内容（做差异对照，避免重复追加已有信息）
    dims = {
        'character_profiles': (bb.character_profiles or '')[:2000],
        'locations': (bb.locations or '')[:1500],
        'foreshadowing': (bb.foreshadowing or '')[:1500],
    }
    dim_block = f"""【人物档案·现有内容】
{dims['character_profiles'] or '（空）'}

【地点·现有内容】
{dims['locations'] or '（空）'}

【伏笔·现有内容】
{dims['foreshadowing'] or '（空）'}"""

    system_prompt = f"""你是小说设定管理员。对照"维度现有内容"与第{chapter_start}-{chapter_end}章正文，做增量修订：
只提取正文中【新出现且现有内容未记录】的信息，追加到对应维度。禁止复述已有内容，禁止改写/删除已有内容。

输出严格 JSON（不要代码块包裹，不要解释）：
{{
  "character_profiles": "新出场/新变化人物的增量条目，纯文本，每人按『姓名：身份：性格：变化：』简洁一行；无新增输出空串",
  "locations": "新出现的地点/场景增量条目，每条一行『地名：说明』；无新增输出空串",
  "foreshadowing": "正文中新埋设或回收的伏笔增量条目，每条一行『伏笔：埋设/回收于第X-Y章』；无新增输出空串"
}}"""

    user_content = f"""作品：《{book.title}》
{dim_block}

【第{chapter_start}-{chapter_end}章正文】
{chapters_text}

请输出增量修订 JSON："""
    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers=build_auth_headers(api_key),
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content}
                ],
                'temperature': 0.2, 'max_tokens': 1500,
            },
            timeout=120)
        result = resp.json()
        raw = (result['choices'][0]['message']['content'] or '').strip()
        # 剥离可能的 markdown 代码块
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.S).strip()
        data = json.loads(raw)
    except Exception:
        return None  # 解析失败静默跳过（不污染维度）

    header = f'\n\n【第{chapter_start}-{chapter_end}章·增量修订（自动）】\n'
    changed = []
    for field, val in data.items():
        if field not in ('character_profiles', 'locations', 'foreshadowing'):
            continue
        val = (val or '').strip()
        if not val:
            continue
        cur = getattr(bb, field, '') or ''
        setattr(bb, field, cur + header + val)
        changed.append(field)
    if changed:
        db.session.commit()
    return {'changed': changed}


# 维度修订并发闸：同一时间只允许一个修订线程（导入回填量大时避免打爆 LLM）
_DIM_REVISE_LOCK = threading.Lock()


def _revise_dimensions_from_chapters_async(book_id, chapter_start, chapter_end):
    """【P2】daemon 线程执行维度增量修订（不阻塞章节保存响应）。
    忙时直接跳过：该区间的增量会随下一次快照/报告周期自然补上，不堆积任务。"""
    from app import app

    def _bg():
        if not _DIM_REVISE_LOCK.acquire(blocking=False):
            return
        try:
            with app.app_context():
                try:
                    _revise_dimensions_from_chapters(book_id, chapter_start, chapter_end)
                except Exception:
                    pass
        finally:
            _DIM_REVISE_LOCK.release()
    try:
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return t
    except Exception:
        return None


def _auto_backfill_dynamic_reports_async(book_id):
    """【dyn5】导入小说后自动回填动态报告（daemon 线程，不阻塞导入响应）。
    从第1个5章区间按顺序补齐所有缺失报告，并在同一线程内串行触发维度增量修订（P2），
    避免多区间并发修订打爆 LLM。未配置 AI Key 时静默跳过（报告可后续手动补）。"""
    from app import app, Book

    try:
        book = Book.query.get(book_id)
        if not book:
            return None

        def _bg():
            with app.app_context():
                try:
                    _check_and_auto_generate_report(book_id, max_intervals=None, revise_async=False)
                except Exception:
                    pass
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return t
    except Exception:
        return None


def _create_state_snapshot(book_id, chapter_end):
    """创建叙事状态检查点快照（借鉴 PlotPilot checkpoint）。
    备份 BookBible 关键字段 + DynamicMemory 5文件，存入 bb.state_snapshots。
    每个快照含：snapshot_id / chapter_end / created_at / bible_fields / dynamic_memory。
    最多保留 20 个快照（超出按时间淘汰最旧的）。"""
    from app import db, BookBible, DynamicMemory

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return
    # 备份 BookBible 关键叙事字段（不含快照自身，避免递归膨胀）
    bible_fields = {}
    for field in ['worldbuilding', 'character_profiles', 'timeline', 'foreshadowing',
                  'style_guide', 'key_rules', 'locations', 'concept', 'plot_design',
                  'relation_graph', 'inventory', 'character_volumes', 'dynamic_volumes',
                  'foreshadowing_volumes', 'locations_volumes', 'foreshadowing_graph',
                  'outline_hierarchy', 'chapter_changes_log']:
        bible_fields[field] = getattr(bb, field, '') or ''
    # 备份 DynamicMemory 5文件
    dm_data = {}
    try:
        dm = DynamicMemory.query.filter_by(book_id=book_id).first()
        if dm:
            for key in ['narrative_engine', 'foreshadowing_tracker', 'character_ecosystem',
                         'ability_world', 'health_dashboard']:
                dm_data[key] = getattr(dm, key, '') or ''
    except Exception:
        pass
    snapshot = {
        'snapshot_id': str(uuid.uuid4())[:8],
        'chapter_end': chapter_end,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'bible_fields': bible_fields,
        'dynamic_memory': dm_data,
    }
    existing = []
    try:
        existing = json.loads(bb.state_snapshots or '[]') if bb.state_snapshots else []
    except Exception:
        existing = []
    existing.append(snapshot)
    # 最多保留 20 个，淘汰最旧
    if len(existing) > 20:
        existing = existing[-20:]
    bb.state_snapshots = json.dumps(existing, ensure_ascii=False)
    db.session.commit()


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports', methods=['GET'])
@login_required
def list_dynamic_reports(book_id):
    """获取所有动态报告"""
    from app import DynamicReport

    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
    return jsonify([r.to_dict() for r in reports])


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports', methods=['POST'])
@login_required
def create_dynamic_report(book_id):
    """手动创建动态报告"""
    from app import db, Book, DynamicReport

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    chapter_start = data.get('chapter_start', 0)
    chapter_end = data.get('chapter_end', 0)

    if not chapter_start or not chapter_end or chapter_end < chapter_start:
        return jsonify({'error': '请指定有效的章节范围'}), 400

    # 如果没有提供content，调用AI生成
    content = data.get('content', '')
    if not content:
        content, error = _generate_dynamic_report_content(book_id, chapter_start, chapter_end)
        if error:
            return jsonify({'error': error}), 500

    title = data.get('title', f'动态-({chapter_start}-{chapter_end}章)')
    report = DynamicReport(
        book_id=book_id, title=title, content=content,
        chapter_start=chapter_start, chapter_end=chapter_end,
        auto_generated=False
    )
    db.session.add(report)
    db.session.commit()
    return jsonify(report.to_dict()), 201


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/batch-generate', methods=['POST'])
@login_required
def batch_generate_dynamic_reports(book_id):
    """按卷批量生成动态报告（每5章一份）。AI识别按钮选择某卷后，自动生成该卷内所有
    尚未生成的5章区间动态报告，减少一个个手动添加的麻烦。
    参数：volume_id（可选，空则全卷）、volume_title、skill_pack_ids、overwrite（是否覆盖已存在，默认false）"""
    from app import db, Book, AIConfig, Chapter, DynamicReport, _get_volume_chapters_ordered

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    overwrite = data.get('overwrite', False)

    config = AIConfig.get_active()
    if not config or not config.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 收集全作品非卷章节（按 order_index，用于计算全局 1-based 章号）
    all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not all_chs:
        return jsonify({'error': '该作品暂无章节，无法批量生成动态报告。'}), 400

    # 确定该卷章节的全局1-based起止章号
    if volume_id:
        # 用共享助手取该卷章节：parent_id 优先，回退顺序遍历（兼容未设置 parent_id 的旧数据）
        vol_chs = _get_volume_chapters_ordered(book_id, volume_id)
        if not vol_chs:
            return jsonify({'error': f'卷“{volume_title or volume_id}”内暂无章节（请确认章节已归入该卷）'}), 400
        # 计算这些章节在 all_chs 中的全局序号（1-based）
        ch_id_to_idx = {c.id: i for i, c in enumerate(all_chs)}
        vol_ch_idx = [ch_id_to_idx[c.id] for c in vol_chs if c.id in ch_id_to_idx]
        if not vol_ch_idx:
            return jsonify({'error': f'卷“{volume_title or volume_id}”内暂无章节'}), 400
        global_start = min(vol_ch_idx) + 1
        global_end = max(vol_ch_idx) + 1
    else:
        global_start = 1
        global_end = len(all_chs)

    # 按5章一份切分区间
    intervals = []
    s = global_start
    while s <= global_end:
        e = min(s + DYNAMIC_REPORT_INTERVAL - 1, global_end)
        intervals.append((s, e))
        s = e + 1

    if not intervals:
        return jsonify({'error': '该卷章节范围无效'}), 400

    # 查询已存在的报告（按chapter_start/chapter_end匹配），决定是否跳过/覆盖
    existing = DynamicReport.query.filter_by(book_id=book_id).all()
    existing_map = {(r.chapter_start, r.chapter_end): r for r in existing}

    generated = []
    skipped = []
    errors = []
    for (cs, ce) in intervals:
        key = (cs, ce)
        if key in existing_map and not overwrite:
            skipped.append({'chapter_start': cs, 'chapter_end': ce, 'reason': '已存在'})
            continue
        # 调用AI生成内容（P0-2: 传入 skill_pack_ids）
        content, err = _generate_dynamic_report_content(book_id, cs, ce, skill_pack_ids=skill_pack_ids)
        if err:
            errors.append({'chapter_start': cs, 'chapter_end': ce, 'error': err})
            continue
        if key in existing_map and overwrite:
            # 覆盖已存在报告
            r = existing_map[key]
            r.content = content
            r.title = f'动态-({cs}-{ce}章)'
            r.auto_generated = False
        else:
            # 新建报告
            r = DynamicReport(
                book_id=book_id, title=f'动态-({cs}-{ce}章)', content=content,
                chapter_start=cs, chapter_end=ce, auto_generated=False
            )
            db.session.add(r)
        db.session.commit()
        generated.append(r.to_dict())

    return jsonify({
        'success': True,
        'volume_title': volume_title or '全部章节',
        'chapter_range': [global_start, global_end],
        'total_intervals': len(intervals),
        'generated_count': len(generated),
        'skipped_count': len(skipped),
        'error_count': len(errors),
        'generated': generated,
        'skipped': skipped,
        'errors': errors,
    })


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/<report_id>', methods=['PUT'])
@login_required
def update_dynamic_report(book_id, report_id):
    """更新动态报告"""
    from app import db, DynamicReport

    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404

    data = request.get_json() or {}
    for field in ['title', 'content', 'chapter_start', 'chapter_end']:
        if field in data:
            setattr(report, field, data[field])
    db.session.commit()
    return jsonify(report.to_dict())


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/<report_id>', methods=['DELETE'])
@login_required
def delete_dynamic_report(book_id, report_id):
    """删除动态报告"""
    from app import db, DynamicReport

    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404
    db.session.delete(report)
    db.session.commit()
    return jsonify({'success': True})


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/batch-delete', methods=['POST'])
@login_required
def batch_delete_dynamic_reports(book_id):
    """批量删除动态报告"""
    from app import db, DynamicReport

    data = request.get_json() or {}
    report_ids = data.get('report_ids') or []
    if not isinstance(report_ids, list) or not report_ids:
        return jsonify({'error': '请提供要删除的报告ID列表'}), 400

    deleted = DynamicReport.query.filter(
        DynamicReport.book_id == book_id,
        DynamicReport.id.in_(report_ids)
    ).all(synchronize_session=False)
    deleted_ids = [r.id for r in deleted]
    for r in deleted:
        db.session.delete(r)
    db.session.commit()
    return jsonify({'success': True, 'deleted_count': len(deleted_ids), 'deleted_ids': deleted_ids})


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/<report_id>/regenerate', methods=['POST'])
@login_required
def regenerate_dynamic_report(book_id, report_id):
    """重新生成动态报告内容（AI）。支持可选 skill_pack_ids 注入提示词。"""
    from app import db, DynamicReport

    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404

    data = request.get_json(silent=True) or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    content, error = _generate_dynamic_report_content(book_id, report.chapter_start, report.chapter_end, skill_pack_ids=skill_pack_ids)
    if error:
        return jsonify({'error': error}), 500

    report.content = content or ''
    db.session.commit()
    return jsonify(report.to_dict())


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/auto-check', methods=['POST'])
@login_required
def auto_check_dynamic_report(book_id):
    """检查并自动生成动态报告（章节保存后触发）"""
    result = _check_and_auto_generate_report(book_id)
    if result is None:
        return jsonify({'success': True, 'message': '无需生成新报告', 'report': None})
    if 'error' in result:
        return jsonify({'success': False, 'error': result['error']}), 500
    return jsonify({'success': True, 'message': '已自动生成动态报告', 'report': result['report']})


@dynamic_reports_bp.route('/api/books/<book_id>/dynamic-reports/context', methods=['GET'])
@login_required
def get_dynamic_report_context(book_id):
    """获取最近的动态报告内容（用于AI创作时注入上下文，减少token）"""
    from app import DynamicReport

    # 返回最近10份报告（覆盖更长前文记忆，保证剧情连贯）
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start.desc()
    ).limit(10).all()
    # 按正序返回
    reports.reverse()
    return jsonify({
        'reports': [r.to_dict() for r in reports],
        'context_text': '\n\n'.join([f'【{r.title}】\n{r.content}' for r in reports if r.content])
    })