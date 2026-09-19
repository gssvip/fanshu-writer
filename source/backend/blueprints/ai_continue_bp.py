"""【正文滚动创作域·蓝图】（自 app.py 拆出，架构门禁 P1；含 AI 定点修改 / SSE / 批写）。

路由清单（5 个）：
  POST /api/books/<book_id>/ai-continue            正文滚动创作（多 Agent 协同版）
  POST /api/books/<book_id>/ai-spot-fix            Spot-Fix 定点修订（P2-9）
  POST /api/books/<book_id>/ai-continue/stream     正文滚动创作流式版（SSE）
  POST /api/books/<book_id>/ai-continue-batch      连续创作（批量，普通 JSON）
  POST /api/books/<book_id>/ai-continue-batch/stream  连续创作流式版（SSE）

依赖方向（无循环）：
  - 路由函数体引用 app.py + 本域纯辅助函数（blueprints/ai_continue_helpers.py）中的大量
    模型/helper；均采用 init(app_module=…) 命名空间注入机制，函数体全局名在请求期解析。
  - 纯辅助函数已拆到 blueprints/ai_continue_helpers.py（单文件行数门禁），此处 re-import
    以保持路由调用与 app.py 向后兼容 re-export 不变。

兼容性：app.py 末尾 re-export _consistency_check / _build_deai_rules_block /
  _build_continue_fingerprint_deps，保证 chat_smart_edit_bp.py 与既有测试的
  `from app import X` / `app_module.X` 延迟引用不受影响。
"""
from __future__ import annotations

from flask import Blueprint

from auth_utils import login_required
from blueprints import ai_continue_helpers
from blueprints.ai_continue_helpers import (
    _build_ai_continue_context,
    _build_continue_fingerprint_deps,
    _build_deai_rules_block,
    _calc_chapter_score,
    _consistency_check,
    _extract_chapter_body,
    _extract_chapter_title,
    _format_chapter_title,
    _generate_chapter_plan,
    _run_blocking_with_heartbeat,
    _stream_llm_chunks_with_heartbeat,
)

ai_continue_bp = Blueprint('ai_continue', __name__)


def _inject_from(app_module):
    """把 app_module 的全局命名空间（非 dunder）注入本模块 globals()。"""
    for _n, _v in vars(app_module).items():
        if not _n.startswith('__'):
            globals()[_n] = _v


def init(app_module=None, **deps):
    """app.py 完成所有模型/helper 定义后调用：向本模块与 ai_continue_helpers 注入全局命名空间。"""
    ai_continue_helpers.init(app_module, **deps)
    if app_module is not None:
        _inject_from(app_module)
    if deps:
        globals().update(deps)


@ai_continue_bp.before_request
def _resync_app_globals():
    """请求期再把 app 最新全局名同步一次（本模块 + helpers），使测试 monkeypatch 生效。"""
    import app as _app
    ai_continue_helpers.init(_app)
    _inject_from(_app)


# ==== 路由（自 app.py 原样迁出） ====

