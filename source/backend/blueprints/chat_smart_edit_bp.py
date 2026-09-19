"""【维度编辑·批量·去AI·校审域】（自 chat_collab_bp.py 拆出，架构门禁 P1-6）。
涵盖：smart_dim_edit / smart_batch / smart_latest_chapter / smart_deai_packs /
smart_deai / smart_style_align / smart_review / smart_volumes / smart_chapters /
smart_chapter_replace。
共享符号由 chat_collab_bp.py 末尾 _register_split_domains() 调用 init() 注入。"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Response, jsonify, request, stream_with_context

from sse_keepalive import gw_stream_with_hb, SSE_HEARTBEAT_COMMENT, HEARTBEAT, _is_stream_retry
from session_persist import load_session_messages, _safe_save_session_messages, _save_partial_on_disconnect


def init(**deps):
    """chat_collab_bp 加载到文件末尾时调用：把共享符号注入本模块全局命名空间。"""
    globals().update(deps)


def register(bp):
    """把本域路由挂到 chat_collab_bp（endpoint 默认取函数名，与装饰器注册一致）。"""
    bp.add_url_rule('/api/ai/smart/dim-edit', view_func=smart_dim_edit, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/batch', view_func=smart_batch, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/latest-chapter', view_func=smart_latest_chapter, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/deai-packs', view_func=smart_deai_packs, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/deai', view_func=smart_deai, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/style-align', view_func=smart_style_align, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/review', view_func=smart_review, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/volumes', view_func=smart_volumes, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/chapters', view_func=smart_chapters, methods=['GET'])
    bp.add_url_rule('/api/ai/smart/chapter-replace', view_func=smart_chapter_replace, methods=['POST'])

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
    # P0 榜单风向：前端扫榜结果 rank_scan 注入（卡片溯源元数据用；【NameError 修复】此前
    # 下方 _enrich_card_rank_meta(card, _rank_scan) 引用了从未定义的 _rank_scan，
    # 生成卡片时直接 NameError → SSE error 帧"name '_rank_scan' is not defined"）
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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

    # 【纯文字铁律】非 timeline 维度：current_content 是 JSON 时（如剧情线卷结构被误存进大纲）
    # 先转纯文本再进 prompt——否则修订铁律"保留原文整体结构"会让 LLM 忠实保持 JSON 符号输出
    current_content = _plain_json_fallback(dim_key, current_content)

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
                    # 【纯文字铁律】非 timeline 维度兜底：LLM 仍输出 JSON 时转纯文本
                    cleaned = _plain_json_fallback(dim_key, cleaned)
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
    # P0 榜单风向：前端扫榜结果 rank_scan 注入（卡片溯源元数据用；【NameError 修复】同 dim-edit，
    # 下方两处 _enrich_card_rank_meta 引用了从未定义的 _rank_scan，批量生成卡片时 NameError）
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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
                            # 【纯文字铁律】非 timeline 维度兜底：LLM 仍输出 JSON 时转纯文本
                            cleaned = _plain_json_fallback(dim_key, cleaned)
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
                # 【纯文字铁律】JSON 存储维度注入前一律转自然语言（人物/剧情线），防模仿 JSON
                if d['key'] == 'character_profiles' and v.startswith('['):
                    v = _character_profiles_to_text(v)
                elif d['key'] == 'timeline' and (v.startswith('[') or v.startswith('{')):
                    v = _json_to_plain_text(v)
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
            max_tok = _DIM_MAX_TOKENS  # 去AI味整章重写：按模型能力给足（旧 4096 会截断长章）
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


