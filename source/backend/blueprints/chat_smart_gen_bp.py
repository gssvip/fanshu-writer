"""【智能创作生成域】（自 chat_collab_bp.py 拆出，架构门禁 P1-6）。
核心生成链路：smart_general / smart_suggest / smart_generate。
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
    bp.add_url_rule('/api/ai/smart/general', view_func=smart_general, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/suggest', view_func=smart_suggest, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/generate', view_func=smart_generate, methods=['POST'])

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
    # P0 榜单风向：前端先扫榜把 rank_scan 塞进来；注入 system prompt + 卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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

    # ===== 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）=====
    conception_rules = build_conception_rules(skill_pack_ids, mode='agent')

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting', title='通用聊天')
    session_id = session.id

    # ===== 复用 chat_smart 的 system prompt + TOC + 定位铁律（核心）=====
    toc_block = _build_toc_block(book_id)
    base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block, rank_scan=_rank_scan)

    # 通用聊天专属追加：构思专属规则 + 关键词命中卡片产出提示 + 增强索要资料禁令（第二保险）
    extra_parts = []
    if conception_rules:
        extra_parts.append(f'\n【构思阶段·平台内置规则+技能包方法论】\n{conception_rules}')
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

    # ===== 写正文意图·注入正文行文规范（与 chat_smart 同源）=====
    # 设定通用Tab(smart_general)默认只注入构思规则(GENERAL_CORE_RULES+构思格式)，不注入
    # WRITING_STYLE_RULES（设计见 build_chat_system_prompt）。但当用户在此 Tab 明确要求
    # "写第X章/写正文/接着写"时，同样需要命中正文行文规范，否则产出的 SAVE_CHAPTER 正文卡
    # 会脱离行文/去AI硬卡约束。与 chat_smart / chat_smart_action / ai_continue 保持同源注入。
    if _is_write_chapter_intent(message):
        try:
            _general_chapter_rules = build_chat_chapter_rules(book, mode='agent')
            if _general_chapter_rules:
                sys_prompt = sys_prompt + '\n\n' + _general_chapter_rules
        except Exception:
            pass  # 注入失败不阻断主流程

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
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        full = []
        try:
            # SSE 首帧 meta：命中章节/维度（前端提示"已定位并注入"）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield sse({'type': 'meta', 'kind': 'auto_context', 'info': auto_ctx_info})

            # 智驾通用生成 max_tokens 完全不设限（同 chat_smart：None → 不发送字段，
            # 避免 max_tokens 被网关当配额预留额度而秒撞 TPM 限流）
            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=None):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                if _is_stream_retry(chunk):
                    yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
                    continue
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
                    # 【纯文字铁律】非剧情线卡片兜底：LLM 仍输出 JSON 时转纯文本
                    card['content'] = _plain_json_fallback(card.get('type') or '', card['content'])
                    if card.get('title'):
                        card['title'] = _clean_text_to_plain(card['title'])
                _enrich_card_rank_meta(card, _rank_scan)
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
            # 历史里保存作者原话（不保存注入引用块，避免多轮重复上下文）
            history = load_session_messages(session)
            history.append({'role': 'user', 'content': message})
            # 历史保存的卡片也同步 enrich，后续复盘/多轮时仍带风向标签
            for c in (cards or []):
                _enrich_card_rank_meta(c, _rank_scan)
            history.append({'role': 'assistant', 'content': clean_content,
                            'cards': [{**c, 'status': 'pending'} for c in cards] if cards else None})
            _safe_save_session_messages(session, history)
            yield sse({'type': 'done', 'session_id': session_id})
        except Exception as e:
            yield sse({'type': 'error', 'error': str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


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
    # P0 榜单风向：前端扫榜结果 rank_scan 注入（可选；没扫则不注入）
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

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

    # ===== 【direct 模式·执行性展开维度】上游必填依赖已完善时不再出多方案 =====
    # 上游（构思的金手指/世界观卖点/主角反派框架、大纲的每卷目标）已把方向锁死，
    # 此时多方案是伪选择：LLM 会在不同方案里各换一套体系方向，选定后与已定上游打架
    # = 拼凑感根源之一。→ 直接返回固定卡片（不调 LLM，零成本零延迟），
    # 前端点卡片走 smart_generate 直生；不满意整体重生成（reroll，换角度不换方向）。
    # 依赖未完善时保留多方案作为探索模式兜底；用户贴了自己的完整内容时走原流程。
    if spec.get('mode') == 'direct' and not user_paste:
        _dep_req = DIMENSION_DEPENDENCIES.get(dim_key, {}).get('required', [])
        _direct_ready = True
        try:
            _direct_ready = check_dim_readiness(bb, dim_key).get('ready', True)
        except Exception:
            _direct_ready = True  # 就绪检查异常不阻断，宁可直生也不让作者卡住
        if _direct_ready:
            _dep_labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in _dep_req if k in _DIM_KEY_TO_SPEC) or '上游设定'
            _direct_hints = {
                'key_rules': '力量体系/等级阶梯/经济数值严格按构思第六节金手指方向展开，禁止另起体系',
                'worldbuilding': '地理/势力/历史严格按构思第九节世界观卖点钩子展开，禁止另起世界观',
                'plot_design': '五幕式总纲严格按已定构思的故事核与已锁卷数展开：每卷目标/冲突/卷尾钩子全部服务于构思主线，禁止另起故事方向',
                'character_profiles': '主角严格按构思第七节魅力公式、反派按第八节框架展开，角色功能位与弧线锚定大纲各卷目标，禁止换人设方向',
                'timeline': '各卷剧情严格按大纲每卷目标/冲突/卷尾钩子展开，禁止偏离五幕框架',
                'foreshadowing': '伏笔埋设/回收按大纲与剧情节点派生，禁止凭空新开主线级伏笔',
                'locations': '地点严格按世界观地理分块与势力分布派生，禁止另起地名体系',
            }
            _direct_meta = {'direct_mode': True}
            if params_sync_notes:
                _direct_meta['params_sync'] = params_sync_notes
            return jsonify({
                'suggestions': [{
                    'id': 'sug_1',
                    'title': f'基于已定{_dep_labels}直接生成',
                    'preview': (f'本维度为执行性展开，方向已由{_dep_labels}锁定：'
                                + _direct_hints.get(dim_key, '基于已定上游方向直接展开')
                                + '。生成后不满意可整体重新生成（换展开角度，方向不变）；需调整局部请在生成后对内容提修改意见。'),
                }],
                'dimension': dim_key,
                'dimension_label': spec['label'],
                'requirement': requirement,
                'meta': _direct_meta,
            })

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

    # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽文风/去AI规则）
    skill_note = build_conception_rules(skill_pack_ids, mode='single')

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

    # 文风维度专属：把「行文文风」菜单注入多方案，要求每套方案点名采用哪种文风并提供差异化选择
    style_extra = ''
    if dim_key == 'style_guide':
        try:
            from app import CHAPTER_LANG_STYLES as _lang_styles
        except Exception:
            _lang_styles = {}
        _style_menu_lines = [f'- {t[0]}：{t[1]}' for t in _lang_styles.values() if isinstance(t, (tuple, list)) and t[0] and t[1]]
        _style_menu = '\n'.join(_style_menu_lines) if _style_menu_lines else '（通用/白描/幽默/爽文/古风…等）'
        style_extra = f"""
【文风维度专属要求·必须点名行文文风】本书提供以下「行文文风」菜单（可单选，也可选 2 种组合成"基调+点缀"）：
{_style_menu}