@ai_continue_bp.route('/api/books/<book_id>/ai-continue', methods=['POST'])
@login_required
def ai_continue(book_id):
    """正文滚动创作（多 Agent 协同版，14项优化）：
    分层 bible 注入+按卷维度+卷纲对齐 / 滚动记忆防遗忘 / 伏笔紧迫度Top25 /
    章节计划前置 / 动态temperature正文生成 / 去AI味审校（容错+deai_status）/
    一致性检查（key_rules/人设）。【优化2】TaskGraph 统一编排：写前构建任务图，
    伏笔任务等安全任务自动执行，各 LLM 阶段完成后 mark_stage 回写，结束持久化轨迹。"""
    book = Book.query.get(book_id)
    if not book: return jsonify({'error': 'Not found'}), 404

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    instruction = request.json.get('instruction', '')
    skill_pack_ids = request.json.get('skill_pack_ids', [])
    enable_consistency_check = request.json.get('enable_consistency_check', False)  # 默认关闭（OOC检测已合并到system_prompt，不再单独调LLM）
    # 修复上下文脱节：接收前端传入的待写章号 + 上一章未保存内容
    target_chapter_num = request.json.get('target_chapter_num')
    prev_chapter_content = request.json.get('prev_chapter_content')
    # 章节正文语言风格（行文文风，最多3个叠加）
    chapter_lang_styles = request.json.get('chapter_lang_styles', [])
    # Prompt 上下文缓存旁路：True=强制重新从各维度资料拼prompt(改了未存库的小设定时用)
    skip_prompt_cache = bool(request.json.get('skip_prompt_cache', False) or request.json.get('skip_cache', False))
    # S1：critical 门禁被 block 后，前端二次确认可传 ignore_gates=True 强制落库
    ignore_gates = bool(request.json.get('ignore_gates', False))

    try:
        ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num, prev_chapter_content, chapter_lang_styles, skip_cache=skip_prompt_cache)
        system_prompt = ctx['system_prompt']
        user_prompt = ctx['user_prompt']
        temperature = ctx['temperature']
        max_tokens = ctx['max_tokens']
        api_key = ctx['api_key']
        base_url = ctx['base_url']
        model = ctx['model']

        # ===== 【优化2】TaskGraph 统一编排：写前任务图+安全任务；异常降级旧直连模式 =====
        wp_graph = wp_runner = None
        try:
            from smart_planner import build_writing_pipeline
            wp_graph, wp_runner, _mission = build_writing_pipeline(
                book_id, bb, ctx['current_chapter_num'],
                skill_pack_ids=skill_pack_ids, enable_consistency_check=enable_consistency_check)
            if wp_runner: wp_runner.mark_stage(wp_graph, 't2_ctx', 'done')
        except Exception:
            wp_graph = wp_runner = None

        # ===== P2：Context Manifest（上下文来源 + hash + token 预算，事后溯源）=====
        context_manifest_data = None
        if ContextOrchestrator is not None:
            try:
                # S2：动态预算（ceiling 12k；模型上下文 ≥ 32k 时放宽到 16k）
                ctx_win = ContextOrchestrator._heuristic_context_window(model)
                auto_ceiling = 16000 if ctx_win >= 32768 else 12000
                # 粗估 system_prompt tokens
                _sys_cn = len([c for c in system_prompt if '\u4e00' <= c <= '\u9fff'])
                _sys_other = len(system_prompt) - _sys_cn
                _sys_tok = int(_sys_cn / 1.5 + _sys_other / 4)
                dynamic_budget = ContextOrchestrator.dynamic_budget(
                    max_gen_tokens=max_tokens,
                    model_name=model,
                    system_prompt_estimate=_sys_tok,
                    ceiling=auto_ceiling,
                )
                _orch = ContextOrchestrator(token_budget=dynamic_budget)
                _sources = {
                    'key_rules': getattr(bb, 'key_rules', '') or '',
                    'worldbuilding': getattr(bb, 'worldbuilding', '') or '',
                    'character_profiles': getattr(bb, 'character_profiles', '') or '',
                    'plot_design': getattr(bb, 'plot_design', '') or '',
                    'concept': getattr(bb, 'concept', '') or book.synopsis or '',
                }
                if prev_chapter_content:
                    _sources['prev_chapter'] = prev_chapter_content[:2000]
                _manifest = _orch.prepare(
                    sources=_sources,
                    chapter_num=ctx['current_chapter_num'],
                    book_id=book_id)
                context_manifest_data = _manifest.to_dict()
            except Exception:
                pass  # manifest 失败不阻断章节生成

        # ===== 正文生成（经 LLM Gateway 统一入口：错误分类 + 智能重试 + 空内容检测）=====
        draft_content, llm_error = _llm_chat(
            [{'role': 'system', 'content': system_prompt},
             {'role': 'user', 'content': user_prompt}],
            api_key=api_key, base_url=base_url, model=model,
            temperature=temperature, max_tokens=max_tokens, timeout=180)
        if not draft_content or not draft_content.strip():
            # 超时类错误返回 504，其他返回 502
            status = 504 if (llm_error and '超时' in llm_error) else 502
            return jsonify({'error': llm_error or 'LLM 返回空内容，请重试'}), status

        if wp_runner:  # 优化2：t2_plan/t3_draft 阶段完成
            wp_runner.mark_stage(wp_graph, 't2_plan', 'done' if ctx.get('chapter_plan') else 'skipped')
            wp_runner.mark_stage(wp_graph, 't3_draft', 'done', {'chars': len(draft_content)})

        # 【修复】额外校验：剥离内部标签后仍有正文，防止 LLM 只输出标签导致门禁误报"正文为空"
        _pre_check_body = _extract_chapter_body(draft_content)
        _pre_check_body = re.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', _pre_check_body).strip()
        if not _pre_check_body:
            return jsonify({'error': 'LLM 仅输出结构标签（pre_write_check/chapter_changes），无正文内容，请重试'}), 502

        # ===== 【字数铁律】初稿字数校验 + AI 重写修正（非物理截断，保证章节完整）=====
        # 【修复】改用公共函数 _ensure_word_count，与流式/连续/连续流式模式统一
        draft_content, review_notes_prefix = _ensure_word_count(
            draft_content, api_key, base_url, model, max_tokens, ctx['current_chapter_num'])
        if wp_runner: wp_runner.mark_stage(wp_graph, 't4_wc', 'done', {'chars': len(draft_content)})

        # ===== 去 AI 味审校 Agent（#6：容错+可观测；2026-08-23 默认启用）=====
        # 内置统一去AI规则（chat_collab_bp 的 GENERAL_CORE_RULES + DEAI_RULES）常驻生效，
        # 不再依赖技能包勾选；审查类(review)技能包作为增强叠加（build_review_rules 内部合并）。
        polished_content = draft_content
        review_notes = review_notes_prefix
        deai_status = 'skipped'  # skipped / rules_ok / rules_missing / success / failed
        deai_rules_block = ''
        try:
            deai_rules_block, _deai_build_status = _build_deai_rules_block(skill_pack_ids, book)
            deai_status = 'rules_ok' if _deai_build_status == 'ok' else 'rules_missing'
        except Exception as _deai_e:
            # helper 也兜不住（连 DEAI_ONLY_RULES import 都失败），显式标 missing 而非静默
            app.logger.error(f'ai_continue 去AI规则构建失败: {_deai_e}')
            print(f'[去AI] ai_continue 去AI规则构建失败: {_deai_e}', file=sys.stderr)
            deai_status = 'rules_missing'
        if deai_rules_block:
            deai_system = ("你是番茄去AI味审查员。对以下刚写好的章节正文做去AI味审校，按规则修改后只输出修改后的正文。\n\n"
                           + deai_rules_block
                           + "\n\n【硬性约束】修改后字数仍须 2400±100（2300-2500区间，含标点），保留原章节的剧情走向和钩子，只改文风不改剧情。")
            try:
                deai_resp = _post_llm_adaptive(api_key, base_url, model,
                    {'model': model,
                     'messages': [{'role':'system','content':deai_system},
                                  {'role':'user','content':f'请审校以下章节正文：\n\n{draft_content}'}],
                     'temperature': 0.5, 'max_tokens': max_tokens},
                    timeout=180)
                deai_result = deai_resp.json()
                polished = deai_result['choices'][0]['message']['content'].strip()
                # 【字数铁律】审校后字数校验：必须落在 2300-2500 区间
                polished_len = _count_cn_chars(polished)
                if polished and 2300 <= polished_len <= 2500:
                    polished_content = polished
                    review_notes = (review_notes_prefix + ' 已自动去AI味审校(' + str(polished_len) + '字)').strip()
                    deai_status = 'success'
                elif polished and polished_len > 500:
                    # 字数不达标但有内容，标记为失败但仍返回初稿
                    review_notes = (review_notes_prefix + f' 去AI味审校返回字数异常({polished_len}字)，已回滚使用初稿').strip()
                    deai_status = 'failed'
                else:
                    review_notes = (review_notes_prefix + ' 去AI味审校返回为空，已回滚使用初稿').strip()
                    deai_status = 'failed'
            except Exception as e:
                review_notes = (review_notes_prefix + f' 去AI味审校异常：{str(e)[:100]}，已回滚使用初稿').strip()
                deai_status = 'failed'

        # ===== 一致性检查 Agent（#13：独立 Agent，P1扩展：含 chapter_plan 比对）=====
        # 一致性检查属识别/检查类任务，用识别模型
        consistency_passed = True
        consistency_issues = ''
        if enable_consistency_check:
            consistency_passed, consistency_issues = _consistency_check(
                book_id, bb, polished_content, ctx['current_chapter_num'],
                api_key, base_url, ctx.get('recognition_model', model), max_tokens=800,
                chapter_plan=ctx.get('chapter_plan', '')
            )
        if wp_runner:  # 优化2：t5_deai/t6_cchk 阶段完成
            wp_runner.mark_stage(wp_graph, 't5_deai', deai_status if deai_status != 'skipped' else 'skipped',
                                 {'status': deai_status})
            wp_runner.mark_stage(wp_graph, 't6_cchk', 'done' if enable_consistency_check else 'skipped',
                                 {'passed': consistency_passed})

        # ===== P1-6 + P1-7：CHANGES 解析 + delta 回写（非流式版）=====
        changes_applied = None
        if extract_changes and apply_chapter_changes:
            try:
                body, changes = extract_changes(polished_content)
                if changes:
                    # 剥离 CHANGES 标签后的正文（落库用纯正文）
                    if body and len(body) > 200:
                        polished_content = body
                    ch_obj = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index.desc()).first()
                    ch_id = ch_obj.id if ch_obj else ''
                    changes_applied = apply_chapter_changes(
                        bb, ch_id, ctx['current_chapter_num'],
                        ctx.get('vol_index', 0), changes,
                    )
                    if changes_applied.get('applied'):
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
            except Exception:
                pass  # 回写失败不阻断
        if wp_runner: wp_runner.mark_stage(wp_graph, 't7_changes', 'done' if changes_applied else 'skipped')

        # ===== P0-1：确定性后写校验（非流式版，零 LLM 成本）=====
        # bible_ctx 覆盖全维度（死亡复活/境界回退/文风漂移检测）；漂移检测并入主校验分支
        post_validate = None
        if validate_chapter_with_bible:
            try:
                body_for_check = _extract_chapter_body(polished_content)
                bible_ctx = {
                    'character_profiles': bb.character_profiles or '',
                    'chapter_changes_log': bb.chapter_changes_log or '',
                    'key_rules': bb.key_rules or '',
                    'worldbuilding': bb.worldbuilding or '',
                    'inventory': bb.inventory or '',
                    'locations': bb.locations or '',
                    'foreshadowing': bb.foreshadowing or '',
                } if bb else None
                validation = validate_chapter_with_bible(body_for_check, bible_ctx)
                # 【P0修复】追加文风漂移检测：计算前5章基准，检测当前章是否漂移
                try:
                    style_baseline = _compute_style_baseline(book_id, ctx['current_chapter_num'])
                    if validate_chapter_with_drift and style_baseline:
                        drift_validation = validate_chapter_with_drift(body_for_check, style_baseline)
                        # 合并漂移检测的 issues 到主 validation
                        for issue in drift_validation.issues:
                            validation.issues.append(issue)
                            validation.score = max(0, validation.score - (5 if issue.severity == 'warning' else 20))
                        # 把漂移统计写入 stats
                        if drift_validation.stats.get('style_drift'):
                            validation.stats['style_drift'] = drift_validation.stats['style_drift']
                except Exception:
                    pass  # 漂移检测失败不影响主校验
                # 无论有没有问题，都把统计项(stats)抛给前端（段均句数/句均字数等健康指标）
                post_validate = validation.to_dict()
                if not validation.issues:
                    post_validate['_hardcard4_ok'] = True
            except Exception:
                pass
        elif validate_chapter:
            try:
                body_for_check = _extract_chapter_body(polished_content)
                validation = validate_chapter(body_for_check)
                # 无论有没有问题，都把统计项(stats)抛给前端监控（段均句数/句均字数/短碎句占比/段内≥3句号数）
                post_validate = validation.to_dict()
                if not validation.issues:
                    # 没命中问题时也要保留 stats 和 passed/score 摘要，前端可展示"健康值"
                    post_validate['_hardcard4_ok'] = True
            except Exception:
                pass
        if wp_runner: wp_runner.mark_stage(wp_graph, 't8_pval', 'done' if post_validate else 'skipped',
                                           {'issues': len((post_validate or {}).get('issues', []))})

        # ===== P2-10：落地门禁（3道，章节落库前拦截；延迟到 _extract_chapter_body 后传纯正文）=====
        gate_result = None

        # ===== P1-5：审计-修订闭环（校验→修订→再校验，最多2轮；编排已抽取至 chapter_review_cycle）=====
        review_cycle_result = None
        if (run_review_cycle and validate_chapter_with_bible and bb
                and post_validate and post_validate.get('issues')):
            try:
                polished_content, review_cycle_result = run_review_cycle_with_bible(
                    polished_content, bb, post_validate, book_id, ctx['current_chapter_num'],
                    api_key, base_url, model, _extract_chapter_body)
            except Exception:
                db.session.rollback()  # 闭环失败不阻断章节生成
        if wp_runner: wp_runner.mark_stage(wp_graph, 't9_cycle', 'done' if review_cycle_result else 'skipped',
                                           {'passed': (review_cycle_result or {}).get('passed')})

        # ===== 审校评分制：聚合 4 套检测结果计算 0-100 分（零 LLM 成本）=====
        try:
            polished_wc = len(re.sub(r'\s', '', polished_content or ''))
            chapter_score = _calc_chapter_score(
                post_validate, consistency_passed, consistency_issues,
                gate_result, polished_wc, ctx.get('chapter_plan', ''))
        except Exception:
            chapter_score = None

        # ===== 标题自动生成：解析【标题】标签，剥离正文中的标签行 =====
        suggested_title = _extract_chapter_title(draft_content)
        # 统一标题格式：第X章 标题文本（与连续创作模式一致，混用模式时格式统一）
        formatted_title = _format_chapter_title(ctx['current_chapter_num'], suggested_title)
        # ★ 统一清洗：无论 changes 是否解析成功，都剥离所有内部标签（pre_write_check / chapter_changes / 标题JSON / 【标题】行）
        # 确保返回给前端的 content 是纯净正文，避免内部产物泄露给用户
        polished_content = _extract_chapter_body(polished_content)
        polished_content = re.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

        # 【修复】落地门禁在 _extract_chapter_body 之后调用，传入纯正文
        # 防御性检查：去AI味/字数修正后若 polished_content 变空，跳过门禁调用避免误报"正文为空"
        if run_all_gates and polished_content and polished_content.strip():
            try:
                gate_result = run_all_gates(polished_content, bb, ctx['current_chapter_num'])
                if wp_runner: wp_runner.mark_stage(wp_graph, 't10_gates', 'done',
                                                   {'passed': gate_result.get('passed'), 'blocked': gate_result.get('blocked')})
                # S1：critical 默认 block；用户二次确认传 ignore_gates 则放行
                if gate_result.get('blocked') and not ignore_gates:
                    if wp_runner: wp_runner.persist_plan_log(wp_graph, 'generate_chapter', {'outcome': 'gate_blocked'})
                    _cache_info = ctx.get('_cache_info') if isinstance(ctx, dict) else None
                    _block_body = {
                        'gate_blocked': True,
                        'ignore_gates_required': True,
                        'content': polished_content,
                        'draft': draft_content if deai_status == 'success' else None,
                        'review_notes': review_notes,
                        'deai_status': deai_status,
                        'chapter_plan': ctx.get('chapter_plan', ''),
                        'current_chapter_num': ctx['current_chapter_num'],
                        'vol_index': ctx.get('vol_index', 0),
                        'vol_title': ctx.get('vol_chapter').title if ctx.get('vol_chapter') else '',
                        'temperature': temperature,
                        'gate_result': gate_result,
                        'block_reason': '落地门禁检测到 critical 问题（如正文过短/为空等）。默认拦截自动落库，'
                                        '请在前端确认「忽略门禁强制保存」后再次提交。',
                        'prompt_cache_info': _cache_info,
                        'cache_stats': _cache_stats_snapshot(),
                    }
                    return _response_with_cache(jsonify(_block_body), _cache_info), 428
                if not gate_result.get('passed'):
                    pass  # 仅 warning：不阻断
            except Exception:
                pass

        # 优化2：写作流水线轨迹持久化（plan_log_json，最近20条）+ 前端可观测
        pipeline_plan = None
        if wp_runner:
            wp_runner.mark_stage(wp_graph, 't11_post', 'declared', {'note': '落库后由 _after_chapter_persisted 执行'})
            wp_runner.persist_plan_log(wp_graph, 'generate_chapter', {'outcome': 'ok'})
            pipeline_plan = wp_graph.to_dict()

        return jsonify({
            'content': polished_content,
            'draft': draft_content if deai_status == 'success' else None,
            'review_notes': review_notes,
            'deai_status': deai_status,  # #6：新增可观测字段
            'chapter_plan': ctx.get('chapter_plan', ''),  # #4：返回计划供前端展示
            'current_chapter_num': ctx['current_chapter_num'],
            'vol_index': ctx.get('vol_index', 0),
            'vol_title': ctx['vol_chapter'].title if ctx.get('vol_chapter') else '',
            'temperature': temperature,  # #10：返回实际使用的 temperature
            'consistency_passed': consistency_passed,  # #13：一致性检查结果
            'consistency_issues': consistency_issues,
            # P0-1 + P1-6/7 新增字段
            'post_validate': post_validate,  # 后写校验报告（AI痕迹检测）
            'changes_applied': changes_applied,  # 章级变更回写摘要
            # P2-10 新增
            'gate_result': gate_result,  # 落地门禁结果
            # P1-5 新增
            'review_cycle': review_cycle_result,  # 审计-修订闭环结果
            # 审校评分制新增
            'chapter_score': chapter_score,  # 0-100 评分 + 等级 + 5维明细 + auto_revise
            # 标题自动生成新增
            'suggested_title': suggested_title,  # 纯标题文本（如"小镇少年"）
            'formatted_title': formatted_title,  # 统一格式标题（如"第1章 小镇少年"），前端直接使用
            # P2 新增：上下文溯源 manifest（记录本次生成注入了哪些 bible 片段 + hash + token 预算）
            'context_manifest': context_manifest_data,
            'pipeline_plan': pipeline_plan,  # 优化2：写作流水线任务图（12阶段执行轨迹）
            'prompt_cache_info': ctx.get('_cache_info', {'hit': False, 'tokens_saved': 0}) if isinstance(ctx, dict) else None,  # PromptCache命中信息
            'cache_stats': _cache_stats_snapshot(),  # 全局cache统计（hits/misses/tokens_saved）
        })
        # 加响应头：X-Prompt-Cache HIT/MISS + X-Tokens-Saved
        _cache_info = ctx.get('_cache_info') if isinstance(ctx, dict) else None
        return _response_with_cache(jsonify(result_body), _cache_info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@ai_continue_bp.route('/api/books/<book_id>/ai-spot-fix', methods=['POST'])
@login_required
def ai_spot_fix(book_id):
    """Spot-Fix 修订端点（P2-9）。
    按校验问题路由：local 类只修补问题段落（省 token），structural 类建议整章重写。
    前端"一键修订"按钮调用。"""
    if not route_revision:
        return jsonify({'error': '修订模块未加载'}), 500
    data = request.get_json() or {}
    content = data.get('content', '')
    validation = data.get('post_validate', {})
    mode = data.get('mode', 'auto')  # auto / spot_fix / rewrite
    if not content:
        return jsonify({'error': '缺少正文内容'}), 400

    # 路由修订策略
    routing = route_revision(content, validation, mode=mode)

    if routing['strategy'] == 'none':
        return jsonify({
            'strategy': 'none',
            'message': '未检测到需要修订的问题',
            'content': content,
        })

    if routing['strategy'] == 'rewrite':
        # structural 问题，建议整章重写（前端可调用 ai-continue）
        return jsonify({
            'strategy': 'rewrite',
            'message': '检测到结构性问题，建议整章重写',
            'structural_issues': routing['structural_issues'],
            'content': content,
        })

    # spot_fix 策略：只送问题段落给 LLM
    patches = routing['patches']
    if not patches:
        return jsonify({'strategy': 'none', 'message': '无法定位问题段落', 'content': content})

    # 构建 Spot-Fix prompt
    sys_prompt, user_prompt = build_spot_fix_prompt(content, patches)
    token_saving = estimate_token_saving(content, patches)

    # 调用 LLM 修订
    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('creation') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers=build_auth_headers(api_key),
            json={'model': model,
                  'messages': [{'role': 'system', 'content': sys_prompt},
                               {'role': 'user', 'content': user_prompt}],
                  'temperature': 0.3,
                  'max_tokens': 2000},
            timeout=60)
        result = resp.json()
        llm_output = result['choices'][0]['message']['content'].strip()

        # patch 回原文
        revised_content = apply_spot_fix_patches(content, patches, llm_output)

        # 对修订后的内容再跑一次后写校验
        post_validate = None
        if validate_chapter:
            try:
                body_for_check = _extract_chapter_body(revised_content)
                validation2 = validate_chapter(body_for_check)
                if validation2.issues:
                    post_validate = validation2.to_dict()
            except Exception:
                pass

        return jsonify({
            'strategy': 'spot_fix',
            'content': revised_content,
            'patches_count': len(patches),
            'token_saving': token_saving,
            'post_validate': post_validate,  # 修订后的校验报告
        })
    except Exception as e:
        return jsonify({'error': f'修订失败：{str(e)[:200]}'}), 500

