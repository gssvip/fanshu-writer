"""【优化建议域】（自 chat_collab_bp.py 拆出，架构门禁 P2a）。

共享符号（SMART_DIMENSIONS, chat_collab_bp）由 chat_collab_bp.py 末尾的
_register_split_domains() 调用 init() 注入——本模块不反向 import
chat_collab_bp，避免循环导入；路由经 register() 挂到同一个
Blueprint，URL / endpoint / methods 与拆分前的装饰器注册完全一致。
"""
from __future__ import annotations

import json
from typing import Dict

from flask import Response, jsonify, request


def init(**deps):
    """chat_collab_bp 加载到文件末尾时调用：把共享符号注入本模块全局命名空间。"""
    globals().update(deps)


def register(bp):
    """把本域路由挂到 chat_collab_bp（endpoint 默认取函数名，与装饰器注册一致）。"""
    bp.add_url_rule('/api/ai/smart/dimensions', view_func=smart_dimensions, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/backfill-eventlog', view_func=backfill_eventlog, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/optimization-report', view_func=optimization_report, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/adopt-optimization-suggestion', view_func=adopt_optimization_suggestion, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/dismiss-optimization-suggestion', view_func=dismiss_optimization_suggestion, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/preview-impact', view_func=preview_impact, methods=['POST'])


def smart_dimensions():
    """返回 AI 智驾支持的维度列表（供前端渲染子按钮）。"""
    return jsonify({'dimensions': SMART_DIMENSIONS})


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