你生成的每一个方案 preview 必须**点名**该方案主用的「行文文风」（用上面菜单里的中文名，如"幽默+市井""冷硬白描""古风雅致""爽文节奏"），并具体说明这种文风如何落到本书的叙事口吻、节奏把控、对白密度里。
3-5 个方案必须在「行文文风」上做出**明确差异**（不同文风组合=不同方案方向），严禁所有方案都用同一种文风。
每个方案 preview 末尾固定追加一行「文风方案：XXX」，标出该方案所用的文风组合，方便作者一眼对比选择。"""

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
{outline_extra}{af_alerts_suggest}{suggest_iron_rule}{preview_volume_req}{style_extra}

{_skill_note_block}

【重要·方案卡格式】请生成 3-5 个不同切入角度的方案。严格按以下 JSON 格式输出（不要任何其他内容、不要 Markdown 代码块，不要解释性文字，不要思考过程，不要规则复述，不要英语）：
{{
  "suggestions": [
    {{"title": "方案标题（10字内，中文，说明该方案核心卖点）", "preview": "方案简介（120-220字，全中文，直接对读者讲清：故事核/核心设定差异/爽点钩子，绝不提本prompt里的任何规则/格式/自检要求）"}}
  ]
}}

【P0 禁令·违者作废】
1. 严禁在 title/preview 里出现任何英语（如 key requirements / theme / each preview must / I'm looking at 等）、严禁中英文混杂；
2. 严禁把本 prompt 里的规则、自检要求、格式说明、"30卷"字样的约束语句当方案内容复述；
3. 严禁 preview 用短于 80 字的占位句子凑数（如"方案二：方案2"），每一条 preview 都必须是完整的中文创意简介；
4. 每条方案必须是"差异化创意"——不能是5条把同一个创意换个同义词重写，要有题材/主角身份/金手指形态/切入视角/核心冲突形态上的明确差异。

【最终自检（输出前必过）】
1. 若本任务属于大纲/剧情/构思维度，每条 preview 必须显式包含"{tv_for_suggest if (tv_for_suggest and tv_for_suggest>=1) else '__'}卷"字样，不得写"十卷""五卷"等默认数字；
2. 所有 preview 检查一遍：有没有英语？有没有复述本 prompt 里的规则/自检/格式说明？字数够 120-220？中文通顺？
3. JSON 合法：无多余逗号，suggestions 长度 3-5，数组元素只含 title 与 preview。"""

    # P0 榜单风向：把扫榜情报追加到 system_prompt。有多方案场景要求标题/卖点/钩子优先贴合。
    _rank_ctx = _format_rank_context(_rank_scan)
    if _rank_ctx:
        sys_prompt = sys_prompt.rstrip() + '\n\n' + _rank_ctx

    messages = [{'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': f'请生成{spec["label"]}的多选方案'}]

    from app import _call_llm
    content, err = _call_llm(messages, max_tokens=2000, temperature=0.85, task_type='creation')
    if err:
        return jsonify({'error': f'生成方案失败：{err}'}), 500

    suggestions = []
    _raw = content or ''
    try:
        m = re.search(r'\{[\s\S]*\}', _raw)
        if m:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                # 支持 suggestions / result / data 等多种外层字段
                for _k in ('suggestions', 'result', 'data', 'items', 'list'):
                    v = parsed.get(_k)
                    if isinstance(v, list):
                        suggestions = v
                        break
    except Exception:
        pass

    # ===== 【P0兜底·用户截图根因】：模型把prompt规则原文/英语/占位句子当方案吐了，必须全打掉 =====
    _BAD_FINGERPRINTS = [
        # 用户截图里实锤出现过的指纹
        'each preview must', 'key requirements', "i'm looking at",
        '方案一：方案1', '方案二：方案2', '方案三：方案3', '方案四：方案4', '方案五：方案5',
        # 规则复述类
        'json格式', '输出格式', '自检', '默认数字', 'suggestions', 'preview must',
        # 中英混类（连续2个以上英语单词夹在中文里）
    ]
    def _is_garbage_preview(txt: str) -> bool:
        if not txt: return True
        low = (txt or '').lower()
        for fp in _BAD_FINGERPRINTS:
            if fp in low: return True
        # 英语单词（字母+空格+字母连续出现≥10字符且非纯URL）=垃圾
        if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', txt): return True
        # 纯占位标题+正文极短=垃圾（如标题"方案1"正文才20字）
        if len(txt.strip()) < 60: return True
        return False

    def _normalize_suggestions(raw_sugs):
        out = []
        for s in (raw_sugs or []):
            if isinstance(s, str):
                out.append({'title': '', 'preview': s.strip()})
            elif isinstance(s, dict):
                title = str(s.get('title') or s.get('name') or s.get('方案名') or '').strip()
                preview = str(s.get('preview') or s.get('content') or s.get('简介') or s.get('description') or s.get('desc') or '').strip()
                if preview:
                    out.append({'title': title, 'preview': preview})
                elif title and len(title) >= 80:
                    # 模型把内容塞进title了，迁移过来
                    out.append({'title': '', 'preview': title})
        return out

    suggestions = _normalize_suggestions(suggestions)
    suggestions = [s for s in suggestions if not _is_garbage_preview(s.get('preview', ''))]

    # 二级兜底：从整段原始文本按"方案N："分段取3-5段（中文段为主，过滤英文/规则段）
    if not suggestions:
        cn_splits = re.split(r'(?:^|\n)\s*(?:方案\s*[一二三四五六1-5][：:\s\.、])', _raw)
        cleaned = []
        for chunk in cn_splits[1:6]:
            chunk = chunk.strip()
            if not chunk: continue
            # 取chunk中第一段落（去掉```json 与 code fence）
            chunk = re.sub(r'```[\s\S]*?```', '', chunk)
            # 去明显是prompt说明/英文的句子
            lines = [l.strip() for l in chunk.split('\n') if l.strip()]
            kept_lines = []
            for l in lines:
                low = l.lower()
                if any(fp in low for fp in _BAD_FINGERPRINTS):
                    continue
                if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', l):
                    continue
                kept_lines.append(l)
            merged = ' '.join(kept_lines).strip()
            if len(merged) >= 80:
                cleaned.append({'title': f'方案{len(cleaned)+1}', 'preview': merged})
        suggestions = cleaned

    # 三级兜底：原始文本无JSON、也没"方案N："分段 → 按中文句号/问号断成连续长句，每 3-5 个长句合并为 1 条 preview
    if not suggestions:
        _clean_text = re.sub(r'```[\s\S]*?```', '', _raw)
        _clean_text = re.sub(r'<[^>]+>', '', _clean_text)
        # 去掉 prompt 类句子（带引号 Each preview / Key requirements / JSON合法 等）
        _lines = [l.strip() for l in _clean_text.split('\n') if l.strip()]
        good = []
        for l in _lines:
            low = l.lower()
            if any(fp in low for fp in _BAD_FINGERPRINTS):
                continue
            if re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', l):
                continue
            good.append(l)
        merged = ''.join(good)
        # 按中文句号/问号/叹号断句
        sents = re.split(r'(?<=[。！？!?；;])', merged)
        sents = [s.strip() for s in sents if len(s.strip()) >= 10]
        bucket = []
        cur_len = 0
        cur_parts = []
        for s in sents:
            cur_parts.append(s)
            cur_len += len(s)
            if cur_len >= 140 and len(cur_parts) >= 2:
                bucket.append(''.join(cur_parts))
                cur_len = 0
                cur_parts = []
                if len(bucket) >= 5: break
        if cur_parts and len(bucket) < 5:
            bucket.append(''.join(cur_parts))
        suggestions = [{'title': f'方案{i+1}', 'preview': p} for i, p in enumerate(bucket) if len(p) >= 80]

    # 四级兜底：按行硬截前5段非空非英文
    if not suggestions:
        lines = [l.strip() for l in (_raw or '').split('\n') if l.strip() and not l.strip().startswith('```')]
        for i, line in enumerate(lines[:5]):
            # 去掉前导序号
            clean = re.sub(r'^[\d一二三四五1-5\.、\)\s]+', '', line)
            # 英文/规则指纹直接跳过
            low = clean.lower()
            if any(fp in low for fp in _BAD_FINGERPRINTS) or re.search(r'[a-zA-Z]{3,}\s+[a-zA-Z]{3,}', clean):
                continue
            if clean and len(clean) >= 60:
                suggestions.append({'title': f'方案{i + 1}', 'preview': clean[:260]})

    if not suggestions:
        # 只有用户方案时，允许 suggestions 仅有 1 条（用户自己的），不强制要求 3-5 条
        if not user_paste:
            return jsonify({'error': 'AI 未返回有效方案，请重试或调整需求'}), 500

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
    # 【direct 模式】整体重新生成（reroll）：换展开角度，方向仍锁定上游不变
    reroll = bool(data.get('reroll'))
    # P0 榜单风向：前端先扫榜把 rank_scan 塞进来，注入 sys_prompt + 落地卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

    # reroll 允许 suggestion 为空：direct 模式的方向来自 DB 已定上游，不依赖方案卡片文本
    if not book_id or dim_key not in _DIM_KEY_TO_SPEC or (not suggestion and not reroll):
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

    # ===== 【direct 模式·上游方向锁定】执行性展开维度（设定/世界观/人物/剧情/伏笔/地图）=====
    # 上游依赖全文已由 _build_dim_context 按 DIMENSION_DEPENDENCIES 注入（见上方【已有设定参考】），
    # 这里不再重复注入全文（避免双注浪费 token），仅追加方向锁定铁律。
    # reroll=True（整体重新生成）：换一个展开角度，但方向仍锁定不变。
    direct_lock_note = ''
    if spec.get('mode') == 'direct':
        try:
            _dep_req = DIMENSION_DEPENDENCIES.get(dim_key, {}).get('required', [])
            _filled_deps = [k for k in _dep_req if _is_dim_filled(bb, k)]
            if _filled_deps:
                _dep_labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in _filled_deps if k in _DIM_KEY_TO_SPEC)
                _roll_note = ('\n【整体重新生成·reroll】上一次生成的内容作者不满意，请换一个展开角度重新组织本维度'
                              '（不同的组织结构/切入顺序/细节侧重），但方向仍以上游设定为准，禁止漂移。'
                              if reroll else '')
                direct_lock_note = ('\n【方向锁定铁律·违规=作废】本维度是执行性展开，上方【已有设定参考】中的'
                                    + (_dep_labels or '上游设定')
                                    + '就是方向源头（最高权威，必须严格遵循）：'
                                      '所有体系/人物/事件必须在其框架内展开细化，禁止另起炉灶、禁止引入与上游冲突的体系/人设/走向；'
                                      '若展开中发现上游有空缺，按上游已有逻辑自然补全，不得反向推翻上游。'
                                    + _roll_note)
        except Exception:
            direct_lock_note = ''

    # 构思阶段·专属规则：通用核心+构思格式约束+master技能包（屏蔽文风/去AI规则）
    # 大纲/剧情维度的专属附加要求在后面组装后，再二次注入 extra_master_note
    extra_master_note_parts = []
    conception_rules = build_conception_rules(skill_pack_ids, mode='agent')

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
                timeline_extra = f'\n\n【剧情维度专属要求】全书严格 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章。请基于五幕式总纲生成全部 {tv} 卷的剧情，各卷剧情连贯、卷间衔接（ending_hook与下一卷开头承接）。\n【大纲承接铁律】若已提供大纲（plot_design），各卷 main_events 的事件排序必须落在大纲该卷核心目标/主要冲突框架内，ending_hook 必须具体承接大纲该卷"卷尾高潮与悬念"（大纲定钩子方向，剧情写具体事件）；大纲未覆盖的空缺按其逻辑自然补全，禁止另起走向。'
                if existing_volumes:
                    timeline_extra += f'\n\n【已有卷剧情（须保持连贯，可在其基础上完善）】\n{existing_volumes}'
                # 核心密度约束：每卷 summary(总概要) + main_events(8-12个主要剧情事件，默认10)
                # 10个主要事件 × 平均5章 = 刚好支撑 50章 × 约2400字/章 ≈ 12万字正文
                _density_hint = ''
                if cpv and cpv > 0:
                    # 按5章/事件做倒推：期望事件数 = cpv/5
                    expected_events = max(8, min(12, int(round(cpv / 5))))
                    _density_hint = f'（按每卷约 {cpv} 章计算，本卷主要剧情事件建议正好 {expected_events} 个；每个事件平均约 {int(round(cpv / expected_events))} 章正文，1个事件可扩成5-10个节点）'
                timeline_extra += f'''

【分卷铁律·必读】**全书共 {tv} 卷，每卷约 {cpv} 章，全书约 {total_chapters} 章**。卷序号从 1 开始连续递增到 {tv}。卷名格式"第N卷 副标题"。必须覆盖全部 {tv} 卷，不得多不得少。

【卷级 6 要素铁律（每卷必须在顶层字段标注）】
  · characters：本卷核心出场人物（主角+关键配角，20-40字，如"主角A、女主角B、兄弟配角C、敌对首领D"）
  · timeline_anchor：本卷时间锚点（距开篇多久+关键日期，如"开篇第 3-8 月，仲夏至深秋"）
  · location：本卷主要地点（20-40字，按先后次序，如"出生小镇→城郊黑市→主城外围"）
  · realm_change：本卷境界变化区间（主角起→止，如"初级一段→中级七段·凝神巅峰"）
  · age_change：本卷年龄变化（主角起→止，如"16岁→16岁8个月"）

【两层结构铁律（首次生成 = 卷级6要素+总概要 + 主要剧情事件，不要写 nodes[]）】
  第一层：每卷必须有 卷级6要素 + summary + main_events[]
    · summary：本卷总体剧情概要（覆盖整卷的总故事走向，150-250字）
    · main_events：本卷 **8-12 个主要剧情事件（默认 10 个）{_density_hint}**
    · 10 个主要剧情事件 × 平均 5 章 × 2400字/章 = 本卷约 12 万字正文
    · **【本次修改·关键】main_event 不套章节（不要 chapters 字段）！** 章节分配由后续「节点设计」阶段精确到每章。
    · 每一个 main_event 必须给出"预计支撑章数"用于密度自检（不写章节区间，只写一个 estimated_chapters 数字，10 个事件的 estimated_chapters 加起来必须等于 {cpv}）。
    · 每一个 main_event 结构如下（6 要素齐全，缺一不可）：
        {{
          "index": 1,
          "title": "事件标题（动宾结构，如"城郊黑市夺令牌"）",
          "estimated_chapters": 5,                        // 本事件预计可支撑的章数，合计必须={cpv}
          "summary": "事件概要（80-160字，具体可落地写约5章内容的剧情推进：起→承→转→合→钩子）",
          "characters": "本事件核心人物（2-5人，按出场权重排序）",
          "events": "事件核心推进（20-40字：谁在什么场景做了什么，造成什么关键后果）",
          "time": "本事件时间锚（相对卷级的位置，如"卷首前10天" / "卷中·仲夏祭当天"）",
          "location": "本事件发生地点（精确到具体场景，如"出生小镇·西区工坊·旧仓库"）",
          "realm_change": "本事件结束时主角的境界变化描述（如"突破初级三段，右手法纹点亮" / "境界不动，根基扎实"）",
          "age_change": "本事件结束时主角的年龄/时程变化（如"16岁1个月零5天" / "距开篇3周"）",
          "bury": "（第X卷节点阶段再精确到章，事件层只写"本事件中段埋下：XXX；预计第Y卷回收"。没埋就空串。）",
          "payoff": "（第X卷节点阶段再精确到章，事件层只写"本事件结尾回收：前文/前卷埋下的XXX；效果：XXX"。没收就空串。）"
        }}
    · 10 个主要剧情事件的 estimated_chapters 相加必须恰好等于 {cpv}（缺/多 1 章都不行）；每个 estimated_chapters 建议 4-6，少数可以 3 或 7，但总计严格={cpv}。
  第二层：nodes[]（详细情节节点）**首次生成一律留空数组 []**，由用户在剧情维度点击每卷「节点设计」按钮后，把每个 main_event 再拆成 5-10 个节点事件生成（节点阶段再补 chapters + 精确到章的 bury/payoff）。首次生成严禁把 nodes[] 写满，严禁越俎代庖替用户做节点设计。

【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；第N卷最后一个 main_event 的结尾悬念必须能被第N+1卷第一个 main_event 承接；伏笔 payoff 能跨卷指向第N±K卷 main_event。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹在 markdown 代码块中，不要任何解释性文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "characters": "本卷核心人物（20-40字）",
    "timeline_anchor": "本卷时间锚（距开篇X月-XX月，关键节气/节日）",
    "location": "本卷主要地点路线（20-40字）",
    "realm_change": "本卷境界起→止，如"初级一段→中级七段·凝神巅峰"",
    "age_change": "本卷年龄起→止，如"16岁→16岁8个月"",
    "summary": "本卷总体剧情概要（150-250字，覆盖整卷总走向）",
    "main_plot": "本卷主线剧情（卷内主线推进路径，100-160字）",
    "core_conflict": "本卷核心冲突（对手/阵营/目标冲突）",
    "ending_hook": "本卷卷尾钩子（动态悬念/冲突/转折，承接下一卷开头）",
    "main_events": [
      {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"事件概要（可落地写约5章的具体推进）","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}},
      {{"index":2,"title":"事件2","estimated_chapters":5,"summary":"...","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}}
    ],
    "nodes": []
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。nodes 必须是空数组，首次不要写节点内容！main_events 禁止出现 chapters 字段！'''
            else:
                # 作者未指定总卷数（tv=0）：让 LLM 先给出建议 N 卷，再按 N 卷输出 JSON
                # 禁止任何默认十卷/五卷/十二卷/5-8卷的数字；JSON 结构与 tv 明确时完全一致
                timeline_extra = f'\n\n【剧情维度专属要求】作者尚未指定总卷数。请你先自行确定一个合理的分卷规模 N（N≥2，禁止擅自默认十卷/五卷/十二卷/十余卷/5-8卷等固定值），再按 N 卷生成完整剧情，每卷剧情须支撑约 {cpv} 章容量，卷间衔接（ending_hook 与下一卷开头承接）。'
                # JSON 数组格式铁律（同上，卷数改成"N卷/第N卷"占位规则）
                _density_hint = ''
                if cpv and cpv > 0:
                    expected_events = max(8, min(12, int(round(cpv / 5))))
                    _density_hint = f'（按每卷约 {cpv} 章计算，每卷主要剧情事件建议正好 {expected_events} 个；每个事件平均约 {int(round(cpv / expected_events))} 章正文，1个事件可扩成5-10个节点）'
                timeline_extra += f'''

【分卷铁律·必读】方案建议 N 卷、每卷约 {cpv} 章、全书约 N×{cpv} 章（N 就是你方案里确定的卷数，禁止擅自写死 10）。卷序号从 1 开始连续递增到 N，卷名格式"第N卷 副标题"。必须覆盖全部 N 卷，不得多不得少。

【卷级 6 要素铁律（每卷必须在顶层字段标注）】
  · characters：本卷核心出场人物（主角+关键配角，20-40字）
  · timeline_anchor：本卷时间锚点（距开篇多久+关键日期/节气）
  · location：本卷主要地点路线（20-40字，按先后）
  · realm_change：本卷境界变化区间（主角起→止）
  · age_change：本卷年龄变化（主角起→止）

【两层结构铁律（首次生成 = 卷级6要素+总概要 + 主要剧情事件，不要写 nodes[]）】
  第一层：每卷必须有 卷级6要素 + summary + main_events[]
    · summary：本卷总体剧情概要（覆盖整卷的总故事走向，150-250字）
    · main_events：本卷 **8-12 个主要剧情事件（默认 10 个）{_density_hint}**
    · 10 个主要剧情事件 × 平均 5 章 × 2400字/章 = 本卷约 12 万字正文
    · **【本次修改·关键】main_event 不套章节（不要 chapters 字段）！** 章节分配由「节点设计」阶段再精确到每章。
    · 每个 main_event 必须给出 estimated_chapters（预计支撑章数），N 卷下每卷 main_events[*].estimated_chapters 加起来必须恰好等于 {cpv}。
    · 每个 main_event 结构如下（6 要素齐全，缺一不可）：
        {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"事件概要（80-160字，可落地写约5章推进）","characters":"核心人物2-5人","events":"谁在何场景做了什么→关键后果（20-40字）","time":"相对卷级时间锚（卷首X天/卷中·祭典当天）","location":"精确场景地点","realm_change":"结束时境界变化描述","age_change":"结束时年龄/时程","bury":"本事件X段埋下：XXX；预计第Y卷回收","payoff":"本事件X段回收：前文/前卷埋下的XXX；效果…"}}
    · 合计 estimated_chapters 刚好 {cpv} 章，密度自检不要错。
  第二层：nodes[]（详细情节节点）**首次生成一律留空数组 []**，由用户在剧情维度点击每卷「节点设计」按钮后，把每个 main_event 再拆成 5-10 个节点事件生成（节点阶段补 chapters + 精确到章的 bury/payoff）。首次严禁写 nodes 内容。

【卷间衔接铁律】第K卷 ending_hook 必须与第K+1卷开头严格衔接；第K卷最后一个 main_event 的结尾悬念必须能被第K+1卷第一个 main_event 承接；伏笔 payoff 能跨卷指向第K±M卷 main_event。

【输出格式铁律·绝对】严格输出 JSON 数组（不要包裹代码块，不要任何解释文字），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "characters": "本卷核心人物（20-40字）",
    "timeline_anchor": "本卷时间锚",
    "location": "本卷主要地点路线",
    "realm_change": "主角境界 起→止",
    "age_change": "主角年龄 起→止",
    "summary": "本卷总体剧情概要（150-250字）",
    "main_plot": "本卷主线推进路径（100-160字）",
    "core_conflict": "本卷核心冲突",
    "ending_hook": "卷尾钩子承接下卷",
    "main_events": [
      {{"index":1,"title":"事件1","estimated_chapters":5,"summary":"...","characters":"","events":"","time":"","location":"","realm_change":"","age_change":"","bury":"","payoff":""}}
    ],
    "nodes": []
  }}
]
直接输出 JSON 数组，不要寒暄，不要解释，不要加任何 Markdown 标题或文字。nodes 必须是空数组，首次不要写节点内容！main_events 禁止出现 chapters 字段！'''
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
   [{"name":"主角名","identity":"...","personality":"..."}]