@ai_continue_bp.route('/api/books/<book_id>/ai-continue/stream', methods=['POST'])
@login_required
def ai_continue_stream(book_id):
    """正文滚动创作流式版（#8：SSE 推送初稿）。
    前端可通过 EventSource 接收 chunks，审校与一致性检查在流结束后由前端单独触发或忽略。
    返回 text/event-stream，每个 chunk 形如 data: {"content": "..."}\n\n"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    instruction = request.json.get('instruction', '')
    skill_pack_ids = request.json.get('skill_pack_ids', [])
    # 修复上下文脱节：接收前端传入的待写章号 + 上一章未保存内容
    target_chapter_num = request.json.get('target_chapter_num')
    prev_chapter_content = request.json.get('prev_chapter_content')
    # 章节正文语言风格（行文文风，最多3个叠加）
    chapter_lang_styles = request.json.get('chapter_lang_styles', [])
    # Prompt 上下文缓存旁路
    skip_prompt_cache = bool(request.json.get('skip_prompt_cache', False) or request.json.get('skip_cache', False))

    ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num, prev_chapter_content, chapter_lang_styles, enable_structured_tags=True, skip_cache=skip_prompt_cache)
    api_key = ctx['api_key']
    base_url = ctx['base_url']
    model = ctx['model']

    # 【优化2】TaskGraph 统一编排（流式版）：写前任务图，LLM 阶段随流推进 mark_stage
    wp_graph = wp_runner = None
    try:
        from smart_planner import build_writing_pipeline
        wp_graph, wp_runner, _mission = build_writing_pipeline(book_id, bb, ctx['current_chapter_num'],
                                                               skill_pack_ids=skill_pack_ids)
    except Exception:
        wp_graph = wp_runner = None

    def generate():
        try:
            # 先推送元信息（计划、章号、卷信息、temperature）
            meta = {
                'meta': True,
                'chapter_plan': ctx.get('chapter_plan', ''),
                'current_chapter_num': ctx['current_chapter_num'],
                'vol_index': ctx.get('vol_index', 0),
                'vol_title': ctx['vol_chapter'].title if ctx.get('vol_chapter') else '',
                'temperature': ctx['temperature'],
            }
            yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'
            if wp_runner:  # 优化2：上下文构建+章节计划阶段完成
                wp_runner.mark_stage(wp_graph, 't2_ctx', 'done')
                wp_runner.mark_stage(wp_graph, 't2_plan', 'done' if ctx.get('chapter_plan') else 'skipped')

            # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
            system_prompt = ctx['system_prompt']

            # 流式生成正文初稿，收集完整内容用于后写校验（P0-1）
            # 【空回复修复1】非 200 显式报错（旧实现遍历错误页无 data 帧→流静默结束→前端"空回复"）
            full_content_parts = []
            resp = _post_llm_adaptive(api_key, base_url, model,
                {'model': model,
                 'messages': [{'role': 'system', 'content': system_prompt},
                              {'role': 'user', 'content': ctx['user_prompt']}],
                 'temperature': ctx['temperature'],
                 'max_tokens': ctx['max_tokens'],
                 'stream': True},
                stream=True, timeout=180)
            if resp.status_code != 200:
                _err_txt = ''
                try:
                    _err_txt = resp.text[:200]
                except Exception:
                    pass
                _err_msg = f"LLM 流式调用失败（HTTP {resp.status_code}）：{_err_txt or '服务返回错误，请检查 API Key/额度'}"
                yield f'data: {json.dumps({"error": _err_msg}, ensure_ascii=False)}\n\n'
                return
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        chunk = line[6:]
                        if chunk == '[DONE]':
                            yield 'data: [DONE]\n\n'
                            break
                        # 收集内容用于后写校验 + 统一格式转发前端
                        try:
                            chunk_data = json.loads(chunk)
                            # 兼容多种供应商格式：标准OpenAI / 简化delta / 直接content
                            delta = ''
                            try:
                                choices = chunk_data.get('choices') or []
                                if choices:
                                    delta = (choices[0].get('delta') or {}).get('content', '') or \
                                            (choices[0].get('message') or {}).get('content', '')
                            except Exception:
                                pass
                            if not delta:
                                delta = chunk_data.get('content') or chunk_data.get('text') or ''
                            if delta:
                                full_content_parts.append(delta)
                                # 统一为标准 OpenAI 格式转发，确保前端能正确解析
                                yield f'data: {json.dumps({"choices": [{"delta": {"content": delta}}]}, ensure_ascii=False)}\n\n'
                        except Exception:
                            pass  # 非 JSON 行跳过

            # 【空回复修复2】流结束但零内容帧 → 显式 error 帧替代静默结束（前端不再"空回复"）
            if not full_content_parts:
                yield f'data: {json.dumps({"error": f"LLM 返回空内容（model={model}，可能原因：max_tokens 过小/模型拒答/网关异常），请重试"}, ensure_ascii=False)}\n\n'
                return
            if wp_runner: wp_runner.mark_stage(wp_graph, 't3_draft', 'done',
                                               {'chars': sum(len(p) for p in full_content_parts)})

            # ===== P1-6 + P1-7：CHANGES 解析 + delta 回写（流结束后）=====
            # 解析 LLM 输出的 12 类变更声明，delta patch 到 dynamic_volumes/foreshadowing_graph
            if extract_changes and apply_chapter_changes and full_content_parts:
                try:
                    full_content_for_changes = ''.join(full_content_parts)
                    body, changes = extract_changes(full_content_for_changes)
                    if changes:
                        bb_obj = BookBible.query.filter_by(book_id=book_id).first()
                        if bb_obj:
                            ch_obj = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index.desc()).first()
                            ch_id = ch_obj.id if ch_obj else ''
                            apply_summary = apply_chapter_changes(
                                bb_obj, ch_id, ctx['current_chapter_num'],
                                ctx.get('vol_index', 0), changes,
                            )
                            if apply_summary.get('applied'):
                                try:
                                    db.session.commit()
                                except Exception:
                                    db.session.rollback()
                                # 推送回写摘要给前端
                                yield f'data: {json.dumps({"changes_applied": apply_summary}, ensure_ascii=False)}\n\n'
                except Exception as ce:
                    # 回写失败不阻断章节生成
                    pass
            if wp_runner: wp_runner.mark_stage(wp_graph, 't7_changes', 'done')

            # 【修复】流式模式补字数修正：初稿字数不在 2300-2500 时 AI 重写（推心跳告知前端）
            if full_content_parts:
                try:
                    raw_content = ''.join(full_content_parts)
                    body_for_wc = _extract_chapter_body(raw_content)
                    draft_wc = _count_cn_chars(body_for_wc)
                    if draft_wc < 2300 or draft_wc > 2500:
                        yield f'data: {json.dumps({"type": "heartbeat", "message": f"正在修正字数（初稿{draft_wc}字）..."}, ensure_ascii=False)}\n\n'
                        corrected, wc_note = _ensure_word_count(
                            body_for_wc, api_key, base_url, model, ctx['max_tokens'], ctx['current_chapter_num'])
                        if corrected and corrected.strip() and _count_cn_chars(corrected) != draft_wc:
                            # 推送修正后的完整正文给前端（替换初稿）
                            yield f'data: {json.dumps({"type": "word_count_corrected", "content": corrected, "note": wc_note}, ensure_ascii=False)}\n\n'
                            # 更新 full_content_parts 供后续去AI味/校验使用
                            full_content_parts = [corrected]
                except Exception:
                    pass  # 字数修正失败不阻断流式生成
            if wp_runner: wp_runner.mark_stage(wp_graph, 't4_wc', 'done', {'chars': _count_cn_chars(''.join(full_content_parts)) if full_content_parts else 0})

            # ===== P0-1：确定性后写校验（流结束后，零 LLM 成本，报告推前端供一键修订）=====
            # 注入 bb 上下文：死亡复活/境界回退/角色名错写硬伤检测
            _validator = validate_chapter_with_bible or validate_chapter
            if _validator and full_content_parts:
                try:
                    full_content = ''.join(full_content_parts)
                    # 剥离可能的 PRE_WRITE_CHECK 和 CHANGES 标签（P1-6 产物），只校验正文
                    body_for_check = _extract_chapter_body(full_content)
                    bible_ctx = None
                    if validate_chapter_with_bible and bb:
                        bible_ctx = {
                            'character_profiles': bb.character_profiles or '',
                            'chapter_changes_log': bb.chapter_changes_log or '',
                            'key_rules': bb.key_rules or '',
                            'worldbuilding': bb.worldbuilding or '',
                            'inventory': bb.inventory or '',
                            'locations': bb.locations or '',
                            'foreshadowing': bb.foreshadowing or '',
                        }
                    validation = _validator(body_for_check, bible_ctx) if bible_ctx else _validator(body_for_check)
                    if validation.issues:
                        yield f'data: {json.dumps({"post_validate": validation.to_dict()}, ensure_ascii=False)}\n\n'
                except Exception as ve:
                    # 校验失败不阻断章节生成
                    pass
            if wp_runner: wp_runner.mark_stage(wp_graph, 't8_pval', 'done' if full_content_parts else 'skipped')

            # ===== 【优化3】流式模式补齐落地门禁（与非流式对齐）=====
            # 流式不落库（前端确认后另行保存），gates 只做告警不阻断：blocked 时推 gate_blocked 帧
            if run_all_gates and full_content_parts:
                try:
                    _body_for_gates = _extract_chapter_body(''.join(full_content_parts))
                    if _body_for_gates and _body_for_gates.strip():
                        _gate = run_all_gates(_body_for_gates, bb, ctx['current_chapter_num'])
                        if wp_runner: wp_runner.mark_stage(wp_graph, 't10_gates', 'done',
                                                           {'passed': _gate.get('passed'), 'blocked': _gate.get('blocked')})
                        _gate_blocked = bool(_gate.get('blocked', False))
                        yield f'data: {json.dumps({"gate_result": _gate, "gate_blocked": _gate_blocked}, ensure_ascii=False)}\n\n'
                except Exception:
                    pass

            # ===== 标题自动生成：解析【标题】标签并推送（统一格式"第X章 标题"，前端优先 formatted_title）=====
            if full_content_parts:
                try:
                    full_content_for_title = ''.join(full_content_parts)
                    suggested_title = _extract_chapter_title(full_content_for_title)
                    formatted_title = _format_chapter_title(ctx['current_chapter_num'], suggested_title)
                    yield f'data: {json.dumps({"suggested_title": suggested_title, "formatted_title": formatted_title}, ensure_ascii=False)}\n\n'
                except Exception:
                    pass

            # 【P1-4修复】流式模式补去AI味 Agent：仅当有 review 类技能包时触发（与多Agent模式一致）
            try:
                review_skill_ids = _resolve_skill_ids_by_category(book, 'review') if book else []
                if review_skill_ids and full_content_parts:
                    full_content_for_deai = ''.join(full_content_parts)
                    body_for_deai = _extract_chapter_body(full_content_for_deai)
                    if body_for_deai and len(body_for_deai) > 200:
                        deai_skill_note = _get_skill_prompts_by_category(review_skill_ids, 'review', ['deai', 'consistency_check'])
                        if deai_skill_note:
                            deai_sys = ('你是去AI味审校专家。按以下技能包要求，对章节正文做最小改动修订，'
                                       '只调整AI痕迹和文风问题，不改变剧情、人物、设定。\n\n'
                                       f'{deai_skill_note}\n\n'
                                       '【输出】直接输出修订后的完整正文（含标题行），不要任何解释。')
                            deai_user = f'原文：\n{body_for_deai[:6000]}'
                            yield f'data: {json.dumps({"type": "deai_start"}, ensure_ascii=False)}\n\n'
                            deai_content, deai_err = _call_llm(
                                [{'role': 'system', 'content': deai_sys}, {'role': 'user', 'content': deai_user}],
                                max_tokens=0, temperature=0.5
                            )
                            if not deai_err and deai_content and deai_content.strip():
                                # 剥离标题行，提取正文
                                deai_body = _extract_chapter_body(deai_content)
                                if deai_body and len(deai_body) > 200:
                                    yield f'data: {json.dumps({"type": "deai_result", "content": deai_body}, ensure_ascii=False)}\n\n'
            except Exception:
                pass  # 去AI味失败不阻断流式生成
            # 优化2：t5_deai 收尾 + 任务图轨迹持久化 + 推送 pipeline_plan 终帧（前端可观测）
            if wp_runner:
                _deai_state = 'done' if (locals().get('deai_body') and len(locals().get('deai_body') or '') > 200) else 'skipped'
                wp_runner.mark_stage(wp_graph, 't5_deai', _deai_state)
                # 优化3：流式模式显式关闭重审校环节（由前端 Spot-Fix/保存后置触发），任务图不留悬空阶段
                wp_runner.mark_stage(wp_graph, 't6_cchk', 'skipped', {'note': '流式一致性检查由前端触发'})
                wp_runner.mark_stage(wp_graph, 't9_cycle', 'skipped', {'note': '流式审校闭环由前端 Spot-Fix 触发'})
                wp_runner.mark_stage(wp_graph, 't11_post', 'declared', {'note': '落库后由 _after_chapter_persisted 执行'})
                try:
                    wp_runner.persist_plan_log(wp_graph, 'generate_chapter', {'outcome': 'ok', 'mode': 'stream'})
                    yield f'data: {json.dumps({"pipeline_plan": wp_graph.to_dict()}, ensure_ascii=False)}\n\n'
                except Exception:
                    pass
        except Exception as e:
            yield f'data: {{"error": "{str(e)[:200]}"}}\n\n'

    return app.response_class(generate(), mimetype='text/event-stream')

@ai_continue_bp.route('/api/books/<book_id>/ai-continue-batch', methods=['POST'])
@login_required
def ai_continue_batch(book_id):
    """连续创作模式（优化版）：批量生成 N 章，每章独立生成，普通 JSON 响应。
    每章只调 1 次 LLM（字数修正/去AI味要求并入 system_prompt），不注入 prev_content；
    保留标题解析与一致性检查（默认关闭）。参数：{instruction, skill_pack_ids,
    chapter_lang_styles, count(1-10), start_chapter_num?}"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    instruction = data.get('instruction', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    chapter_lang_styles = data.get('chapter_lang_styles', [])
    count = max(1, min(10, int(data.get('count', 3))))  # 1-10 章
    start_chapter_num = data.get('start_chapter_num')
    # Prompt 上下文缓存旁路（批量：每章内部走同一指纹，用户改了未存库的内容时传 True 绕开）
    skip_prompt_cache = bool(data.get('skip_prompt_cache', False) or data.get('skip_cache', False))
    # S1：批量模式下，任意一章触发 critical 时的处理策略
    ignore_gates = bool(data.get('ignore_gates', False))

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    import re as _re_batch
    results = []
    failed = []  # Bug2 修复：记录失败章节，避免静默吞错导致剧情断档
    prev_polished = ''  # Bug5 修复：缓存上一章已生成正文，传递给下一章避免断档

    for i in range(count):
        try:
            # 章号：首章用 start_chapter_num 或自动计算，后续递增
            target_num = start_chapter_num + i if start_chapter_num else None
            # Bug5 修复：传递上一章已生成正文（数据库尚未保存或刚保存的场景都能承接剧情）
            ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids,
                                              target_num, prev_polished or None, chapter_lang_styles,
                                              skip_cache=skip_prompt_cache)
            api_key = ctx['api_key']
            base_url = ctx['base_url']
            model = ctx['model']
            cur_ch = ctx['current_chapter_num']

            # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
            # 去AI味禁词已由 chat_collab_bp.DEAI_RULES 统一负责，此处不再追加手工补丁（冗余）
            system_prompt = ctx['system_prompt']

            # Bug3 修复：LLM 调用添加状态码与结构检查，避免 KeyError 静默失败
            try:
                resp = _post_llm_adaptive(api_key, base_url, model,
                    {'model': model, 'messages': [{'role':'system','content':system_prompt},
                                                  {'role':'user','content':ctx['user_prompt']}],
                     'temperature': ctx['temperature'], 'max_tokens': ctx['max_tokens']},
                    timeout=180)
            except requests.exceptions.RequestException as re_err:
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 请求失败: {re_err}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': f'LLM 请求失败: {str(re_err)[:200]}'})
                break  # 【铁律】失败即停，避免后续章节无意义空跑

            if resp.status_code != 200:
                err_body = resp.text[:300] if hasattr(resp, 'text') else ''
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 返回 HTTP {resp.status_code}: {err_body}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': f'LLM HTTP {resp.status_code}'})
                break  # 【铁律】失败即停

            try:
                resp_json = resp.json()
                draft_content = resp_json['choices'][0]['message']['content']
            except (ValueError, KeyError, IndexError, TypeError) as parse_err:
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 返回结构异常: {parse_err}, body={resp.text[:300]}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回结构异常'})
                break  # 【铁律】失败即停

            if not draft_content or not draft_content.strip():
                failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回内容为空'})
                break  # 【铁律】内容为空即停，避免后续章节无上下文可衔接

            # 【修复】额外校验：剥离内部标签后仍有正文（防止 LLM 只输出标签，门禁误报"正文为空"）
            _pre_check_body = _extract_chapter_body(draft_content)
            _pre_check_body = _re_batch.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', _pre_check_body).strip()
            if not _pre_check_body:
                failed.append({'chapter_num': cur_ch, 'error': 'LLM 仅输出结构标签（pre_write_check/chapter_changes），无正文内容'})
                break  # 【铁律】失败即停

            polished_content = draft_content

            # 一致性检查（默认关闭）
            consistency_passed = True
            consistency_issues = ''

            # 标题自动生成：解析 JSON 标题，剥离正文标签行
            suggested_title = _extract_chapter_title(draft_content, draft_content)

            # Bug7 修复：先从 draft_content 提取 chapter_changes（暂存），等 Chapter flush 拿到 id 后再回写
            chapter_changes_data = None
            if extract_changes:
                try:
                    _, changes = extract_changes(draft_content)
                    if changes:
                        chapter_changes_data = changes
                except Exception:
                    db.session.rollback()

            polished_content = _extract_chapter_body(polished_content)
            # 额外剥离末尾的标题 JSON 块（_extract_chapter_body 未覆盖）
            polished_content = _re_batch.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

            # 【修复】batch模式补字数修正：与多Agent模式统一，初稿字数不在2300-2500时AI重写
            try:
                polished_content, wc_note = _ensure_word_count(
                    polished_content, api_key, base_url, model, ctx['max_tokens'], cur_ch)
            except Exception:
                wc_note = ''

            # Bug9：统一 count_words 统计字数；★连续模式也跑 post_validate（与多agent对齐）
            wc = count_words(polished_content)
            post_validate = None
            if validate_chapter_with_bible:
                try:
                    bible_ctx = {
                        'character_profiles': bb.character_profiles or '',
                        'chapter_changes_log': bb.chapter_changes_log or '',
                        'key_rules': bb.key_rules or '',
                        'worldbuilding': bb.worldbuilding or '',
                        'inventory': bb.inventory or '',
                        'locations': bb.locations or '',
                        'foreshadowing': bb.foreshadowing or '',
                    } if bb else None
                    validation = validate_chapter_with_bible(polished_content, bible_ctx)
                    # 【P0修复】追加文风漂移检测：与多Agent同步/单章模式一致，避免死代码
                    try:
                        style_baseline = _compute_style_baseline(book_id, cur_ch)
                        if validate_chapter_with_drift and style_baseline:
                            drift_validation = validate_chapter_with_drift(polished_content, style_baseline)
                            for issue in drift_validation.issues:
                                validation.issues.append(issue)
                                validation.score = max(0, validation.score - (5 if issue.severity == 'warning' else 20))
                            if drift_validation.stats.get('style_drift'):
                                validation.stats['style_drift'] = drift_validation.stats['style_drift']
                    except Exception:
                        pass
                    if validation.issues:
                        post_validate = validation.to_dict()
                except Exception:
                    pass

            # 校验报告
            chapter_score = _calc_chapter_score(post_validate, consistency_passed, consistency_issues, {}, wc, ctx.get('chapter_plan', ''))

            # Bug7 修复：先创建 Chapter 并 flush 拿到 ch.id，再回写 chapter_changes（chapter_id 不再为空串）
            # 统一标题格式：第X章 标题文本（与多Agent同步/流式模式一致）
            title = _format_chapter_title(cur_ch, suggested_title)
            max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
            # Bug1 修复：归卷直接设置 parent_id（用 ctx 中的 vol_chapter），不依赖末尾 resort
            parent_id = ctx['vol_chapter'].id if ctx.get('vol_chapter') else ''
            ch = Chapter(book_id=book_id, title=title, content=polished_content,
                         order_index=max_order + 1, is_volume=False,
                         parent_id=parent_id,
                         word_count=wc)
            db.session.add(ch)
            db.session.flush()  # 拿到 ch.id

            # Bug7 修复：用真实 ch.id 回写 chapter_changes（不再传空串）
            if chapter_changes_data and apply_chapter_changes:
                try:
                    apply_chapter_changes(
                        bb, ch.id, cur_ch,
                        ctx.get('vol_index', 0), chapter_changes_data,
                    )
                except Exception:
                    db.session.rollback()

            update_book_stats(book_id)
            db.session.commit()

            # 【P1-4修复】连续创作模式补落地门禁（与多Agent模式对齐，仅 warning 不阻断）
            # 防御性检查：polished_content 为空时跳过门禁，避免误报"正文为空"
            gate_result_batch = None
            if run_all_gates and polished_content and polished_content.strip():
                try:
                    gate_result_batch = run_all_gates(polished_content, bb, cur_ch)
                except Exception:
                    pass

            # Bug4 修复：每章保存后检查并自动生成动态报告（每5章触发），避免后续章节注入过时报告
            try:
                _check_and_auto_generate_report(book_id)
            except Exception:
                pass
            # 【P0-3】每 20 章自动触发防遗忘检查（daemon 线程，不阻塞批处理）
            try:
                _maybe_auto_trigger_anti_forget_check(book_id, cur_ch)
            except Exception:
                pass

            result_entry = {
                'chapter_num': cur_ch,
                'chapter_id': ch.id,
                'title': title,
                'content': polished_content,
                'word_count': wc,
            }
            if gate_result_batch and not gate_result_batch.get('passed'):
                result_entry['gate_warning'] = gate_result_batch
            # S1：批量模式 critical 命中时：
            # - 若 ignore_gates=False：整批回滚 + 返回 gate_blocked（前端弹窗让用户选择重试）
            # - 若 ignore_gates=True：照常写入，只附 gate_warning
            if gate_result_batch and gate_result_batch.get('blocked') and not ignore_gates:
                db.session.rollback()
                # 收集已完成的章节摘要一起返回，方便用户知道是哪一章触发
                return jsonify({
                    'gate_blocked': True,
                    'ignore_gates_required': True,
                    'blocked_at_chapter': cur_ch,
                    'block_reason': (
                        f'批量创作第{cur_ch}章触发落地门禁 critical，为避免半成品写入已回滚整批。'
                        '请在前端勾选「忽略门禁强制保存」后再次提交整批。'
                    ),
                    'gate_result': gate_result_batch,
                    'partial_results': [{'chapter_num': r['chapter_num'], 'title': r['title'],
                                         'word_count': r.get('word_count', 0)} for r in results],
                }), 428
            results.append(result_entry)
            # Bug5 修复：缓存本章正文供下一章承接
            prev_polished = polished_content
        except Exception as e:
            # Bug2 修复：单章失败记录日志与失败列表，避免静默吞错导致剧情断档
            fail_num = start_chapter_num + i if start_chapter_num else i + 1
            try:
                app.logger.error(f'ai_continue_batch 第{fail_num}章生成失败: {e}', exc_info=True)
            except Exception:
                pass
            db.session.rollback()
            failed.append({'chapter_num': fail_num, 'error': str(e)[:200]})
            # Bug5：失败时清空 prev_polished，避免把失败章当上一章注入
            prev_polished = ''
            break  # 【铁律】异常即停，避免错误级联放大

    # Bug1 修复：不再调 resort_chapters_by_title(rebin_volumes=True)——批处理标题可能无"第X章"
    # 前缀致混合排序错乱，且 rebin 会覆盖自定义卷名；章节已按 order_index=max+1 顺序保存
    return jsonify({
        'chapters': results,
        'total': len(results),
        'failed': failed,
        'failed_count': len(failed),
    })