4. 正确格式（必须这样输出，纯中文，每字段一行）：
   姓名：主角名
   身份：边军遗孤，大宗弃徒
   性格：外表沉静寡言，行事果断
   动机：查清蒙冤真相，建立立足之地
   背景：底层劳工出身
   关系：与同营弟兄为生死之交
   能力：古法修行，近身搏杀
   物品：贴身佩刀
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

    # 构思维度专属：从"一句话故事核"扩充为可支撑百万字的完整故事蓝图（8-10个一级分节）
    concept_extra = ''
    if dim_key == 'concept':
        concept_extra = """
【构思维度·完整蓝图铁律·必须写满10分节·最少1200字·禁止两句话应付】
本书的"构思"不是一句话梗概，而是整本书全部后续维度（世界观、规则、人物、大纲、剧情、伏笔）的源头蓝图；如果这里写得空、两句话结束，下游所有维度的内容都会跟着空洞、敷衍、撑不起来（12万字×N卷的体量需要源头蓝图就有足够密度）。

输出必须按以下 10 个分节依次输出，分节标题要完整保留、不得跳过任何一节。一节最少 80-150 字，10 节合计不少于 1200 字。每一节都要写"具体到能拍板"的内容，不能出现"待设定""后续再定""根据需要补充"这类空话。

一、【一句话故事核（Logline）】
写法：主角(身份+核心处境) + 要什么(显性目标) / 怕什么(隐性恐惧) + 最大阻碍(对手/规则/自己) + 赌上什么(代价与风险)。
错误示范："废柴少年一路逆袭。"（太空）
正确示范："被夺走核心天赋的大宗嫡子沦为底层劳力，为了复仇、为了证明一条不必靠天赋的正道，他必须在五年内以身为炉重启成长之路，赌上全家性命与自己是否还会再次被背叛，最终把夺走自己一切的人从高台上拽下来。"

二、【三幕/五幕主题曲线（贯穿全书的价值主张）】
本书的核心主题是什么？主角的"起点信念"是什么？中期会因为什么事件动摇、对主题产生"反主题"的怀疑？高潮处他如何用最终选择把主题钉死？请给出 起点 → 反诘 → 抉择 → 终局 的4段主题变化，每段 60-120 字。

三、【核心冲突三角（主角 vs 对手 vs 世界规则）】
分别写清楚：
1) 主角（Protagonist）的"外部欲望/内部缺失/执念来源"；
2) 对手（Antagonist / 反派阵营 / Boss 体系）：对手的合理动机（不只是坏，要"他为何这么做、他的世界逻辑是什么、他的创伤/恐惧/执念"）、与主角的镜像关系、两人之间的零和博弈点；
3) 世界规则（System / 铁律 / 天道 / 律法 / 阶层）：世界的规则如何系统性地压迫主角？打破这个规则的代价是什么？——三个维度必须各自独立且彼此咬合。

四、【目标分层（短期/中期/长期/终极）】
给主角设计 4 层目标，每层一个具体可操作的阶段性终点（不是空洞的变强）：
- 短期（第1卷内）：具体可落地的胜负条件（如：逃出某困境、通过某考核、拿到某通行许可、赚到启动资金）；
- 中期（前30%，立足阶段）：建立一个据点/团队/身份/势力雏形（如：跻身前列、加入精英队伍、拥有自家生意）；
- 长期（中后段，立势-立威阶段）：从个人强 → 有自己的班底/地盘/话语权/制度权；
- 终极：与核心冲突三角对决的终局条件（推翻XX、改写XX规则、把XX从高台上拽下、守护住X）；
每一层目标都要写"如果失败了，主角/重要之人会失去什么具体东西"，避免空洞。

五、【核心爽感机制（爽点谱系 + 读者期待曲线）】
请按题材从以下爽感类型中选 3-5 种作为主爽点，并写出每种爽点：首次出现的卷/时机、升级迭代的节奏、爽点公式（谁在谁面前做了什么→观众/世界的即时反应→主角/对手的后续后果），不能全写"装逼打脸"：
  · 废柴逆袭 / 扮猪吃虎 / 打脸逆袭 / 升级跃迁 / 大境界突破
  · 智斗反杀 / 借刀杀人 / 权谋布局 / 以弱胜强
  · 资源暴富 / 赚差价 / 经济霸权 / 产业链垄断（商战/种田/经营资源文）
  · 团队建立 / 兄弟情义 / 红颜情愫 / 师徒传承
  · 势力崛起 / 建国立朝 / 势力扩张 / 战争碾压
  · 血脉觉醒 / 金手指解锁 / 神秘传承揭秘
  · 世界观解谜 / 真相揭秘 / 前文伏笔集中回收
  · 虐渣复仇 / 以牙还牙 / 恶有恶报 / 因果闭环
要写出每一类爽点的"触发→爆发→余波"三拍结构，以及第1卷、第3卷、第5卷、卷末各至少安排哪一次代表性爽点，便于下游剧情维度照此铺排。

六、【金手指/外挂设计（能力 + 约束 + 代价）】
金手指不是越强越好，而是"越贴合主角身份+约束越清晰+代价越具体"越好。请写出：
1) 类型：系统/灵宠/灵魂寄宿/传承/重生经验/血脉/天赋异禀/祖传异宝/空间/契约/职业技能；
2) 核心能力清单：至少 3 条不同方向的能力（主战力 + 辅助 + 资源/情报/成长）；
3) 能力分级：初期解锁什么、中期升级什么、后期才能开什么、满级形态是什么；
4) 硬约束/冷却/资源消耗：每次使用需要什么条件？哪些情况下直接失效？有没有冷却/代价/反噬？（金手指不是想怎么开就怎么开，越有边界，冲突越好看）；
5) 与主角执念的贴合度：金手指如何刚好服务于主角的短期/中期/长期目标？为什么偏偏是他得到了这个金手指，而不是路人？
6) 终极风险：金手指本身会不会变成最终 Boss？会不会有更高级的使用者盯上它？它的来历之谜是否可以作为中后期主线谜团？

七、【主角魅力公式：身份 × 反差 × 创伤执念】
为本书主角写"专属记忆符号"三选一或三选二：
- 记忆符号（外貌/口头禅/小动作）：常年佩戴的旧物、眉眼间的疤痕、习惯性摩挲的随身物件、紧张时的小动作、胜负前的标志性语言；
- 三重反差（至少一层落地成明确设定）：
  · 外在身份 vs 内里实力/权谋（扮猪吃虎）
  · 初始处境 vs 后期状态（起点弱→终局强）
  · 对外态度 vs 对内底线（嘴硬护短、佛系底线、外冷内柔）
- 核心创伤（具体、戳心、贯穿全书）：必须写"哪一年/哪一天/什么具体事件、谁做了什么、主角因此失去了什么、他身体里留下什么具体的印记（伤疤/烙印/残疾/噩梦/对某个词过敏）"，不要写"身世凄惨"空话；
- 核心执念：从创伤衍生出来的、贯穿全书的"我要拿回什么/我要证明什么/我要保护什么/我要赎罪什么/我绝不允许再发生什么"；必须强烈、可行动、可被对手精准利用制造冲突。

八、【对手/反派魅力（反派合理不是纯坏）】
写出至少 2 个层级的对手：
1) 前期对手（第1-2卷）：与主角处于同一阶层/同一地域，理由合理（资源、面子、立场、家族复仇、夺舍、背叛、信仰冲突、上位者命令），写清楚他的"赢面"在哪，不能是纯粹送经验的白痴；
2) 中期对手（第3-5卷）：格局拉高，从个人冲突 → 势力/派系/组织/国家/种族/阶级层面的系统性对手，动机要与他的身份/家族/信仰/创伤绑定；
3) 终局Boss（全书最后）：他所坚持的"世界真理/秩序"是什么？他为什么认为自己做的是对的？他与主角在主题上的根本分歧是什么？最终两人决战的"理念决战层面的胜负"比"战力胜负"更重要。
每个对手都要写：他的"合理诉求 / 他的创伤 / 他的赢面 / 他的软肋 / 他与主角的镜像点"。

九、【世界观卖点钩子（世界的"独特之处"）】
本书的世界与同题材其他书比，最独特的 3-5 个设定卖点是什么？（例如：凡人修仙传的"灵根决定一切但资源寿元逼死人"、诡秘之主的"序列途径+扮演法+特性守恒"、全球高武的"地窟入侵+财富换气血"、道诡异仙的"病与道互相映射"）。每个卖点写清楚：独特之处是什么 + 它会催生出什么独有剧情/冲突/爽点 + 第1卷首次在哪种情境下亮相。

十、【全书情感底色 + 读者定位 + 文风力向】
- 情感底色：是热血少年向/稳扎稳打凡人流/黑暗复仇流/轻松欢乐流/治愈治愈向/权谋智斗流/群像史诗流？
- 读者画像：主要读者在什么情绪下最爽？（下班解压、通勤放松、代入逆袭、解谜、看战争碾压、看情感、看种田经营）
- 文风力向建议：语言调性（文言感/口语化/冷硬/幽默/细腻/史诗）、节奏密度（每N章一个小高潮、每卷1-2个大高潮）、战斗/日常/成长/情感/经营/战争的大致比例。
- 最后写一句"如果下游维度的设定内容有冲突，这一节所定的情感底色优先。"
"""

    # 核心规则维度专属：从"一句话等级"扩充成体系化能力/修炼/科技/经济规则库
    key_rules_extra = ''
    if dim_key == 'key_rules':
        key_rules_extra = """
【设定（核心规则）维度·完整体系铁律·最少1500字·分11节输出·禁止几句话就完】
本维度是"世界的物理/社会法则"，下游所有剧情、战力、人物、势力、经济都要在这个规则体系内自洽。禁止只写"境界一到九段"就结束。

【开篇必出·书名与简介先行】输出内容的最前面必须先给两块：
①【备选书名】给出 3 个备选书名（风格差异化，各自点出核心卖点，均须贴合本书题材与核心创意）；
②【小说简介】紧跟其后，150 字以内，讲清主角处境、核心冲突与最大爽点，可直接用作书籍详情页简介。
之后再按以下 11 节依次输出，一节不跳过，合计不少于 1500 字，每一节都要有具体数字、例子。

一、【力量总体系分类（总览表）】
先给一张分类清单：本书的力量/能力属于以下哪一大类（允许混合，最多选 2 主类 + 1 辅类）：
  · 修炼体系（东方玄幻/仙侠/武侠/武道）
  · 魔法/巫术体系（西方奇幻/骑士/魔兽）
  · 序列/职业/扮演体系（如诡秘）
  · 科技树/基因/机甲/星舰体系（科幻/机甲/末世/星际）
  · 异能/规则系（都市异能/规则怪谈/超能力）
  · 国术/军武/谍报体系（近现代军事/谍战）
  · 经营/种田/职业经营（经商、基建、种田、网游、经营流）
  · 文运/国运/言灵（文道、以诗杀敌、言出法随）
写出各分类在本书中"谁掌握"（种族/国家/势力/门派/职业/阶层）、它们之间的克制关系（如：科技vs修炼谁强谁弱、什么条件下能跨类对抗）。

二、【等级阶梯表（境界/阶位/军衔/职称）】
写出完整的等级阶梯，按"凡人→登堂→入室→大成→登峰→破界→成神/至尊/不朽..."的逻辑分层，每一大层都要写：
1) 等级命名（如：淬体→通脉→凝气→化液→固晶→金身→神念→法相→洞天...，或：列兵→下士→上士→少尉→少校→少将→上将→元帅）；
2) 每级之间的战力差距（量化：如：一级可打10个普通人、二级可打100个；或：练气和筑基战力差 10 倍，筑基可跨一级但不能跨两级）；
3) 每级之间的"突破门槛"：资源、条件、感悟、试炼、风险、失败后果；
4) 每级对应的社会地位：初阶/中阶/高阶/大师/宗师/圣/神 → 对应在组织/国家/种族里的职称与权力；
5) 寿元上限：每阶寿元多少？衰老速率？意外死亡的常见方式？
6) 战力天花板：本卷末主角到哪？全书完结主角到哪？最强者到哪？——明确写清楚，避免后期战力崩。

三、【核心修炼/提升路径（两种以上路线的差异）】
不要所有人走同一条路，至少给 2 种主要提升路径 + 1 种偏门路线：
- 主路线A（大众路线）：门派/正统/学院/国家体系 → 优点/缺点/社会认可度；
- 主路线B（民间路线）：家族传承/野路子/黑市传承/异族功法 → 优点/缺点/风险；
- 偏门路线C：禁术/魔修/外道/夺舍/机械飞升/信仰成神 → 代价/反噬/被追杀的理由；
每条路径要写"它的天花板在哪、适合什么人、与其他路线的克制关系"。

四、【功法/技能树（分类分级 + 稀有度 + 配搭建议）】
把功法/技能/魔法/科技模块按"战斗系/辅助系/经营生产系/生活系/秘术禁术"分类，再按等级分级（凡/灵/宝/法/道/圣/神，或 E/D/C/B/A/S/SS）：
- 每大类至少举 3-5 个代表性技能，说明其效果、学习门槛、资源消耗、实战中最强的点和最弱的点；
- 常见的"技能搭配组合"（如：近战法修 + 瞬发低阶盾 + 身法），禁止万能主角什么都会；
- 稀有技能的获取方式（秘境/传承/血脉/功勋兑换/黑市/奇遇/金手指），并写明获取风险。

五、【资源与货币体系（完整经济系统，这是支撑种田/经营/修炼类文密度的关键）】
必须写完整，别让主角钱和资源像天上掉下来的：
1) 通用货币：名称、单位、进制（如：灵石，1上=100中=10000下；或：帝国信用币/联邦积分）；
2) 辅助货币/等价物：丹药、矿石、符箓、法器、兵器、布匹、粮食、贡献点、功勋点、军功、门派令牌；
3) 常见物品的价格参考表（至少 10 项）：如炼气期丹药 1000 下灵以下、筑基期 10 万下灵以下、金丹 1000 万下灵以下；一台机甲/一阶妖兽核/一块玄铁/一把制式武器值多少？
4) 资源产地：哪些地区产哪些核心资源？这些地方被谁掌控？资源争夺是不是主线冲突之一？
5) 贫富差距与修炼成本：突破一阶平均要花多少资源？底层/中产/顶层各自能承担到哪一阶？为什么大多数人卡在某一阶？

六、【装备/法宝/载具系统】
装备/机甲/飞船/法器/灵器/法宝/道装/护甲/兵器：
1) 分级与命名：凡器/灵器/宝器/法器/道器/仙器/神器（或 E→SSS），每阶对应战力加成；
2) 获取方式：自制/铁匠/锻造师/炼金术士 → 材料清单 + 打造门槛；
3) 绑定/认主方式：滴血/神识绑定/契约/基因锁/权限码；
4) 损耗与修复：会不会爆？会不会碎？如何修复？修复代价；
5) 代表性装备：列举 3-5 件核心装备（主角的本命刀、中期的舟、后期的阵盘），分别对应什么阶段的战力升级。

七、【炼丹/炼器/阵法/符箓/御兽/驯虫/符文/编程/制造 等生产/副职业】
这些副职业是填充正文密度、支撑经营/种田/爽点的关键，不是可有可无：
1) 选择本书至少 2-3 种副职业，写清楚它们的等级阶梯；
2) 各副职业对主职战力的加成方式（如：炼丹出丹药给修炼加速 + 给队友补血；炼器给自己量身做装备；阵法可布置组织防线/战场困敌；御兽直接多一个战力）；
3) 副职业的"升级门槛"：材料/经验/传承/配方/图纸，为什么稀缺？
4) 写出 3-5 个代表性物品（如：筑基丹、九转金丹、迷踪阵、天雷符、三级机甲引擎），分别说明配方、效用、市场价格。

八、【修炼/能力的硬约束 + 反噬代价】
为了让剧情有冲突，能力必须有边界：
- 强行越级/禁术/连轴战斗的反噬是什么？（经脉受损、寿元衰减、精神错乱、走火入魔、机械臂过载、基因崩坏、被系统惩罚）
- 有没有"心魔/外魔/天魔阻道"？什么时候出现？怎么过？失败的后果？
- 能力/境界能不能"掉落"？掉落之后如何恢复？有没有"二次破境更上一层"的可能？
- 金手指/核心能力的"冷却时间/资源消耗/使用条件/触发场景"，至少写 3 条边界条件。

九、【种族/职业/阵营能力克制表】
【维度边界】本节只写"能力与克制"（战斗/对抗怎么算）；种族的文化信仰、栖息地、外貌寿命、种族关系史归"世界观"维度种族大观节，本节不重复展开。
列出本书的主要种族/职业/阵营（人族 / 妖族 / 魔族 / 灵族 / 机械族 / 异能者 / 修士 / 星舰军官 / 江湖门派 / 教会 / 联邦...）：
- 每一方的核心能力方向、战斗优势、战斗劣势（只写能力面）；
- 种族/阵营之间的能力克制关系（谁克谁、什么条件下能跨级对抗）；
- 跨种族/跨阵营的能力禁忌（夺舍？吞噬？血脉污染？借用异族之力会怎样？）。

十、【世界硬规则与禁忌（铁律·剧情冲突的发动机）】
【维度边界】本节只管"力量使用与超凡行为的禁忌"（修炼/异能/科技使用层面的铁律）；社会治理类律法（刑法/审判/政体/税收）归"世界观"维度政治与律法节，本节不重复。
列出至少 8 条"世界铁律"，人物如果违反就会被追杀/死亡/天谴/降级/反噬/被剥夺权力：
- 如：夺舍 = 魔道 / 修炼者不能对凡人大开杀戒 / 热武器在XX地失效 / 飞升或成神要献祭XX / 系统拒绝作弊 / 非凡者在公众面前暴露异能会被官方清洗 / 谁敢动XX遗迹谁就被诅咒 / 高级修士不可随意干预凡间；
每条铁律还要写：执法者是谁？执法手段？是否有灰色地带/暗规则可以钻空子？主角会不会在剧情中主动/被动违反这些铁律？铁律本身是不是最终 Boss 用来维持秩序的工具？

十一、【体系天花板总览（战力/科技上限）】
【维度边界】本节只写"力量体系的层级与天花板"（各级战力/科技上限）；文明的政体规模、社会形态、平均生活水平归"世界观"维度势力/阶级节，本节不重复。
如果是科幻/机甲/星际/末世：写清科技树（能源、武器、护盾、跃迁、AI、生物技术、外骨骼、星舰等级、反物质）、当前主角所处的科技层级与全书天花板差多少？
如果是玄幻/仙侠/奇幻：写清修炼文明的实力层级（王朝→大宗→圣地→皇朝→仙朝→神朝），每一级对应的平均实力上限、可调动战力规模；
最终给出一句"下游任何剧情/战力/装备/经营描写如果和本节铁律冲突，以本节体系为准，必须自动修正。"
"""

    # 世界观维度专属：从"简单大陆介绍"扩充到15节级完整世界百科
    worldbuilding_extra = ''
    if dim_key == 'worldbuilding':
        worldbuilding_extra = """
【世界观维度·完整世界百科铁律·最少2000字·分15节输出·禁止两句话就完】
本维度是"故事发生的世界全景"，下游剧情的地理移动、势力站队、国家战争、势力冲突、种族矛盾、资源战争、风土人情都从这里来。不能只写"主角所在的大陆有东/西/南/北四大域"就结束。按以下 15 节依次输出，一节不跳过，合计不少于 2000 字。

一、【世界总览（宇宙/大陆/位面/世界树结构）】
从最高维度开始讲：世界是几层结构？（如一界/三界/三十三天/多元宇宙/源海+源核+界域/单一大陆+N个秘境/星球数/联邦星域）。主角所在的"主舞台"叫什么？整体面积相当于几个地球？海拔？气候带？大陆上有几大板块？（如一块超级大陆 + 若干附属岛链 + 禁忌海）。世界的"边界"在哪里？越界会怎么样？——把一张世界地图的大致轮廓用文字描述出来。

二、【世界起源/创世纪元史（至少3段创世神话 + 真实历史版本）】
- 民间神话版（老百姓信什么？各个种族的创世神是谁？）；
- 学术界/学院/圣廷/图书馆"正史版"；
- 隐藏真相版（作为中后期主线谜团，可以不揭谜底，但要留下"有问题的缝隙"）；
至少包含 3 个大纪元：远古纪元（太初/开天/第一文明）→ 中古纪元（王朝/神朝/大战/大断层）→ 近古纪元（秩序重建/大航海/灵气复苏/工业革命）→ 当代纪元（主线开局前 100 年发生了什么关键事件）。每个纪元至少 1 件"改变世界格局的大事"。

三、【地理分块（主大陆/次大陆/群岛/禁地/秘境/天堑）】
把主舞台按地理拆成至少 6 个"大区"，写出：名字、气候、地形特色（山脉/平原/森林/沙漠/沼泽/火山/冰原）、资源特产、常住人口、主要势力控制者、区域内部最大的矛盾、对主线剧情的作用；
必须包含至少 4 类特殊区域：
1) 开局出生区域（主角起家/被欺负/翻身的起点）；
2) 中期主要舞台（大宗/学院/都城/大型岛屿，在此发展势力）；
3) 禁地/绝地/秘境（危险但有机缘，中后期主线突破区）；
4) 跨区域天堑（如：无法穿越的禁忌海/需要传送阵才能过的大峡谷/空间裂缝带/辐射污染区——是剧情推进时天然的关卡）。

四、【气候与天象体系】
这个世界有没有"灵气潮汐"、"季节紊乱"、"极夜极昼"、"九星连珠"、"血月"、"圣日"、"天劫"、"灵雨/红雨/陨石雨"等特殊天象？
- 它们发生的周期？对修炼/战争/经济/农作物/社会心理有什么具体影响？
- 哪些天象会被视为吉兆/凶兆？宗教仪式是如何利用它的？——这会催生出大量可落地的剧情事件（如：每十年一次的潮汐大典 = 学院大比 + 资源拍卖会 + 敌对势力偷袭的绝佳时机）。

五、【主要势力总表（至少8个）】
至少列 8 个势力，按"大宗/王朝/帝国/家族/商会/教会/地下组织/异族联盟/学院派/军方"等不同性质分类，每一方写：
- 势力名称、性质、级别（王朝/大宗三级）、领袖、核心人物2-3人；
- 地盘（哪几个大区）、核心人口/兵员/成员规模；
- 核心战力（首领/国师/老祖什么境界、王牌军团/阵法/秘宝）；
- 经济来源（收税/矿产/商铺/炼丹/走私/奴隶/功德香火/官方拨款）；
- 理念/立派宗旨/意识形态/对外关系（和谁是盟友、和谁世仇、与主角利益的交集点）；
- 剧情位置：第1卷出现哪些？第2-4卷卷入哪些？终局谁是友谁是敌？

六、【社会制度与阶级分层】
这个世界的"人分几等"？写出完整的阶级金字塔：
- 顶层：皇帝/教皇/宗主/圣地传人/神裔/贵族/世袭大家族 → 拥有哪些权力？（生杀？立法？免税？垄断资源？）
- 中层：修士/军官/官员/富商/职业者 → 上升通道是什么？（科举？组织考核？军功？捐官？商道？）
- 底层：凡人/劳工/佃农/流民/佣兵/学徒 → 被盘剥的方式？翻身的概率？
- 禁忌群体：魔修/异族混血/偷渡者/贱籍/黑户 → 他们如何生存？主角是否曾属于这一类？
写出各阶层之间的流动是开是闭？有没有严格的种姓/血脉/出身限制？阶级矛盾是不是主线冲突的发动机？

七、【政治与律法（谁制定规则、谁执法、谁钻空子）】
【维度边界】本节写社会治理面（政体/法律来源/执法审判/灰色地带）；力量使用层面的超凡禁忌（夺舍=魔道、暴露异能被清洗等）归"设定"维度世界硬规则节，本节不重复。
- 国体：君主专制/贵族共和/组织议会/联邦民主/神权/军政府？
- 法律的来源：神谕/皇帝敕令/组织戒律/商会章程/旧例判例；
- 执法者：禁军/锦衣卫/刑堂/审判庭/治安司/赏金猎人；
- 审判程序：公开审判/私刑/决斗审判/神判；
- 灰色地带：黑市/地下钱庄/灰色法条/买官卖官/贵族豁免权——主角前期怎么在灰区活命、怎么利用灰区翻身、后期要不要打破这些灰区规则？

八、【经济与贸易系统（生产/运输/商路/黑市）】
【维度边界】本节写产业/商路/贸易/黑市与货币发行权；货币的具体面值、换算进制、物品价格表归"设定"维度资源与货币体系节，本节不重复列价格。
1) 主要产业：农业/矿业/制造业/修炼业/服务业/信息业；
2) 核心商路：哪几条大路/运河/航线/传送阵是经济命脉？分别掌握在谁手里？谁能卡住谁的脖子？
3) 主要港口/坊市/交易都市：至少 3 个，写清它们的特色、税收制度、地下势力；
4) 黑市：哪里有？做什么交易？（禁药、禁器、人口、情报、违禁功法、异族货物）；执法力度如何？
5) 通货膨胀/货币发行权：谁能铸币？谁能印钞？会不会恶性通胀？主角是否会在中期通过经营/商战掌控货币权？

九、【种族大观（至少5个智慧种族）】
【维度边界】本节写种族的文化面（栖息地/信仰/习俗/种族关系/社会地位）；种族的战斗能力方向与克制关系归"设定"维度能力克制表节，本节不重复列战力数值。
列出 5 个以上智慧种族（人/妖/魔/灵/龙/矮/精灵/鲛人/石族/亡灵/机械族/异兽族…），分别写：
- 外貌、寿命、繁衍方式、栖息地、核心优势、核心短板；
- 文化信仰、禁忌、主流价值观；
- 与其他种族的关系（被谁奴役？与谁通商？与谁世仇？）；
- 混血种群（半妖/半魔/半精灵/人机混合体）的社会地位与歧视链；
至少选 1 个种族作为"主角阵营的盟友"，1 个作为"全书主对手族群"，1 个作为"灰色中间势力，时敌时友"。

十、【宗教、信仰、神话体系】
世界上的主要宗教/信仰（至少 2 个正统 + 1 个邪教/秘教）：
- 神祇、神系、教义、主神、正神、邪神；
- 教会结构：教皇/大主教/神父/修士/骑士团/裁判所；
- 信仰的力量：信徒祷告能否产生神力？神是否会显圣？神会不会死？神与修炼体系的关系？
- 宗教与世俗政权的关系（教权>皇权？还是对立？）；
- 邪教/秘密教派存在的土壤是什么？它的教义虽然扭曲但有没有合理的底层吸引力？主角会不会被误判为邪教徒？

十一、【语言、文字、度量衡】
- 通用语、各族语言、古语/神语/加密语（龙语、精灵语、神纹、古篆、机械编码）是否存在？谁掌握？懂古语是否等于掌握传承/开启秘境的钥匙？
- 文字：方块字/拼音/符文/立体文字？识字率？底层人是否普遍文盲？主角是否因为识字/会古语而产生优势？
- 度量衡：长度（丈/尺/米/里/光年）、重量（斤/两/石/吨）、时间（时辰/刻/日/月/年/纪元/星历）、面积（亩/顷/平方公里）、容量（斗/升/桶）；
- 历法：节日/节气/圣日/祭日/战争纪念日——剧情中大型事件（考核、婚礼、大典、宣战、刺杀）常常放在节日，直接可用。

十二、【风俗、礼仪、服饰、饮食、建筑】
- 出生礼、成人礼、婚礼、葬礼、祭祀大典：具体流程是怎样的？哪些环节可以被对手利用制造冲突？
- 阶层服饰规制：什么颜色/纹样/材质/佩饰是某阶级专属？僭越会怎样？
- 饮食：主食、肉食、饮品、酒文化、饮茶、宴席座次；不同地域/种族口味差异；
- 建筑风格：山门洞府/皇宫/市井民居/店铺/书院/地下世界/异族营地——描写具体场景时要能对号入座。

十三、【军事与战力体系（组织/兵种/阵法/战争规模）】
如果有战争/国战/势力战：
- 军事组织：禁军/边军/卫所/大宗护山队/骑士团/雇佣军/星舰舰队；
- 兵种：步兵/骑兵/弓箭/重甲/法师/术师/飞骑/舰炮手/机甲兵/异兵种；
- 阵法/战阵/舰队编队：列阵的增益、破阵的代价、历史名战案例；
- 战争规模：万人级/十万人级/百万人级/星域级？一场大战消耗多少资源（粮草/弹药/灵石/丹药/人命）？战后如何恢复？——支撑中后期卷的战争线密度。

十四、【交通与通讯（跑图/传信的速度与成本）】
这会影响剧情节奏：
- 步行/骑马/飞骑/马车/宝船/飞舟/传送阵/虫洞/跃迁引擎：各自的速度、成本、门槛、谁能使用？
- 传信：飞鸽/信鸟/传音玉简/传讯符/灵网/无线电/量子通讯/星链：速度、距离、保密等级、是否可以截获/伪造？
- 写出"从A地到B地"的典型行程时间与代价：主角能不能追得上一场大战？紧急情报什么时候到？——剧情节奏的硬约束。

十五、【世界的未解之谜/禁忌之地/上古遗留】
这些是中后期的剧情燃料、世界观解谜、伏笔谜底、终极BOSS来源。至少列出 5 个：
- 每个谜团写：已知传说、主流猜测、真实真相留空（但要暗示一个方向）、与主线的关联（主角会在第几卷因为什么事件接触到它）、揭开它会带来什么改变（颠覆秩序？获得传承？引入更大的外患？）；
最后写一句："下游剧情、伏笔、人物背景中如果涉及谜团，均以本节为总源头，不得互相矛盾。"
"""

    # 地图维度专属：结构化地图条目（按地点清单式输出，每个地点至少120字）
    locations_extra = ''
    if dim_key == 'locations':
        locations_extra = """
【地图维度·结构化地点铁律·最少1200字·每个地点一条·禁止只写地点名】
输出按"地点清单"结构，编号从 1 开始连续递增，至少列出 12 个关键地点，每条地点至少 120 字，合计不少于 1200 字。每个地点必须包含以下 8 项（缺少任何一项视为敷衍）：
1) 名称：正式名 + 俗称/古名 + 所属行政区/大区；
2) 地理：经纬度大貌（东/西/南/北/中）、地形、气候、物产、天险；
3) 归属势力：宗主国/统治组织/占领军/割据家族/地下势力；
4) 核心建筑/地标：至少 3 处具体地标（城门/广场/大殿/学院/工坊区/港口/传送阵/禁地）；
5) 人口与阶层：人口规模、核心阶层、歧视链情况；
6) 经济命脉：主要产业、商会、税收、黑市；
7) 剧情作用：哪几卷作为主舞台？第一次出场时发生什么核心事件？
8) 隐藏信息/伏笔：此地埋着什么未解之谜/旧战场/上古遗址/禁忌？会不会在中后段再作为主战场回归？
至少包含以下类别各 1-2 处：
  · 主角出生/幼年居住地
  · 第1卷开局受苦/翻身地（苦役营/小镇/贫民窟/边城）
  · 中期大宗/学院/都城/主基地
  · 大型商业城市/坊市/港口
  · 禁地/秘境/古战场/遗迹
  · 边关/战区/要塞
  · 敌国首都/敌对阵营核心地盘
  · 中立区/三不管地带/灰色地带
"""

    # 伏笔维度专属：结构化伏笔（埋设章/回收章/权重/依赖/埋收方式）
    foreshadowing_extra = ''
    if dim_key == 'foreshadowing':
        foreshadowing_extra = """
【伏笔维度·结构化长短线铁律·最少1000字·每条伏笔1张卡片·禁止只有几句话】
输出必须是按"长线伏笔 + 中线伏笔 + 短线伏笔"三层结构，总伏笔数不少于 15 条，合计不少于 1000 字。每条伏笔必须按以下 10 项写满（少一项视为敷衍）：
1) 编号：F-N（长线F1-F5 / 中线M1-M7 / 短线S1-S5...）
2) 类别：身世型 / 宝物型 / 能力型 / 势力型 / 人物型 / 世界真凶型 / 规则型 / 情感型 / 因果复仇型；
3) 伏笔内容（一句话讲清埋什么）；
4) 埋设方式：对话口误/旧物细节/背景消息/路人闲聊/异象/梦境/半截信/残缺功法/某NPC反常行为；
5) 埋设位置：建议卷+建议章号（或"第1卷.E2"即第1卷第2个主要剧情事件）；
6) 回收位置：建议卷+建议章号（长线必须跨2卷以上，中线跨1卷内若干事件，短线30章内收）；
7) 权重 1-10：权重10=终局核心、1=气氛点缀；
8) 依赖项：必须先回收哪几条伏笔才能回收这一条？（例如：F5 依赖 M2、S3 先收）；
9) 收伏笔时的"爆点"：回收时要给读者什么强反馈？打脸/真相大白/战力暴涨/势力翻盘/情感大反转/世界秩序颠覆？；
10) 防遗忘备注：当正文写到埋设章附近时，要自然地"提一嘴"，避免读者忘记（AI写作时会自动检测，不准漏埋）。
必须保证：长线≥4，中线≥6，短线≥5；并至少有 2 条跨卷大伏笔（贯穿全书 F 级）终局才回收。
"""

    # 大纲维度专属：原 outline_extra 基础上再加"五幕·每幕指标清单"防一句化
    if dim_key == 'plot_design':
        # 这里不再覆盖 outline_extra，而是往它后面追加分节清单
        outline_extra += """
【大纲分节清单·每卷必须写满 6 项 + 跨卷承接 + 爽点排布·最少1500字】
除了五幕对应和核心目标外，**每一卷**你必须再额外把以下 6 项写出来，缺一项视为敷衍：
1) 本卷爽点排布（至少4个小爽点+1个大高潮爽点），分别对应剧情哪个阶段；
2) 本卷人物方向（新登场的重要人物类型与出场目的，1句话方向即可，如"引入中期对手××及其势力"——具体人物名单与塑造归"人物"维度和"剧情"维度 characters，大纲不展开）；
3) 本卷地点动线方向（起点→主要舞台→卷尾所在，1句话方向即可——具体地点清单与地理细节归"剧情"维度 location 和"地图"维度，大纲不展开）；
4) 本卷主角的"修炼/事业/财富/关系/势力"五项进展指标，每项分别从X到Y；
5) 本卷伏笔主题方向（本卷侧重埋什么类型的伏笔，如"主角身世线+上古遗迹线"，1-2句话方向即可——具体埋设条目/回收位置归"剧情"维度 bury/payoff 和"伏笔"维度，大纲不展开）；
6) 本卷结尾处主角"得到了什么 / 失去了什么 / 主动承担的新任务"是什么，确保能把下一卷拉起来；
【维度边界·防与剧情维度重复】大纲是目标层：写"每卷要完成什么、爽点怎么配、钩子往哪指"；人物名单/地点清单/伏笔条目/事件排序是执行层，归"剧情"维度（其卷级 characters/location 与事件 bury/payoff 会按卷展开），大纲写到方向为止，禁止逐条展开。
另外，整体大纲结尾要补一张【跨卷连贯性总览】表格化文字（文本即可）：
第1卷尾钩子 ←接→ 第2卷开头契机
第2卷尾钩子 ←接→ 第3卷开头契机
……
保证每一卷的 ending 都不是空洞悬念，而是能被下一卷第一幕直接承接的具体事件/危机/任务。
"""

    # 文风维度专属：从"叙事风格几句话"扩充到8分节风格手册
    style_extra = ''
    if dim_key == 'style_guide':
        style_extra = """
【文风指南维度·8分节风格手册·最少1000字·禁止只写"语言流畅有张力"】
你必须按以下 8 节输出风格手册，一节不少，合计不少于 1000 字，下游正文生成时会逐条对照：
1) 总体调性（热血/稳扎稳打/冷硬/幽默/细腻/史诗/治愈/暗黑复仇/轻小说欢乐）；
2) 叙事节奏：每N章一个小高潮、每卷几个中高潮、卷尾大高潮比例，日常/打斗/修炼/经营/对话/情感/权谋/战争/解谜的篇幅占比（加起来=100%）；
3) 描写比例：外貌、服饰、场景、心理、动作、对话各自的篇幅倾向（多/中/少）；
4) 战斗描写：白描还是华丽？先出招式名还是先写后果？一招之内要写几层细节（攻击→格挡→借力→反击→余波→围观反应→双方心理→战后代价）；
5) 对话风格：书面化？口语化？短句多还是长句多？有没有专用尊称/敬语/异族语调/古白话/翻译腔？
6) 视角规则：严格第三人称有限视角？能否上帝视角？换POV时的切换规则（章节开头？空行分隔？）？能否切换到反派视角？
7) 爽点触发写法：打脸/升级/赚钱/获得宝物/团队胜利——这些爽点统一按"酝酿→压→爆→反馈→余波"五拍来写，每拍多少比例？
8) 高压线：绝对禁止出现的词、句式、桥段（如过度玛丽苏、反复"倒吸凉气"、全员降智、女主花瓶、解释性大段独白、战力乱跳、章节无钩子断章、现代脏话出现在古代背景、作者跳出来吐槽等），列出至少 10 条。
"""

    # 【P0修复】timeline 维度输出按卷 JSON 数组，末尾不附加"300-800字纯文本"规则
    # 否则 AI 会输出纯文本大纲，落地时无法按卷 upsert。
    # 但剧情维度 main_plot/ending_hook/nodes.summary 等字段是自然语言文本，
    # 仍必须遵守叙事工艺铁律，注入 JSON 兼容的专用版。
    if dim_key == 'timeline':
        _tail_rule = f'\n\n{TIMELINE_NARRATIVE_RULES}'
    else:
        _tail_rule = f'\n\n请直接输出该维度的完整设定内容（300-800字），不要寒暄，不要解释，不要加 Markdown 标题。\n\n{PLAIN_TEXT_LAYOUT_RULES}'

    # 把维度专属附加规则合并到 conception_rules 末尾，一并注入
    if outline_extra:
        conception_rules += '\n\n' + outline_extra.strip()
    if timeline_extra:
        conception_rules += '\n\n' + timeline_extra.strip()
    if concept_extra:
        conception_rules += '\n\n' + concept_extra.strip()
    if key_rules_extra:
        conception_rules += '\n\n' + key_rules_extra.strip()
    if worldbuilding_extra:
        conception_rules += '\n\n' + worldbuilding_extra.strip()
    if character_extra:
        conception_rules += '\n\n' + character_extra.strip()
    if locations_extra:
        conception_rules += '\n\n' + locations_extra.strip()
    if foreshadowing_extra:
        conception_rules += '\n\n' + foreshadowing_extra.strip()
    if style_extra:
        conception_rules += '\n\n' + style_extra.strip()
    if af_alerts_gen:
        conception_rules += '\n\n' + af_alerts_gen.strip()
    # 【direct 模式】上游方向锁定块置于规则块最前（权威最高，先于分节清单读到）
    if direct_lock_note:
        conception_rules = direct_lock_note + '\n\n' + conception_rules

    # 预拼接块（Python 3.11 禁止 f-string 表达式内含反斜杠，故先算好再引用）
    _self_content_block = ("【当前维度已有内容（可在此基础上完善，不要简单重复）】\n" + self_content) if self_content else ""
    _skill_note_block = ("【构思阶段·平台内置规则 + 技能包方法论 + 本维度专属要求】\n" + conception_rules) if conception_rules else ""

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
{suggestion or "（整体重新生成：不基于旧方案，直接按上方方向锁定原文与规则重新展开）"}

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

    # P0 榜单风向：把扫榜市场情报追加到 system prompt 末尾（用户点了扫榜才生效）
    _rank_ctx = _format_rank_context(_rank_scan)
    if _rank_ctx:
        sys_prompt = sys_prompt.rstrip() + '\n\n' + _rank_ctx

    # ===== 会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）=====
    session = _get_or_create_session_for_book(session_id, book_id, scope='smart_setting',
                                              title=f'{spec["label"]}生成')
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
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
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
                    # 【纯文字铁律】用户粘贴内容是 JSON 时也统一转纯文本（智驾全维度纯文字口径）
                    clean_content = _plain_json_fallback(dim_key, clean_content)
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
                _enrich_card_rank_meta(card, _rank_scan)
                yield sse({'type': 'card', 'card': card, 'session_id': session_id})
                for ec in extra_cards:
                    ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                    if ec.get('title'):
                        ec['title'] = _clean_text_to_plain(ec['title'])
                    _enrich_card_rank_meta(ec, _rank_scan)
                    yield sse({'type': 'card', 'card': ec, 'session_id': session_id})
                history = load_session_messages(session)
                history.append({'role': 'user', 'content': f'落地用户{spec["label"]}方案'})
                history.append({'role': 'assistant', 'content': body,
                                'cards': [{**c, 'status': 'pending'} for c in [card] + extra_cards]})
                _safe_save_session_messages(session, history)
                yield sse({'type': 'done', 'session_id': session_id})
                return

            # 按维度给足 token（旧值 2000 会把设定/世界观截成半截，见 _DIM_MAX_TOKENS 注释）
            max_tok = _dim_max_tokens(dim_key)

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
            max_attempts = 4  # 首次 + 最多 3 次重试
            _EMPTY_FALLBACK_LEN = 30  # 兜底阈值（人物/文风/伏笔这类短内容维度也能触发）
            _last_stream_err = ''
            _last_fc_truncated = False  # 上次失败是否为截断类（思考耗尽/输出被切）→ 重试直接顶满 max_tokens
            _sections_retried = False   # 缺节自动补写是否已触发过（只补一次，防空转）
            for _attempt in range(max_attempts):
                yield SSE_HEARTBEAT_COMMENT  # SSE 保活：防 Render 30s idle timeout
                if _attempt >= 1:
                    # 【聊天截断修复】重试发 attempt_reset 让前端清空上一轮半截内容（旧实现两轮 delta 拼一条消息→像被截断）
                    yield sse({'type': 'meta', 'kind': 'attempt_reset',
                               'info': {'attempt': _attempt + 1, 'max_attempts': max_attempts,
                                        'reason': _last_stream_err[:160] if _last_stream_err else ''}})
                full = []
                _temp = 0.7 + min(_attempt * 0.08, 0.2)  # 初始 0.7 起步，重试微增
                if _attempt == 0:
                    _max_tok = max_tok
                elif _last_fc_truncated:
                    _max_tok = _DIM_MAX_TOKENS  # 截断类失败（思考耗尽 token）重试直接顶满，渐进 1.5x 不够思考消耗
                else:
                    _max_tok = min(int(max_tok * (1.5 if _attempt == 1 else 2)), _DIM_MAX_TOKENS)
                # 第 2 次起进"精简模式"：截断过长 system/铁律，防 prompt 溢出 → 模型拒答吐空
                _msgs_for_this_call = _downgrade_prompt_for_retry(cur_messages, keep_dim=dim_key) if _attempt >= 1 else cur_messages
                try:
                    # 【聊天终止修复】单次流失败只记录原因并降级重试，不再炸掉整条 SSE（旧实现直接
                    # 进外层 except → error 帧 → 前端 removeEmptyAi 消息戛然而止）
                    for chunk in gw_stream_with_hb(gw, _msgs_for_this_call, temperature=_temp, max_tokens=_max_tok):
                        if chunk is HEARTBEAT:
                            yield SSE_HEARTBEAT_COMMENT
                            continue
                        if _is_stream_retry(chunk):
                            yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
                            continue
                        full.append(chunk)
                        yield sse({'type': 'delta', 'content': chunk})
                except GeneratorExit:
                    # 客户端断开：同步抢救本轮已流出的部分内容再退出（禁止 yield）
                    _save_partial_on_disconnect(session, f'智驾生成·{spec["label"]}',
                                                requirement or suggestion[:60], ''.join(full))
                    raise
                except Exception as se:
                    _last_stream_err = str(se)[:300]
                    # 【智驾生成错误修复】确定性失败（key 无效/额度耗尽/用户取消）重试无意义，
                    # 直接跳出循环走下方空内容 error 帧立即报错（旧实现 401 也傻等 4 轮超时）
                    from llm_gateway import FailureClass
                    _fc = getattr(se, 'failure_class', None)
                    _last_fc_truncated = _fc == FailureClass.FORMAT_ERROR  # 思考耗尽/输出被切 → 重试顶满 max_tokens
                    if _fc in (FailureClass.AUTHENTICATION, FailureClass.QUOTA, FailureClass.CANCELLED):
                        break
                    continue
                raw_joined = ''.join(full)
                # EMPTY_OUTPUT 兜底1：先全局剥离 think 标签（R1 系列模型最多的问题）
                raw_no_think = _strip_think_tags(raw_joined)
                cleaned = raw_no_think.strip()
                if dim_key == 'timeline':
                    # timeline：仅 fence 清理 + JSON 规整
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
                # EMPTY_OUTPUT 兜底2：清理后仍空但 raw 有字数 → 用原始仅去 fence/html 的版本（宁脏勿空）
                if (not cleaned or len(cleaned.strip()) < 2) and len(raw_no_think.strip()) >= _EMPTY_FALLBACK_LEN:
                    fallback = raw_no_think.strip()
                    m = re.match(r'```(?:\w+)?\s*([\s\S]*?)\s*```\s*$', fallback)
                    if m:
                        fallback = m.group(1).strip()
                    fallback = re.sub(r'<br\s*/?>', '\n', fallback)
                    fallback = re.sub(r'</?p>', '\n', fallback)
                    if len(fallback.strip()) >= _EMPTY_FALLBACK_LEN:
                        cleaned = fallback.strip()
                # EMPTY_OUTPUT 兜底3：客套/拒答检测（"好的没问题"这种也当空）
                if cleaned and _is_refusal_or_fluff(cleaned):
                    cleaned = ''
                content = cleaned
                issues = validator.validate(dim_key, content, raw_length_hint=len((raw_no_think or '').strip()))
                _log_validation_issues(bb, dim_key, issues)
                # 【增强·缺节自动补写】warn 级缺节（分节命中 <60%，常因截断/跳节）也触发一次
                # 带缺失清单的重试（旧实现只重试 error 级 → 半成品直接交付，用户看到"生成了一部分"）
                _sec_retry = (not _sections_retried and _attempt < max_attempts - 1
                              and bool(validator.sections_missing_issues(issues)))
                if _sec_retry:
                    _sections_retried = True  # 缺节补写只触发一次，避免空转烧 token
                if (not validator.should_retry(issues) and not _sec_retry) or _attempt >= max_attempts - 1:
                    validation_meta = validator.to_meta(issues)
                    break
                # 需要重试：带错误反馈重新生成（error 级优先，其次缺节补写清单）
                retry_hint = validator.build_retry_hint(issues) or validator.build_sections_retry_hint(issues)
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
            # 【空回复兜底】多轮尝试后仍无内容：显式 error 帧（替代静默 done+空卡片）
            if not content or not content.strip():
                _err = _last_stream_err or 'LLM 多次尝试后仍返回空内容，请检查模型配置/额度后重试'
                yield sse({'type': 'error', 'error': f'生成失败：{_err}'})
                return
            extra_cards = _parse_card_markers(content)  # 世界观会额外产出地图卡片
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
            _enrich_card_rank_meta(card, _rank_scan)
            card_meta = {'validation': validation_meta} if validation_meta else None  # 自检结果随卡片下发
            yield sse({'type': 'card', 'card': card, 'session_id': session_id, 'meta': card_meta})
            for ec in extra_cards:
                ec['content'] = _clean_text_to_plain(ec.get('content', ''))
                if ec.get('title'):
                    ec['title'] = _clean_text_to_plain(ec['title'])
                _enrich_card_rank_meta(ec, _rank_scan)
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
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})