@ai_continue_bp.route('/api/books/<book_id>/ai-continue-batch/stream', methods=['POST'])
@login_required
def ai_continue_batch_stream(book_id):
    """连续创作流式版（SSE）：解决 Render 同步请求超时（约100s）导致 Failed to fetch。
    每章 LLM 调用 stream 模式逐 chunk 收集并定期推心跳保活；事件类型：
    chapter_start / heartbeat(每5s防空闲超时) / chapter_done(含完整章节) /
    chapter_failed / batch_done / error(致命终止)。"""
    import time as _time

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    instruction = data.get('instruction', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    chapter_lang_styles = data.get('chapter_lang_styles', [])
    count = max(1, min(10, int(data.get('count', 3))))
    start_chapter_num = data.get('start_chapter_num')
    # Prompt 上下文缓存旁路
    skip_prompt_cache = bool(data.get('skip_prompt_cache', False) or data.get('skip_cache', False))
    # S1：流式批量模式下，critical 命中时推送 gate_blocked 事件并提前结束
    ignore_gates = bool(data.get('ignore_gates', False))

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    def generate():
        import re as _re_bs
        results = []
        failed = []
        prev_polished = ''

        for i in range(count):
            try:
                target_num = start_chapter_num + i if start_chapter_num else None
                # 先推送 chapter_start（用预估章号），让 Render 立即收到首字节，
                # 避免 _build_ai_continue_context 内 _generate_chapter_plan 调 LLM 阻塞 20-30s 期间无数据导致断开
                yield f'data: {json.dumps({"type": "chapter_start", "chapter_num": target_num or (i + 1), "message": f"正在准备第{target_num or (i + 1)}章上下文..."}, ensure_ascii=False)}\n\n'

                ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids,
                                                  target_num, prev_polished or None, chapter_lang_styles,
                                                  skip_chapter_plan=True, skip_cache=skip_prompt_cache)
                api_key = ctx['api_key']
                base_url = ctx['base_url']
                model = ctx['model']
                cur_ch = ctx['current_chapter_num']

                # 章号可能与预估不同（后端基于 max(order_index)+1 重新计算），推送修正事件
                if cur_ch != (target_num or (i + 1)):
                    yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在生成第{cur_ch}章..."}, ensure_ascii=False)}\n\n'

                # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
                # 去AI味禁词已由 chat_collab_bp.DEAI_RULES 统一负责，此处不再追加手工补丁（冗余）
                system_prompt = ctx['system_prompt']

                # LLM 流式调用：逐 chunk 收集，定期推送心跳保持连接活跃
                full_parts = []
                last_heartbeat = _time.time()
                try:
                    resp = _post_llm_adaptive(api_key, base_url, model,
                        {'model': model, 'messages': [{'role':'system','content':system_prompt},
                                                      {'role':'user','content':ctx['user_prompt']}],
                         'temperature': ctx['temperature'], 'max_tokens': ctx['max_tokens'],
                         'stream': True},
                        stream=True, timeout=180)
                except requests.exceptions.RequestException as re_err:
                    try:
                        app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 LLM 请求失败: {re_err}')
                    except Exception:
                        pass
                    failed.append({'chapter_num': cur_ch, 'error': f'LLM 请求失败: {str(re_err)[:200]}'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": f"LLM 请求失败"}, ensure_ascii=False)}\n\n'
                    # 【铁律】前面章节失败，后续章节自动停止，避免连续生成无意义空章节
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 请求失败，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                if resp.status_code != 200:
                    err_body = resp.text[:300] if hasattr(resp, 'text') else ''
                    try:
                        app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 LLM HTTP {resp.status_code}: {err_body}')
                    except Exception:
                        pass
                    failed.append({'chapter_num': cur_ch, 'error': f'LLM HTTP {resp.status_code}'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": f"LLM HTTP {resp.status_code}"}, ensure_ascii=False)}\n\n'
                    # 【铁律】前面章节失败，后续章节自动停止
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 返回 HTTP {resp.status_code}，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                # 后台线程读取流式响应，主生成器从队列消费，无数据时 yield 心跳
                # 修复 network error：iter_lines() 阻塞时心跳无法推送
                for evt in _stream_llm_chunks_with_heartbeat(resp, cur_ch, last_heartbeat):
                    if evt[0] == 'chunk':
                        full_parts.append(evt[1])
                        last_heartbeat = _time.time()
                    elif evt[0] == 'heartbeat':
                        yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在生成第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                        last_heartbeat = _time.time()
                    elif evt[0] == 'error':
                        raise evt[1]
                    elif evt[0] == 'done':
                        break
                try:
                    resp.close()
                except Exception:
                    pass

                draft_content = ''.join(full_parts)
                if not draft_content or not draft_content.strip():
                    failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回内容为空'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": "LLM 返回内容为空"}, ensure_ascii=False)}\n\n'
                    # 【铁律】LLM 返回内容为空，后续章节自动停止（空章节无上下文可衔接，继续生成只会产出更多空内容）
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 返回内容为空，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                # 【修复】额外校验：剥离内部标签后是否仍有正文（防止 LLM 只输出标签而无正文，
                # 后续 _extract_chapter_body 后变空，门禁误报"正文为空"）
                _pre_check_body = _extract_chapter_body(draft_content)
                _pre_check_body = _re_bs.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', _pre_check_body).strip()
                if not _pre_check_body:
                    err_msg = 'LLM 仅输出结构标签（pre_write_check/chapter_changes），无正文内容'
                    failed.append({'chapter_num': cur_ch, 'error': err_msg})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": err_msg}, ensure_ascii=False)}\n\n'
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 {err_msg}，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                polished_content = draft_content
                suggested_title = _extract_chapter_title(draft_content, draft_content)

                # 提取 chapter_changes（暂存，待 flush 后回写）
                chapter_changes_data = None
                if extract_changes:
                    try:
                        _, changes = extract_changes(draft_content)
                        if changes:
                            chapter_changes_data = changes
                    except Exception:
                        db.session.rollback()

                polished_content = _extract_chapter_body(polished_content)
                polished_content = _re_bs.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

                # ===== 去AI味审校 Agent（选项A：批处理也跑去AI味修正，与单章模式对齐）=====
                # 2026-08-23 默认启用：内置统一去AI规则（build_review_rules）常驻，
                # 不再依赖技能包勾选；审查类(review)技能包作为增强叠加。
                # 字数校验通过→用修正版；字数异常/失败→回滚用初稿
                deai_status = 'skipped'  # skipped / rules_ok / rules_missing / success / failed
                deai_rules_block = ''
                try:
                    deai_rules_block, _deai_build_status = _build_deai_rules_block(skill_pack_ids, book)
                    deai_status = 'rules_ok' if _deai_build_status == 'ok' else 'rules_missing'
                except Exception as _deai_e:
                    # helper 也兜不住（连 DEAI_ONLY_RULES import 都失败），显式标 missing 而非静默
                    app.logger.error(f'ai_continue_batch_stream 去AI规则构建失败: {_deai_e}')
                    print(f'[去AI] ai_continue_batch_stream 去AI规则构建失败: {_deai_e}', file=sys.stderr)
                    deai_status = 'rules_missing'
                if deai_rules_block:
                    # 推送心跳：去AI味审校中
                    yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在去AI味审校第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                    deai_system = ("你是番茄去AI味审查员。对以下刚写好的章节正文做去AI味审校，按规则修改后只输出修改后的正文。\n\n"
                                   + deai_rules_block
                                   + "\n\n【硬性约束】修改后字数仍须 2400±100（2300-2500区间，含标点），保留原章节的剧情走向和钩子，只改文风不改剧情。")
                    try:
                        deai_hb = f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在去AI味审校第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                        deai_resp = yield from _run_blocking_with_heartbeat(
                            lambda: _post_llm_adaptive(api_key, base_url, model,
                                {'model': model,
                                 'messages': [{'role':'system','content':deai_system},
                                              {'role':'user','content':f'请审校以下章节正文：\n\n{polished_content}'}],
                                 'temperature': 0.5, 'max_tokens': ctx['max_tokens']},
                                timeout=180),
                            deai_hb)
                        if deai_resp.status_code == 200:
                            deai_result = deai_resp.json()
                            deai_polished = deai_result['choices'][0]['message']['content'].strip()
                            # 剥离可能的内部标签（去AI味 LLM 偶尔会带上）
                            deai_polished = _extract_chapter_body(deai_polished)
                            deai_polished = _re_bs.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', deai_polished).rstrip()
                            deai_wc = count_words(deai_polished)
                            if deai_polished and 2300 <= deai_wc <= 2500:
                                polished_content = deai_polished
                                deai_status = 'success'
                            elif deai_polished and deai_wc > 500:
                                deai_status = 'failed'  # 字数异常，回滚用初稿
                            else:
                                deai_status = 'failed'  # 内容为空，回滚用初稿
                        else:
                            deai_status = 'failed'
                            try:
                                app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 去AI味 HTTP {deai_resp.status_code}')
                            except Exception:
                                pass
                    except Exception as deai_err:
                        deai_status = 'failed'
                        try:
                            app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 去AI味异常: {deai_err}')
                        except Exception:
                            pass

                # 【修复】batch_stream模式补字数修正：与多Agent模式统一
                try:
                    polished_content, wc_note_bs = _ensure_word_count(
                        polished_content, api_key, base_url, model, ctx['max_tokens'], cur_ch)
                except Exception:
                    wc_note_bs = ''

                wc = count_words(polished_content)

                # post_validate（去AI味检测）
                post_validate = None
                if validate_chapter_with_bible:
                    try:
                        bible_ctx = {
                            'character_profiles': bb.character_profiles or '',
                            'chapter_changes_log': bb.chapter_changes_log or '',
                            'key_rules': bb.key_rules or '',
                            'worldbuilding': bb.worldbuilding or '',
                            'inventory': bb.inventory or '',
                            'locations': bb.locations or '',
                            'foreshadowing': bb.foreshadowing or '',
                        } if bb else None
                        validation = validate_chapter_with_bible(polished_content, bible_ctx)
                        # 【P0修复】追加文风漂移检测：与多Agent同步/连续同步模式一致，避免死代码
                        try:
                            style_baseline = _compute_style_baseline(book_id, cur_ch)
                            if validate_chapter_with_drift and style_baseline:
                                drift_validation = validate_chapter_with_drift(polished_content, style_baseline)
                                for issue in drift_validation.issues:
                                    validation.issues.append(issue)
                                    validation.score = max(0, validation.score - (5 if issue.severity == 'warning' else 20))
                                if drift_validation.stats.get('style_drift'):
                                    validation.stats['style_drift'] = drift_validation.stats['style_drift']
                        except Exception:
                            pass
                        if validation.issues:
                            post_validate = validation.to_dict()
                    except Exception:
                        pass

                chapter_score = _calc_chapter_score(post_validate, True, '', {}, wc, ctx.get('chapter_plan', ''))

                # 创建 Chapter 并 flush 拿 id
                # 统一标题格式：第X章 标题文本（与多Agent同步/连续同步模式一致）
                title = _format_chapter_title(cur_ch, suggested_title)
                max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
                parent_id = ctx['vol_chapter'].id if ctx.get('vol_chapter') else ''
                ch = Chapter(book_id=book_id, title=title, content=polished_content,
                             order_index=max_order + 1, is_volume=False,
                             parent_id=parent_id,
                             word_count=wc)
                db.session.add(ch)
                db.session.flush()

                # 用真实 ch.id 回写 chapter_changes
                if chapter_changes_data and apply_chapter_changes:
                    try:
                        apply_chapter_changes(bb, ch.id, cur_ch, ctx.get('vol_index', 0), chapter_changes_data)
                    except Exception:
                        db.session.rollback()

                update_book_stats(book_id)
                db.session.commit()

                # 【P1-4修复】批处理流式补落地门禁（与多Agent模式对齐，仅 warning 不阻断）
                # 防御性检查：polished_content 为空时跳过门禁，避免误报"正文为空"
                gate_result_bstream = None
                if run_all_gates and polished_content and polished_content.strip():
                    try:
                        gate_result_bstream = run_all_gates(polished_content, bb, cur_ch)
                    except Exception:
                        pass

                # 每章后检查自动生成动态报告（可能触发 LLM 调用，用线程+心跳避免阻塞）
                try:
                    report_hb = f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在更新动态报告..."}, ensure_ascii=False)}\n\n'
                    yield from _run_blocking_with_heartbeat(
                        lambda: _check_and_auto_generate_report(book_id),
                        report_hb)
                except Exception:
                    pass
                # 【P0-3】每 20 章自动触发防遗忘检查（daemon 线程，不阻塞 SSE 流）
                try:
                    _maybe_auto_trigger_anti_forget_check(book_id, cur_ch)
                except Exception:
                    pass

                chapter_info = {
                    'chapter_num': cur_ch,
                    'chapter_id': ch.id,
                    'title': title,
                    'content': polished_content,
                    'word_count': wc,
                    'deai_status': deai_status,  # skipped/success/failed，告知前端是否做过去AI味修正
                }
                if gate_result_bstream and not gate_result_bstream.get('passed'):
                    chapter_info['gate_warning'] = gate_result_bstream
                # S1：流式批量模式 critical 命中：
                # - 回滚本批 DB 改动 + 推送 gate_blocked 事件 + 结束流
                if gate_result_bstream and gate_result_bstream.get('blocked') and not ignore_gates:
                    try: db.session.rollback()
                    except Exception: pass
                    yield f'data: {json.dumps({"type": "gate_blocked", "ignore_gates_required": True, "chapter_num": cur_ch, "gate_result": gate_result_bstream, "block_reason": f"第{cur_ch}章触发落地门禁 critical，已回滚本批写入。勾选忽略门禁后再提交。"}, ensure_ascii=False)}\n\n'
                    # 直接结束流
                    yield f'data: {json.dumps({"type": "batch_done", "total": i, "failed_count": 1, "failed": [{"chapter_num": cur_ch, "error": "gate_blocked"}]}, ensure_ascii=False)}\n\n'
                    return
                results.append(chapter_info)
                prev_polished = polished_content

                # 推送章节完成事件
                yield f'data: {json.dumps({"type": "chapter_done", "chapter": chapter_info}, ensure_ascii=False)}\n\n'

            except Exception as e:
                fail_num = start_chapter_num + i if start_chapter_num else i + 1
                try:
                    app.logger.error(f'ai_continue_batch_stream 第{fail_num}章生成失败: {e}', exc_info=True)
                except Exception:
                    pass
                db.session.rollback()
                failed.append({'chapter_num': fail_num, 'error': str(e)[:200]})
                yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": fail_num, "error": str(e)[:200]}, ensure_ascii=False)}\n\n'
                # 【铁律】章节生成异常，后续章节自动停止，避免错误级联放大
                yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{fail_num}章生成异常：{str(e)[:100]}，后续章节已自动停止", "failed_chapter": fail_num}, ensure_ascii=False)}\n\n'
                prev_polished = ''
                break

        # 推送批处理完成事件
        yield f'data: {json.dumps({"type": "batch_done", "total": len(results), "failed_count": len(failed), "failed": failed}, ensure_ascii=False)}\n\n'

    # stream_with_context 保住应用上下文（多章串行含大量 DB 操作，yield 后丢上下文会 network error）；
    # 响应头禁缓存/禁代理缓冲（Cloudflare/Render 默认缓冲 SSE 致浏览器长时间收不到数据）
    resp = app.response_class(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp

