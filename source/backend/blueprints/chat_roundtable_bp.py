"""【圆桌会议 + 联网搜索配置域】（自 chat_collab_bp.py 拆出，架构门禁 P2a）。

共享符号（CARD_REGISTRY, _DIM_KEY_CARD, _DIM_MAX_TOKENS, _RT_CREATE_ALL, _RT_CREATE_DIMS, _RT_CREATE_FIELD, _auto_rank_scan_from_nl, _build_toc_block, _clean_text_to_plain, _core_params_iron_block, _detect_dim_from_text, _dim_max_tokens, _enrich_card_rank_meta, _format_rank_context, _get_latest_chapter_info, _get_or_create_session_for_book, _is_rt_continue, _rt_create_dimension_system, _rt_load_state, _rt_load_state_by_sid_independent, _rt_parse_create_dims, _rt_persist_messages, _rt_save_state, _rt_stream_turn, build_chat_system_prompt, chat_collab_bp, parse_cards, strip_cards）由 chat_collab_bp.py 末尾的
_register_split_domains() 调用 init() 注入——本模块不反向 import
chat_collab_bp，避免循环导入；路由经 register() 挂到同一个
Blueprint，URL / endpoint / methods 与拆分前的装饰器注册完全一致。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify, request, stream_with_context

from blueprints.persona_config import _MODERATOR_ROLE, _PERSONAS, _ROUNDTABLE_ORDER
from session_persist import load_session_messages
from sse_keepalive import SSE_HEARTBEAT_COMMENT


def init(**deps):
    """chat_collab_bp 加载到文件末尾时调用：把共享符号注入本模块全局命名空间。"""
    globals().update(deps)


def register(bp):
    """把本域路由挂到 chat_collab_bp（endpoint 默认取函数名，与装饰器注册一致）。"""
    bp.add_url_rule('/api/ai/search-config', view_func=ai_search_config_get, methods=['GET'])
    bp.add_url_rule('/api/ai/search-config', view_func=ai_search_config_put, methods=['PUT'])
    bp.add_url_rule('/api/ai/chat/roundtable', view_func=chat_roundtable, methods=['POST'])


# ============================================================================
# 【通用聊天】新增路由（薄壳，核心逻辑放独立模块，防chat_collab_bp超基线）
#   POST /api/ai/chat/general                   通用聊天（任意话题）+ 命中维度提示气泡
#   GET/PUT /api/ai/search-config               联网搜索 Key 配置（存 AppPreference KV）
# ============================================================================

def _sync_search_keys_from_preference():
    """把 AppPreference.web_search_keys 里的 Tavily/Exa/Brave Key 同步进 os.environ。

    优先用外部环境变量；只有 env 未设置时才用用户保存的 Key。
    """
    import json as _json
    try:
        from app import AppPreference
    except Exception:
        return
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        if not str(raw).strip():
            return
        d = _json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(d, dict):
            return
        for env_key, pref_key in (('TAVILY_API_KEY', 'tavily'), ('EXA_API_KEY', 'exa'), ('BRAVE_API_KEY', 'brave')):
            if not os.environ.get(env_key):
                v = str(d.get(pref_key) or '').strip()
                if v:
                    os.environ[env_key] = v
    except Exception:
        return


def ai_search_config_get():
    """返回当前联网搜索 Key 配置状态（不回传明文，只有"已配置/未配置"）。"""
    from app import AppPreference
    d = {}
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        d = json.loads(raw) if str(raw).strip() else {}
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    masked = {}
    for k in ('tavily', 'exa', 'brave'):
        v = str(d.get(k) or '').strip()
        masked[k] = '已配置' if v else ''
    env = {}
    for k in ('TAVILY_API_KEY', 'EXA_API_KEY', 'BRAVE_API_KEY'):
        env[k] = bool(str(os.environ.get(k) or '').strip())
    return jsonify({'keys': masked, 'env': env})


def ai_search_config_put():
    """保存联网搜索 Key（可选，Tavily/Exa/Brave 任一即可；留空=清除该引擎）。"""
    from app import AppPreference
    body = request.json or {}
    cur = {}
    try:
        raw = AppPreference.get('web_search_keys', '') or ''
        cur = json.loads(raw) if str(raw).strip() else {}
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    changed = {}
    for k in ('tavily', 'exa', 'brave'):
        if k in body:
            v = str(body.get(k) or '').strip()
            cur[k] = v
            changed[k] = bool(v)
    AppPreference.set('web_search_keys', json.dumps(cur, ensure_ascii=False))
    # 新 Key 立刻生效（写入 os.environ 供 web_search_bridge 这里的连接读取）
    _sync_search_keys_from_preference()
    return jsonify({'ok': True, 'updated': changed})



def chat_roundtable():
    """圆桌会议（多 Agent 轮询讨论，参考 AutoGen RoundRobinGroupChat 固定顺序轮流发言）：
    - 7个内置专家按固定顺序轮流发言：【榜单分析师（首位，先扫榜定风向）】→毒舌读者→剧情架构师→世界观策划→爆款编辑→润色编辑→深度采访
    - 完整走两轮，让交锋充分深入
    - 每步实时SSE推给前端，用户可以看到整个讨论过程
    - 讨论结束主持人做总结报告，输出共识+结论+落地步骤
    - body: { book_id?, session_id?, topic }  (book_id可选，绑定作品时注入维度资料)
    """
    from app import db, AISession, Book, BookBible, AIConfig
    from llm_gateway import LLMGateway, get_llm_config

    data = request.json or {}
    book_id = data.get('book_id')
    session_id = data.get('session_id')
    topic = (data.get('topic') or '').strip()
    # P0 深度思考级别：统一用标准思考，保证讨论质量
    deep_think = 1
    # P0 榜单风向：先扫榜再开会，把市场风向注入主持人/所有专家/总结报告
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None
    # 【rt-header右上角"继续"按钮专用】：前端点继续不新增用户气泡，传的 topic 仍是原始议题（不是"继续"）
    # 所以必须靠这个独立布尔位强制命中 resuming/append_mode，避免走到"全新会议"分支重开场（榜单分析师从头来）
    resume_from_checkpoint = bool(data.get('resume_from_checkpoint'))
    # ================== 【圆桌·自动扫榜增强·安全版】==================
    # 自动扫榜 = 只在"全新会议"里触发一次（generate() 内部的全新会议分支里执行）。
    # 绝对不在续会/追加/调整/创作阶段触发，因为：
    #   1) 续会时用户的 topic 是"继续"两字，用它去扫榜 = 扫出一堆无关的TOP书 = 垃圾数据
    #   2) 扫榜要8~20s 网络/LLM 调用 → 放 chat_roundtable 外层 = 任何"继续"都先卡8~20s = 用户感知"继续功能没了/卡死/断掉"
    #   3) 调整阶段用户 topic 是"对总结的修改意见"= 也不该扫，拿上次议题扫出来的用就行
    # 所以下面两个变量只做占位，真正赋值在 generate() 内部"全新会议"分支：
    _rank_ctx_global = _format_rank_context(_rank_scan)
    _rank_analyst_report: str = ''

    if not topic:
        return jsonify({'error': '缺少讨论话题'}), 400

    # book_id 为空 = 纯自由讨论；绑定则注入作品维度资料
    scope = 'roundtable_global' if not book_id else 'roundtable_per_book'
    book_title = ''
    bb_summary = ''
    book = None
    bb = None
    base_system = ''
    if book_id:
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        book_title = book.title or ''
        bb = BookBible.query.filter_by(book_id=book_id).first()
        from app import Chapter, parse_chapter_number
        recent_chapters: list = []
        next_chapter_num: int | None = None
        toc_block = ''
        try:
            ch_info = _get_latest_chapter_info(book_id)
            next_chapter_num = ch_info['next_num']
            recent_raw = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
            def _ck(c):
                n = parse_chapter_number(c.title or '')
                return n if isinstance(n, int) and n > 0 else (99999 + int(c.order_index or 0))
            recent_sorted = sorted(recent_raw, key=_ck)
            recent_chapters = [
                {
                    'title': ch.title or f'第{ch.order_index or 0}章',
                    'word_count': getattr(ch, 'word_count', 0) or 0,
                    'order_index': int(ch.order_index or 0),
                } for ch in recent_sorted[-5:]
            ]
        except Exception:
            recent_chapters = []
            next_chapter_num = None
        try:
            toc_block = _build_toc_block(book_id)
        except Exception:
            toc_block = ''
        try:
            from prompt_context_cache import PromptContextCache
            _cache = PromptContextCache.get_instance()
            _cache_key = f'general_chat_system:{book_id}'
            def _builder():
                return build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
            base_system = _cache.get_or_build(_cache_key, _builder, ttl_sec=900)
        except Exception:
            base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
        non_empty_fields = [f for f in [
            ('concept', '核心构思'), ('worldbuilding', '世界观'), ('key_rules', '核心规则'),
            ('character_profiles', '人物'), ('plot_design', '大纲'),
            ('timeline', '剧情线'), ('foreshadowing', '伏笔'),
            ('locations', '地点'), ('style_guide', '文风'),
        ] if getattr(bb, f[0], None) and str(getattr(bb, f[0])).strip()]
        bb_summary = '、'.join(nf[1] for nf in non_empty_fields) if non_empty_fields else '暂无已填充维度'

    # 会话创建/获取
    # 【D2: resume_from_checkpoint 核心修复】
    # 之前如果是通用 Tab 触发的圆桌 (scope!=roundtable_*)，首次请求时后端强制 scope 路由并保存了 state，
    # 但前端"继续"按钮传的是 chatGeneralSessionId（上次的通用对话 session_id，scope 是 general/roundtable_per_book）。
    # 后端按"scope=roundtable_global + title=圆桌会议"去查，查到的不一定是同一条会话，或干脆捞不到 state → state=None → 新会议重开场。
    # 修复：resume_from_checkpoint=true 且前端给了 session_id 时，【优先从独立 roundtable_state 小表读】，
    #       因为 ai_sessions.meta_json 列根本不存在（前序所有写均是内存态，ORM 不映射），所以以前按 meta_json 查100%失败。
    _req_session_id = (data.get('session_id') or '').strip() or None
    _resume_state_found_in_session = False
    if resume_from_checkpoint and _req_session_id:
        try:
            # 用新表独立连接直接读真实进度 —— 100% 对齐 _rt_save_state 存的位置
            _rst = _rt_load_state_by_sid_independent(_req_session_id)
            _candidate = None
            if isinstance(_rst, dict) and ((_rst.get('done') and isinstance(_rst['done'], list) and len(_rst['done']) > 0) or _rst.get('moderator_open') or _rst.get('topic')):
                # 有真实进度：尝试把 session ORM 对象切到该 session_id（后续其他逻辑要用 session.scope/book_id）
                try:
                    _candidate = AISession.query.filter_by(id=_req_session_id).first()
                except Exception:
                    _candidate = None
            if _candidate is not None:
                session = _candidate
                session_id = session.id
                book_id = session.book_id or book_id
                scope = session.scope or scope
                _resume_state_found_in_session = True
            elif isinstance(_rst, dict) and ((_rst.get('done') and isinstance(_rst['done'], list) and len(_rst['done']) > 0) or _rst.get('moderator_open') or _rst.get('topic')):
                # ai_sessions 里没这条记录（可能 scope 错被删），但 roundtable_state 有真进度
                #   → 用兜底创建的 session，但 state 会在 generate 里 D3-0 独立读命中
                _resume_state_found_in_session = False
        except Exception:
            _resume_state_found_in_session = False

    if not _resume_state_found_in_session:
        # 回到原有逻辑（正常的首次开会 / 新建会议，或 resume_from_checkpoint 但 session_id 对应无状态）
        if not book_id:
            session = AISession.query.filter(
                AISession.scope == 'roundtable_global',
                AISession.title == '圆桌会议',
            ).order_by(AISession.updated_at.desc()).first()
            if not session:
                session = AISession(id=str(uuid.uuid4()), scope='roundtable_global',
                                    title='圆桌会议', book_id=None,
                                    messages_json='[]', created_at=datetime.now(timezone.utc),
                                    updated_at=datetime.now(timezone.utc))
                db.session.add(session); db.session.commit()
            session_id = session.id
        else:
            session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=topic[:30])
            session_id = session.id

    # P1-1 模型配置解析（与 chat_general 完全一致，复用会话级模型配置逻辑）
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    session_cfg_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            session_meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            session_cfg_id = (session_meta.get('ai_config_id') or '').strip() or None
    except Exception:
        session_cfg_id = None
    chosen_cfg_id = req_ai_config_id or session_cfg_id
    cfg = AIConfig.get_by_id(chosen_cfg_id) if chosen_cfg_id else None
    if cfg and not cfg.api_key:
        cfg = None
    if cfg is None:
        cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    if chosen_cfg_id and chosen_cfg_id == cfg.id and session:
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict): meta = {}
            if meta.get('ai_config_id') != cfg.id:
                meta['ai_config_id'] = cfg.id
                session.meta_json = json.dumps(meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass

    # 模型URL/KEY解析（与 chat_general 完全一致）
    import os as _os_g
    from llm_gateway import _normalize_llm_base_url as _nlg
    import app as _modg
    try:
        _actg = _modg.AIConfig.get_active()
        _actg_id = getattr(_actg, 'id', None) if _actg else None
    except Exception:
        _actg_id = None
    _is_act_g = (_actg_id and chosen_cfg_id and _actg_id == chosen_cfg_id) or (not chosen_cfg_id)
    if _is_act_g:
        _bg, _kg, _mg = get_llm_config(_modg)
        if cfg.model and cfg.model != _mg:
            _mg = cfg.model
    else:
        _bg = _nlg(cfg.base_url or _os_g.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1'), cfg.model)
        _kg = cfg.api_key or _os_g.environ.get('USER_LLM_API_KEY', '')
        _mg = cfg.model or _os_g.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    # 运行时变量（datetime/timezone/timedelta 走模块顶层导入；函数内局部 import 会让
    # Python 把名字判为局部变量 → 早于 import 的使用点直接 UnboundLocalError，实测踩过）
    _tz = timezone(timedelta(hours=8))
    _now = datetime.now(_tz)
    _var_ctx = {
        'date': _now.strftime('%Y-%m-%d'),
        'time': _now.strftime('%H:%M'),
        'current_book': book_title or '(未绑定作品)',
        'model_name': _mg,
    }
    def _var_replace(s: str) -> str:
        if not s: return s
        for k, v in _var_ctx.items():
            s = s.replace('{' + k + '}', str(v))
        return s

    # 加载历史（保存完整讨论过程供后续复盘）
    history = load_session_messages(session)

    def generate():
        yield ': ping-heartbeat-keepalive\n\n'
        # ⭐ nonlocal session：修复 D3 中 `session = rs` 导致 Python 把 session 当成 generate() 局部变量，
        #     结果在之前的 _rt_load_state(session) 就读到"没赋值的 local var"→ UnboundLocalError 崩溃。
        #     加上 nonlocal 后 session=rs 会写回外层 chat_roundtable 的 session，内外看到同一个指针。
        # ⭐ nonlocal session_id：同理——D3-0/D3 里 `session_id = session.id` 让 Python 把 session_id 判成
        #     generate() 局部变量；普通续会/全新会议路径不经过那两行赋值，最终 done/card 帧（943/923 行）
        #     读 session_id 时 UnboundLocalError → 整场讨论完成后崩在收尾帧，前端报"圆桌续会失败"。
        nonlocal _rank_ctx_global, _rank_analyst_report, _rank_scan, session, session_id
        all_messages = []

        def _emit(gen, speaker_id):
            # 把 _rt_stream_turn 的 tag 流翻译成 SSE 帧；正文累积进外层 full_parts
            for _tag, _pay in gen:
                if _tag == 'hb':
                    yield f'{SSE_HEARTBEAT_COMMENT}'
                elif _tag == 'reason':
                    yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _pay}, ensure_ascii=False)}\n\n'
                elif _tag == 'retry':
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": _pay}, ensure_ascii=False)}\n\n'
                elif _tag == '__done__':
                    full_parts.append(_pay)
                else:
                    full_parts.append(_pay)
                    yield f'data: {json.dumps({"type": "delta", "speaker": speaker_id, "content": _pay}, ensure_ascii=False)}\n\n'

        try:
            N = len(_ROUNDTABLE_ORDER)
            default_rounds = 2
            # 【轮数可配】解析作者本轮要求的讨论轮数（如"讨论3轮"/"开4轮"/"谈个5轮"）。
            # 未命中则按默认(2轮)；命中则「全新会议」首轮按 {轮数}×6位专家 开完。
            _round_req = None
            _rm = re.search(r'(?:讨论|开会|开|谈|聊|辩|进行)\s*([1-9]\d?)\s*(?:轮|圈|回合)', topic)
            if _rm:
                _round_req = int(_rm.group(1))
            # 首帧meta：告诉前端这是圆桌模式（rounds 反映实际轮数：默认2轮；作者指定则按指定值）
            _hint_rounds = (_round_req if _round_req else default_rounds)
            yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_start", "info": {"rounds": _hint_rounds, "speakers": N}}, ensure_ascii=False)}\n\n'
            # 【R3·终极修复session错位】把后端**真实用的 session_id** 推给前端。
            # 之前 bug：未绑书时后端强制用 roundtable_global session，save_state 存在那里；
            #        但前端"继续"按钮传的是 chatGeneralSessionId（general scope）→ 两条 session 彻底错开
            #        → state_loaded=F。 现在前端存好后端的 session_id，下次续会直接带它回来，100% 对位。
            _rt_real_sid = str(getattr(session, 'id', '') or session_id or '')
            # 注意：f-string 表达式内不能出现与外层相同的引号（Python < 3.12 语法限制，
            # CI 用 3.11）——scope 兜底值必须用 "" 而非 ''。
            yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_session", "info": {"session_id": _rt_real_sid, "scope": (scope or ""), "book_id": (str(book_id) if book_id else None)}}, ensure_ascii=False)}\n\n'

            is_continue = _is_rt_continue(topic)
            state = _rt_load_state(session)

            # 【D3-0：独立连接读 state】resume_from_checkpoint=true 时，先按_req_session_id开全新连接读DB，
            #    绕过请求db.session的回滚脏数据（刚才N2修复了写时独立连接，读这里也对齐彻底隔离）
            if resume_from_checkpoint and (not state or not isinstance(state, dict) or len(state.get('done') or []) == 0):
                try:
                    if _req_session_id:
                        _st_ind = _rt_load_state_by_sid_independent(_req_session_id)
                        if isinstance(_st_ind, dict) and (
                            len(_st_ind.get('done') or []) > 0 or _st_ind.get('moderator_open') or _st_ind.get('topic')
                        ):
                            state = _st_ind
                            # 尝试把 session 对象切到独立读命中的那条，保证后续 _rt_save_state 存到同一个地方
                            try:
                                _rs_match = AISession.query.filter_by(id=_req_session_id).first()
                                if _rs_match is not None:
                                    session = _rs_match
                                    session_id = session.id
                            except Exception:
                                pass
                except Exception:
                    pass

            # 【D3·终极兜底捞 state】：resume_from_checkpoint=true 时，
            # 如果上面仍然拿到 state=None 或空 dict（比如 session/scope 彻底错位、state 存在别的会话里），
            # 扫最近的 ai_sessions，用每条 session.id 去独立 roundtable_state 小表里找进度（因为 meta_json 列不存在）。
            if resume_from_checkpoint and (not state or not isinstance(state, dict) or len(state.get('done') or []) == 0):
                try:
                    q = AISession.query
                    if book_id:
                        q = q.filter(AISession.book_id == book_id)
                    recent_sessions = q.order_by(AISession.updated_at.desc()).limit(50).all()
                    _best_st = None
                    _best_score = -1
                    _best_session = None
                    for rs in recent_sessions:
                        try:
                            _rsid = str(getattr(rs, 'id', '') or '').strip()
                            if not _rsid: continue
                            st = _rt_load_state_by_sid_independent(_rsid)
                            if not isinstance(st, dict): continue
                            _sc = 0
                            _sc += 10 * len(st.get('done') or [])
                            if st.get('moderator_open'): _sc += 3
                            if st.get('topic'): _sc += 1
                            if book_id and str(rs.book_id or '') == str(book_id): _sc += 5
                            if _sc > _best_score:
                                _best_score = _sc
                                _best_st = dict(st)
                                _best_session = rs
                        except Exception:
                            continue
                    if _best_st is not None and _best_score > 0:
                        state = _best_st
                        if _best_session is not None:
                            session = _best_session
                            session_id = str(getattr(session, 'id', '') or session_id)
                except Exception:
                    pass  # 扫描失败 = 静默继续，交给原逻辑

            # 会议已完成 + 用户发"继续/会议继续/追加一轮" → 交给 append_mode 追加新一轮
            # 会议已完成 + 用户发新话题（非续会指令）→ 自动落入下方"新会议"，不重复上一场

            # 模式判定：
            #  append_mode —— 已整体完成，用户说"继续/会议继续/追加一轮"→ 追加新一轮
            #  adjust_mode —— 已整体完成，用户发的是对结论的"自然意见/反馈"（非"继续"关键词）→ 调整阶段
            #                 （主持确认意见→专家逐一回应意见收敛修正→出调整结论+更新可采纳卡片）
            #  resuming    —— 开会中途断连/手动停止，接着剩余回合开完既定轮数
            #  其余         —— 全新会议（两轮）
            # 【创作模式】作者要求"按讨论结果创作各维度/某维度" → 直接产出可采纳卡片
            _create_dims = _rt_parse_create_dims(topic) if (state and state.get('completed')) else None
            create_mode = bool(_create_dims is not None)
            append_mode = bool(is_continue and state and state.get('completed'))
            # 已完成 + 用户发的不是"继续"/创作指令而是自然反馈 → 进入调整阶段，复用上次议题与讨论上下文
            adjust_mode = bool(not is_continue and not create_mode and state and state.get('completed') and (topic or '').strip())
            resuming = bool(is_continue and state and state.get('active') and not state.get('completed'))

            # ⭐【rt-header右上角绿色"继续"按钮强制续会覆盖】
            # 用户点击继续按钮，前端不传"继续"topic（传原始议题），所以上面 is_continue=False，
            # 必须在 resume_from_checkpoint=true 时强制 is_continue=True 并重算 append_mode/resuming。
            if resume_from_checkpoint:
                is_continue = True
                if state and isinstance(state, dict):
                    if state.get('completed'):
                        # 已完成一轮以上 → 追加新一轮（与 append_mode 原语义一致）
                        append_mode = True
                        resuming = False
                        adjust_mode = False
                    else:
                        # 任何中途未完成状态：active=True/False/缺失 → 一律进入 resuming，绝不再走全新会议
                        # （之前 active 标志可能被异常路径漏写，这里兜底强制续会）
                        append_mode = False
                        resuming = True
                        adjust_mode = False
                        state['active'] = True
                        state['completed'] = False
                        if not state.get('phase'):
                            state['phase'] = 'resumed'
                # state is None（极少：DB 清理 / 首次开会被截断在主持人开场前）→ 不设置任何模式，
                # 走 else 全新会议兜底，避免报错误死流程

            # 【D2·静默续会】用户点继续=只想接着讨论，任何续会命中/追加/失败都不推 roundtable_status，直接进模式分支
            _diag_done_n = len(state.get('done') or []) if isinstance(state, dict) else 0

            if create_mode:
                # ========== 创作模式：按讨论共识创作维度 → 产出标准可采纳卡片 ==========
                try:
                    from app import BookBible as _ModBookBible
                    _bb = _ModBookBible.query.filter_by(book_id=book_id).first() if book_id else None
                except Exception:
                    _bb = None
                # 讨论共识源：优先当前 state 全量讨论记录 + 最近落盘总结报告
                _consensus = (state.get('discussion_history') or f'【议题】\n{topic}\n\n')
                try:
                    _hist_sum = ''
                    for _m in reversed(history or []):
                        if isinstance(_m, dict) and _m.get('role') == 'assistant' and '总结报告' in str(_m.get('content', '')):
                            _hist_sum = str(_m.get('content', ''))[:8000]
                            break
                except Exception:
                    _hist_sum = ''
                if _hist_sum:
                    _consensus = f'{_consensus}\n\n【最终总结报告】\n{_hist_sum}'
                # 没有识别出具体维度 → 默认全部
                if not _create_dims:
                    _create_dims = list(_RT_CREATE_ALL)
                _gw_c = LLMGateway(_bg, _kg, _mg)
                _bb_existing = {}
                if _bb:
                    for _fk in _RT_CREATE_FIELD:
                        try:
                            _v = getattr(_bb, _RT_CREATE_FIELD[_fk], None)
                            if _v and str(_v).strip():
                                _bb_existing[_fk] = str(_v)
                        except Exception:
                            pass
                _iron = _core_params_iron_block(_bb, book) if (book and _bb) else ''
                for _dk in _create_dims:
                    _label, _ctype = _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))
                    # 注意：f-string 表达式中不能含反斜杠（Python<3.12/PEP701 之前），故把 \n 预计算成变量
                    _create_msg = "\n\n📌 正在按讨论结果创作【" + _label + "】…\n\n"
                    yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": _create_msg}, ensure_ascii=False)}\n\n'
                    _sys = _rt_create_dimension_system(_dk, book, _iron, _consensus, _bb_existing.get(_dk, ''))
                    if _rank_ctx_global:
                        _sys = _sys.rstrip() + '\n\n' + _rank_ctx_global
                    _cre_full = []
                    for _tk2, _tp2 in _rt_stream_turn(_gw_c, [
                        {'role': 'system', 'content': _var_replace(_sys)},
                        {'role': 'user', 'content': f'请按讨论结论创作《{"book_title" if book else "本书"}》的【{_label}】维度'},
                    ], 0.7, None, attempts=2):
                        # body 为正文增量；__done__ 是全文汇总，跳过避免重复
                        if _tp2 is None or _tk2 == '__done__':
                            continue
                        if _tk2 == 'body':
                            _cre_full.append(_tp2)
                        yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": _tp2}, ensure_ascii=False)}\n\n'
                    _content = ''.join(_cre_full).strip()
                    _content = _clean_text_to_plain(_content)
                    _card = {
                        'id': str(uuid.uuid4())[:8],
                        'type': _ctype,
                        'title': f'{_label}（按圆桌讨论创作）',
                        'content': _content,
                        'target': _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))[0],
                    }
                    _enrich_card_rank_meta(_card, _rank_scan)
                    yield f'data: {json.dumps({"type": "card", "card": _card, "session_id": session_id}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator_summary"}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
                return
            elif append_mode:
                # ========== 追加一轮：沿用上次议题与全部发言，继续开新一轮 ==========
                state['completed'] = False
                state['active'] = True
                topic_final = state.get('topic') or topic
                done = state.get('done', []) or []
                discussion_history = state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n'
                if state.get('moderator_open'):
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{state['moderator_open']}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                _nxt = 1 + len(done) // N
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0], "round": _nxt}}, ensure_ascii=False)}\n\n'
                yield f'data: {json.dumps({"type": "delta", "speaker": "moderator", "content": f"（已读到之前的完整讨论，现在追加一轮：第{_nxt}轮继续深挖…）"}, ensure_ascii=False)}\n\n'
                target_total = len(done) + N
                # 立即落盘一次（刷新即可见历史），随后继续发言
                _rt_persist_messages(session, history, topic_final, state.get('moderator_open', ''), done, '')
            elif adjust_mode:
                # ========== 调整阶段：作者对总结报告给出意见 → 专家逐一回应意见并收敛修正 ==========
                state['completed'] = False
                state['active'] = True
                state['phase'] = 'adjust'
                topic_final = state.get('topic') or topic
                mod_open = state.get('moderator_open', '')
                done = state.get('done', []) or []
                feedback = (topic or '').strip()
                # 把作者意见追加进讨论记录 → 后续专家发言都能读到并回应
                discussion_history = (state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n')
                if mod_open:
                    discussion_history += f"\n【上次主持人开场】\n{mod_open}\n\n"
                discussion_history += f"\n【作者意见（调整阶段）】\n{feedback}\n\n"
                if mod_open:
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{mod_open}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                # 主持人开场：复述作者意见，说明本轮要重新审视并收敛修正，把场子交给专家
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0], "round": (1 + len(done)//N), "phase": "adjust"}}, ensure_ascii=False)}\n\n'
                adj_system = _MODERATOR_ROLE[1] + f"""

【议题】{topic_final}

【作者意见】
{feedback}

【场景】这是圆桌会议总结报告出具后，作者对新结论提出了具体意见/调整方向，进入"调整阶段"。
【任务】你现在以主持人身份开场（3-4句话）：先复述作者意见的核心点，说明本轮专家将围绕该意见重新审视并收敛修正此前结论，然后点出你最想先听哪位专家回应，把场子交给专家。不要展开论证。
"""
                if book_id and base_system:
                    adj_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。\n\n" + adj_system
                adj_system = adj_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    adj_system = adj_system.rstrip() + '\n\n' + _rank_ctx_global
                adj_msgs = [{'role': 'system', 'content': _var_replace(adj_system)},
                            {'role': 'user', 'content': f'主持人开场，议题：{topic_final}，作者意见：{feedback}'}]
                gw_mod = LLMGateway(_bg, _kg, _mg)
                full_parts = []
                for f in _emit(_rt_stream_turn(gw_mod, adj_msgs, 0.6, None), 'moderator'):
                    yield f
                adj_open = ''.join(full_parts)
                all_messages.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{adj_open}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator"}, ensure_ascii=False)}\n\n'
                state['moderator_open'] = adj_open
                state['discussion_history'] = discussion_history
                _rt_save_state(session, db, state)
                # 落盘一次（刷新可见历史 + 本轮作者意见）
                _rt_persist_messages(session, history, topic_final, adj_open, done, '')
            elif resuming:
                # ========== 续会：沿用上次的议题与进度，接着剩余回合开会 ==========
                topic_final = state.get('topic') or topic
                done = state.get('done', []) or []
                discussion_history = state.get('discussion_history') or f'【原始议题】\n{topic_final}\n\n'
                if state.get('moderator_open'):
                    all_messages.append({'role': 'assistant', 'content': f"【{_MODERATOR_ROLE[0]}】\n{state['moderator_open']}"})
                for d in done:
                    all_messages.append({'role': 'assistant', 'content': f"【{d.get('name','')}】\n{d.get('content','')}"})
                # ⚠️ 【关键修复】绝对不要再 yield roundtable_speaker(moderator)！
                #    之前的写法会触发前端切换"当前发言人=主持人"→追加一个主持人 speech 气泡
                #    → 用户观感：点继续后"又从主持人开始重新说了"。
                # 正确做法：静默给一条 roundtable_status 状态提示（不进 speech、不切发言人），
                # 然后直接 fall-through 到 while 循环，从 len(done) 对应的下一位专家继续。
                _len_done = len(done)
                if _len_done == 0 and not state.get('moderator_open'):
                    # 极端兜底：连主持人开场都没存上（例如用户在主持人 LLM 生成时就强制关流），
                    # 视为新会议重开，保证不空白卡死 / 不产出 0 条讨论记录
                    resuming = False
                    state = None
                else:
                    _next_round = 1 + _len_done // N
                    _next_idx = _len_done % len(_ROUNDTABLE_ORDER)
                    _next_id = _ROUNDTABLE_ORDER[_next_idx] if 0 <= _next_idx < len(_ROUNDTABLE_ORDER) else _ROUNDTABLE_ORDER[0]
                # 立即落盘一次（刷新即可见已完成的发言）
                _rt_persist_messages(session, history, topic_final, state.get('moderator_open', '') if isinstance(state, dict) else '', done, '')
            else:
                # ========== 全新会议：主持人开场 ==========
                # --- Step 0：【圆桌自动扫榜·只在全新会议触发】---
                # 续会/追加/调整/创作模式一律跳过，避免卡死用户"继续"按钮的响应。
                # 如果用户没传 preset rank_scan → 按"议题关键词决定平台"自动扫一次
                if not _rank_scan and topic:
                    try:
                        # 先给前端推一帧扫榜提示（用户感知到"正在抓榜"，不会以为卡死）
                        yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "🧾 榜单分析师正在扫描番茄/起点新书榜，整理市场风向…（约8-15秒）"}}, ensure_ascii=False)}\n\n'
                        _rt_bb_fc2 = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
                        _rt_fc2 = (_rt_bb_fc2.concept or _rt_bb_fc2.master_outline or '').strip() if _rt_bb_fc2 else ''
                        _auto_rs2, _auto_sse2 = _auto_rank_scan_from_nl(
                            topic, fallback_concept=_rt_fc2, book_title=book_title or '',
                            explicit_rank_scan=True
                        )
                        if _auto_rs2:
                            _rank_scan = _auto_rs2
                            # 重构两个全局供后续所有 expert/moderator/summary 使用
                            _rank_ctx_global = _format_rank_context(_rank_scan)

                            def _rt_report_md2(rp: dict) -> str:
                                lines: list[str] = []
                                lines.append(f"📈 圆桌开场前系统已自动扫榜成功｜平台：{rp.get('platform_label','番茄新书榜')}")
                                if rp.get('scan_time'):  lines.append(f"· 扫榜时间：{rp['scan_time']}")
                                if rp.get('subcategory_label'): lines.append(f"· 命中赛道：{rp['subcategory_label']}")
                                if rp.get('books') and isinstance(rp['books'], list):
                                    tops = rp['books'][:5]
                                    lines.append(f"· TOP{len(tops)} 同类题材上榜书（书名+一句话钩子+作者）：")
                                    for i, b in enumerate(tops, 1):
                                        parts = []
                                        if b.get('title'): parts.append(str(b['title']))
                                        if b.get('hook_1line'): parts.append(str(b['hook_1line']))
                                        if b.get('author'): parts.append(f"作者：{b['author']}")
                                        lines.append(f"  {i}. " + " ｜ ".join(parts) if parts else f"  {i}. {b}")
                                for key, zh in [('reader_buy_points', '读者买单要素·共性卖点'),
                                                ('reader_abandon_points', '读者弃文毒点·共性避坑'),
                                                ('title_formula_examples', '书名公式范例'),
                                                ('opening_hook_templates', '开篇钩子套路模板'),
                                                ('market_advice', '市场落地方向建议')]:
                                    v = rp.get(key)
                                    if isinstance(v, str) and v.strip():
                                        lines.append(f"\n【{zh}】\n{v.strip()}")
                                    elif isinstance(v, list) and v:
                                        lines.append(f"\n【{zh}】")
                                        for it in v:
                                            lines.append(f"- {it}")
                                return "\n".join(lines).strip()
                            _rank_analyst_report = _rt_report_md2(_rank_scan)
                            # 扫榜完成给用户一帧提示（可选）
                            # 提变量，避免嵌套 f-string + json.dumps 里 \" 导致 SyntaxError（Python 不允许 f-string {} 内有反斜杠）
                            _plat2 = _rank_scan.get('platform_label', '番茄新书榜') if isinstance(_rank_scan, dict) else '番茄新书榜'
                            _nb2 = len((_rank_scan.get('books') or []) if isinstance(_rank_scan, dict) else [])
                            _sse_meta_obj2 = {
                                'type': 'meta',
                                'kind': 'roundtable_status',
                                'info': {
                                    'text': f'✅ 扫榜完成：{_plat2}｜命中 {_nb2} 本TOP书，榜单分析师第一个发言会展示。'
                                }
                            }
                            yield f'data: {json.dumps(_sse_meta_obj2, ensure_ascii=False)}\n\n'
                    except Exception:
                        # 扫榜失败 = 当没发生，继续让主持人+后续7位专家正常讨论
                        pass
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator", "speaker_name": _MODERATOR_ROLE[0]}}, ensure_ascii=False)}\n\n'
                mod_system = _MODERATOR_ROLE[1] + f"""

【讨论议题】
{topic}

【规则】
- 你现在开场，简短说明讨论规则：7位专家分两轮依次发言，第一位「榜单分析师」会先给大家分享真实榜单风向（番茄/起点新书榜），随后每人就议题讲出自己的专业见解
- 鼓励交锋，允许不同意前面发言人的观点，必须碰撞出真实结论
- 开场只要说3-5句话点明规则和议题，不用展开，把场子交给专家就行
"""
                if book_id and base_system:
                    mod_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。讨论请以落地资料为准。\n\n" + mod_system
                mod_system = mod_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    mod_system = mod_system.rstrip() + '\n\n' + _rank_ctx_global
                mod_messages = [{'role': 'system', 'content': _var_replace(mod_system)}]
                mod_messages.append({'role': 'user', 'content': f'请开始开场，议题是：{topic}'})

                gw_mod = LLMGateway(_bg, _kg, _mg)
                full_parts = []
                for f in _emit(_rt_stream_turn(gw_mod, mod_messages, 0.6, None), 'moderator'):
                    yield f
                mod_content = ''.join(full_parts)
                all_messages.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{mod_content}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator"}, ensure_ascii=False)}\n\n'

                topic_final = topic
                done = []
                discussion_history = f'【原始议题】\n{topic_final}\n\n【主持人开场】\n{mod_content}\n\n'
                # 开场完成即落一次进度 → 之后任何一步断掉都能续会
                # total_rounds_hint = 用户明确指定的轮数（_round_req），否则按默认 2；resuming/append/adjust 都读这个字段
                state = {'active': True, 'completed': False, 'phase': 'discussion',
                         'topic': topic_final, 'moderator_open': mod_content,
                         'done': [], 'discussion_history': discussion_history,
                         'total_rounds_hint': (_round_req if _round_req else default_rounds)}
                _rt_save_state(session, db, state)
                # 开场即落盘消息 → 刷新界面能看到开场
                _rt_persist_messages(session, history, topic_final, mod_content, [], '')

            # ========== 讨论：从断点/开头继续，按 target_total 轮×N位依次发言 ==========
            # 注：本函数全部 LLM 调用 max_tokens=None（不发送该字段）——圆桌 6 专家×2 轮串行
            # 大量请求，网关把 max_tokens 当配额预留额度时发大值秒撞 TPM 限流掐流（前端表现为
            # network error）；不设按模型默认输出上限执行，全部走流式（gw_stream_with_hb）。
            done_count = len(done)
            gw_sp0 = LLMGateway(_bg, _kg, _mg)
            # 计算 target_total 的统一公式（新会议/resuming/append/adjust 都复用）：
            #   · state.total_rounds_hint = 作者指定的总轮数 或 默认2轮
            #   · 全新会议 = (_round_req or default_rounds)，并且写入 state
            #   · resuming = 读 state.total_rounds_hint，不写死 2
            #   · adjust_mode 阶段 = 开一轮（N位专家逐一回应反馈），target_total = len(done) + N
            #   · append_mode 追加一轮 = len(done) + N（保持原有逻辑）
            if adjust_mode:
                target_total = len(done) + N
            else:
                # 解析应开的总轮数（按用户指定或state中保存的）
                _rounds_hint_cur = state.get('total_rounds_hint') if isinstance(state, dict) and state else None
                if _rounds_hint_cur is None:
                    _rounds_hint_cur = (_round_req if _round_req else default_rounds)
                try:
                    _rounds_hint_cur = max(1, min(99, int(_rounds_hint_cur)))
                except Exception:
                    _rounds_hint_cur = default_rounds
                target_total = _rounds_hint_cur * N
            # 如果用户进入 append_mode 时显式说"继续"但实际还差很远就追加到满轮 → 保持 append_mode 原语义"追加一轮"：
            if append_mode:
                target_total = len(done) + N
            # 把 total_rounds_hint 写入 state（供 resuming/下一次 append 使用）
            if isinstance(state, dict) and not state.get('total_rounds_hint') and not adjust_mode and not append_mode:
                state['total_rounds_hint'] = _rounds_hint_cur
                _rt_save_state(session, db, state)
            while done_count < target_total:
                seq_abs = done_count
                round_num = 1 + seq_abs // len(_ROUNDTABLE_ORDER)
                speaker_id = _ROUNDTABLE_ORDER[seq_abs % len(_ROUNDTABLE_ORDER)]
                sp_name, sp_system_prompt = _PERSONAS[speaker_id]
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": speaker_id, "speaker_name": sp_name, "round": round_num}}, ensure_ascii=False)}\n\n'

                sp_system = sp_system_prompt + f"""

【当前议题】
{topic_final}

【规则】
现在是圆桌会议第{round_num}轮讨论，你是{sp_name}，请严格按你的专业身份发言。
- {f'前面已有多位专家的发言，你必须先回应他们的观点，可以用不同意直接碰撞' if not (round_num == 1 and speaker_id == _ROUNDTABLE_ORDER[0]) else '第一轮开场，你第一个从你的专业视角破题'}
- {f'这是第{round_num}轮，请收敛：抓住前面几轮别人没讲透的坑，或直接反驳前面错误的结论，给出你这一轮的主张' if round_num > 1 else '这是第一轮，请从你的专业视角给出清晰的第一轮见解'}
- 不总结所有人，只说你自己的专业观点
- 字数控制在300-800字，观点鲜明、可直接落地，拒绝空话套话
"""
                # ==============================================
                # 【榜单分析师·专属增强】首位专家：把扫榜完整报告塞进他的 system prompt，
                # 让他第一轮第一个发言就给全桌摊开「真实榜单风向」（TOP书/卖点/毒点/书名公式/钩子）
                # ==============================================
                if speaker_id == 'rank_analyst' and _rank_analyst_report.strip():
                    sp_system += (
                        "\n\n================================\n"
                        "【★★★ 圆桌前置·系统已自动扫榜成功（你是第一个发言者，必须先把这份情报展示给全桌）★★★】\n"
                        "下面这份报告是刚从番茄/起点新书榜**真实抓下来的 TOP 书数据 + LLM 情报聚合**：\n\n"
                        + _rank_analyst_report.strip() +
                        "\n================================\n"
                        "【你的第一轮发言要求（只在第一轮且你第一个说话时执行）】：\n"
                        "1) 开场先给一张「📈 扫榜情报摘要」：平台+赛道+扫榜时间、TOP3 一句话钩子、共性卖点、共性毒点\n"
                        "2) 然后给出「🎯 市场落地方向」：基于榜单，对本次议题具体建议怎么切赛道、怎么取名、前3章钩子怎么埋\n"
                        "3) 最后给后续专家一个「📢 给全桌的定调」：明确告诉毒舌读者/架构师/世界观策划/爆款编辑/润色编辑/采访——他们讨论时应该优先吸收风向的哪些点、避开哪些坑\n"
                        "4) 不拍脑袋，每一条建议必须标注'参考榜上书XXX的套路'/'避开榜上书XXX的毒点'\n"
                    )
                if book_id and base_system:
                    sp_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。讨论请以落地资料为准。\n\n" + sp_system
                sp_system = sp_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
                if _rank_ctx_global:
                    sp_system = sp_system.rstrip() + '\n\n' + _rank_ctx_global

                sp_messages = [{'role': 'system', 'content': _var_replace(sp_system)}]
                sp_messages.append({'role': 'user', 'content': (discussion_history + f"\n【轮次】第{round_num}轮 → 轮到【{sp_name}】发言，请开始：\n")[:12000]})

                full_parts = []
                for f in _emit(_rt_stream_turn(gw_sp0, sp_messages, 0.7, None), speaker_id):
                    yield f
                sp_content = ''.join(full_parts)

                done = done + [{'round': round_num, 'speaker': speaker_id, 'name': sp_name, 'content': sp_content}]
                discussion_history += f"\n【第{round_num}轮 · {sp_name}】\n{sp_content}\n\n"
                all_messages.append({'role': 'assistant', 'content': f'【{sp_name}】\n{sp_content}'})
                yield f'data: {json.dumps({"type": "speaker_done", "speaker": speaker_id, "round": round_num}, ensure_ascii=False)}\n\n'

                # 每完成一人落一次进度 → 断连后说"继续"即可从下一位精准接上
                _mod_open = state.get('moderator_open', '') if isinstance(state, dict) else ''
                state = {'active': True, 'completed': False, 'phase': 'discussion',
                         'topic': topic_final, 'moderator_open': _mod_open,
                         'done': done, 'discussion_history': discussion_history}
                _rt_save_state(session, db, state)
                # 同时把已讨论内容落盘到会话 → 中途断连/手动停止后刷新也能看到
                _rt_persist_messages(session, history, topic_final, _mod_open, done, '')
                done_count += 1

            # ========== 总结报告 ==========
            yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_speaker", "info": {"speaker_id": "moderator_summary", "speaker_name": "主持人·总结报告"}}, ensure_ascii=False)}\n\n'
            # 从议题关键词识别命中维度（构思/设定/世界观/大纲/人物/剧情/伏笔/文风等）
            _hit_dims = _detect_dim_from_text(topic_final)
            _hit_card_types = []
            _hit_labels = []
            for _k, _kw in _hit_dims:
                _ct = _DIM_KEY_CARD.get(_k)
                if _ct and _ct in CARD_REGISTRY:
                    _hit_card_types.append(_ct)
                    _hit_labels.append(CARD_REGISTRY[_ct]['label'])
            _sum_dim_note = ('；'.join(_hit_labels) if _hit_labels else '暂无明确命中')
            _sum_dim_types = ('/'.join(_hit_card_types) if _hit_card_types else 'SAVE_CONCEPT/SAVE_RULE/SAVE_WORLDSETTING/SAVE_OUTLINE_NODE/SAVE_PLOT/SAVE_CHARACTER/SAVE_FORESHADOW/SAVE_LOCATION/APPLY_STYLE')

            sum_system = _MODERATOR_ROLE[1] + f"""

【讨论议题】
{topic_final}

【完整讨论记录】
{discussion_history[:12000]}

【总结要求】
{f'''把讨论收束成一份清晰的总结报告（本次是【调整阶段】，请结合作者意见『{feedback[:120]}』，在上一版结论基础上说明：哪些做了修正、哪些维持、最终结论是否变化；「落地采纳建议」只针对调整后仍要采纳的维度）。结构必须是：

# 圆桌会议调整结论：{topic_final[:40]}''' if adjust_mode else f'''把讨论收束成一份清晰的总结报告，结构必须是：

# 圆桌会议总结：{topic_final[:40]}'''}

## 核心共识
列出大家都同意的结论，每条一句话

## 主要分歧
列出不同专家观点不一致的地方，点出各方理由

## 优化建议（按优先级排序）
1. 最优先改什么（必须具体可落地）
2. 次优先改什么
3. ...

## 落地采纳建议（是否采纳到各维度）
结合讨论结论，逐条给出本话题若落地应"要不要采纳"进哪些创作维度，每条格式：
- 维度：人物 / 大纲 / 世界观 / 剧情 / 伏笔 / 文风 / 设定 / 构思 / 地图……
- 结论：一两句说清该维度应做什么调整或新增
- 是否采纳：直接写"建议采纳"/"有条件采纳"/"暂不采纳"，并一句话说明理由

## 最终结论
一句话给作者拍板：这个点子能不能打，核心优势在哪，最大短板在哪

严格按这个结构输出，用markdown标题分级，结论要明确，别模棱两可。

【落地采纳建议的书写要求】
"落地采纳建议"小节除上述格式外，每条必须写"是否采纳"（建议采纳/有条件采纳/暂不采纳）；本小节用纯中文 markdown 输出，不要出现 [[CARD:...]] 这类标记，卡片另由系统整理。
本次议题已识别相关维度：{_sum_dim_note}
"""

            if book_id and base_system:
                sum_system = base_system.rstrip() + f"\n\n当前绑定作品《{book_title}》，已填充维度：{bb_summary}。总结请以落地资料为准。\n\n" + sum_system
            sum_system = sum_system.rstrip() + f"\n\n【运行时上下文变量】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
            if _rank_ctx_global:
                sum_system = sum_system.rstrip() + '\n\n' + _rank_ctx_global
            sum_messages = [{'role': 'system', 'content': _var_replace(sum_system)}]
            sum_messages.append({'role': 'user', 'content': '请输出总结报告'})

            gw_sum = LLMGateway(_bg, _kg, _mg)
            full_parts = []
            for f in _emit(_rt_stream_turn(gw_sum, sum_messages, 0.5, None), 'moderator_summary'):
                yield f
            sum_content = ''.join(full_parts)
            all_messages.append({'role': 'assistant', 'content': f'【总结报告】\n{sum_content}'})
            yield f'data: {json.dumps({"type": "speaker_done", "speaker": "moderator_summary"}, ensure_ascii=False)}\n\n'

            # ========== 单独整理"落地采纳建议卡片"（不进讨论气泡，作为可采纳的 ActionCard 下发） ==========
            sum_cards = []
            try:
                yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "正在整理可落地的采纳建议…"}}, ensure_ascii=False)}\n\n'
                _card_sys = _MODERATOR_ROLE[1] + f"""

【任务】根据下面的圆桌会议总结，把"落地采纳建议"小节里判定为「建议采纳/有条件采纳」的维度整理成可落地的 Action Card。

【输出格式】只输出卡片标记，禁止一切解释/前言/后记，一个维度一张，格式严格如下：
[[CARD:卡片类型|标题|具体内容]]

【卡片类型对照】SAVE_CONCEPT=构思, SAVE_RULE=设定, SAVE_WORLDSETTING=世界观, SAVE_OUTLINE_NODE=大纲, SAVE_PLOT=剧情, SAVE_CHARACTER=人物, SAVE_FORESHADOW=伏笔, SAVE_LOCATION=地图, APPLY_STYLE=文风
【内容要求】卡片内容必须具体、可直接写入对应维度（如"采纳到人物"就写清楚姓名/性格/动机等）；拿不准的维度宁可不产，少于一行不要产。
"""
                _card_msg = [{'role': 'system', 'content': _var_replace(_card_sys)},
                             {'role': 'user', 'content': sum_content[:8000]}]
                _card_full = []
                for _tk, _tp in _rt_stream_turn(gw_sum, _card_msg, 0.3, 2048):
                    if _tk == 'body':
                        _card_full.append(_tp)
                _card_txt = ''.join(_card_full) or ''
                sum_cards = parse_cards(_card_txt)
            except Exception:
                sum_cards = []
            # 下发卡片；顺带清理总结正文里可能残留的卡片标记
            for _card in sum_cards:
                _enrich_card_rank_meta(_card, _rank_scan)
                yield f'data: {json.dumps({"type": "card", "card": _card, "session_id": session_id}, ensure_ascii=False)}\n\n'
            sum_content = strip_cards(sum_content or '').strip()
            all_messages[-1]['content'] = f'【总结报告】\n{sum_content}'

            # 落盘 + 标记会议完成（保留全量进度，供其后再"继续/追加一轮"）
            state['completed'] = True
            state['phase'] = 'done'
            _rt_save_state(session, db, state)

            # 把整场（含追加轮）的可复盘消息落盘 → 刷新界面不丢
            _mod_open = state.get('moderator_open', '') if isinstance(state, dict) else ''
            # 落盘的卡片也同步 enrich，保证后续复盘/续会仍保留风向来源
            for _c in sum_cards:
                _enrich_card_rank_meta(_c, _rank_scan)
            _rt_persist_messages(session, history, topic_final, _mod_open, done, sum_content, summary_cards=sum_cards)

            full_discussion = [
                {'speaker': m.get('content', '').split('】')[0].split('【')[-1] if m.get('role') == 'assistant' else '', 'content': m.get('content', '')}
                for m in all_messages
            ]
            yield f'data: {json.dumps({"type": "done", "session_id": session_id, "summary": sum_content}, ensure_ascii=False)}\n\n'

        except Exception as e:
            import traceback
            traceback.print_exc()
            # ======= 圆桌会议：异常退出（断连/超时/模型错误）也要存 state，支持"继续"断点续会 =======
            # 对齐节点设计师 L9778-L9790 异常存进度逻辑：
            # 把已经完整生成完毕并 append 到 done 的发言、discussion_history 全部存进 meta_json，
            # 防止"开到一半崩溃 → 用户说继续 → state 丢了 → 当成新会议/追加一轮"
            try:
                if session:
                    _st_save = dict(state) if isinstance(state, dict) else {}
                    # 异常前做一些兜底：把 discussion_history / done 字段都写全，缺的就用局部变量
                    if 'done' not in _st_save or not isinstance(_st_save.get('done'), list):
                        try: _st_save['done'] = list(locals().get('done') or [])
                        except Exception: _st_save['done'] = []
                    if 'discussion_history' not in _st_save or not isinstance(_st_save.get('discussion_history'), str):
                        try: _st_save['discussion_history'] = str(locals().get('discussion_history') or '')
                        except Exception: _st_save['discussion_history'] = ''
                    if 'topic' not in _st_save:
                        try: _st_save['topic'] = str(locals().get('topic_final') or topic or '')
                        except Exception: _st_save['topic'] = ''
                    if 'moderator_open' not in _st_save:
                        try: _st_save['moderator_open'] = str(locals().get('state', {}).get('moderator_open', '') if isinstance(state, dict) else '')
                        except Exception: pass
                    if 'active' not in _st_save: _st_save['active'] = True
                    if 'completed' not in _st_save: _st_save['completed'] = False
                    if 'phase' not in _st_save: _st_save['phase'] = 'interrupted'
                    _st_save['updated_at'] = datetime.now(timezone.utc).isoformat() if 'timezone' in dir() else __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
                    _rt_save_state(session, db, _st_save)
                    # 同时落盘一次历史（刷新就能看到已完成的发言）
                    try:
                        _mod_for_save = _st_save.get('moderator_open', '') if isinstance(_st_save, dict) else ''
                        _topic_for_save = _st_save.get('topic', '') if isinstance(_st_save, dict) else ''
                        _done_for_save = _st_save.get('done', []) if isinstance(_st_save, dict) else []
                        _rt_persist_messages(session, history, _topic_for_save, _mod_for_save, _done_for_save, '')
                    except Exception:
                        pass
            except Exception:
                pass
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'
        finally:
            # ==============================================
            # ⭐【断点续会·终极兜底：finally 存 state】
            # ==============================================
            # 覆盖【用户点"停止"/刷新页面/关闭浏览器】场景：SSE 连接断 = Python 抛 GeneratorExit
            # GeneratorExit 是 BaseException 子类 ❗NOT Exception❗，所以上面的 except Exception 抓不到，
            # 之前就是这里漏了 → state 没存 → 下次点继续 state=None → 全新会议从主持人+榜单分析师重开场。
            try:
                _fin_has_session = (session is not None) and ('db' in dir()) and (db is not None)
            except Exception:
                _fin_has_session = False
            if _fin_has_session:
                try:
                    _fin_state = dict(state) if isinstance(state, dict) else {}
                except Exception:
                    _fin_state = {}
                # —— 1) done 数组（续会核心：已完整发言的专家列表）：直接用局部变量名 done，UnboundLocalError 兜底
                try:
                    # noinspection PyUnboundLocalVariable
                    _fin_done_local = done  # type: ignore
                    if isinstance(_fin_done_local, list) and _fin_done_local:
                        _fin_state['done'] = [dict(x) if isinstance(x, dict) else x for x in _fin_done_local]
                except (UnboundLocalError, NameError, Exception):
                    if 'done' not in _fin_state or not isinstance(_fin_state.get('done'), list):
                        _fin_state['done'] = []
                if not isinstance(_fin_state.get('done'), list):
                    _fin_state['done'] = []
                # —— 2) discussion_history：拼接上下文给续会的下一位专家读
                try:
                    # noinspection PyUnboundLocalVariable
                    _fin_hist_local = discussion_history  # type: ignore
                    if isinstance(_fin_hist_local, str) and _fin_hist_local:
                        _fin_state['discussion_history'] = _fin_hist_local
                except (UnboundLocalError, NameError, Exception):
                    if not _fin_state.get('discussion_history'):
                        try:
                            # noinspection PyUnboundLocalVariable
                            _fin_tf_local = topic_final  # type: ignore
                            if _fin_tf_local: _fin_state['discussion_history'] = f'【原始议题】\n{_fin_tf_local}\n\n'
                        except (UnboundLocalError, NameError, Exception):
                            if topic: _fin_state['discussion_history'] = f'【原始议题】\n{topic}\n\n'
                # —— 3) topic / moderator_open（前者保证续会不用传"继续"两个字）
                try:
                    # noinspection PyUnboundLocalVariable
                    _fin_tf_local2 = topic_final  # type: ignore
                    if _fin_tf_local2 and not _fin_state.get('topic'):
                        _fin_state['topic'] = str(_fin_tf_local2)
                except (UnboundLocalError, NameError, Exception):
                    if topic and not _fin_state.get('topic'):
                        _fin_state['topic'] = topic
                if not _fin_state.get('moderator_open'):
                    try:
                        # noinspection PyUnboundLocalVariable
                        _fin_mc = mod_content  # type: ignore
                        if _fin_mc: _fin_state['moderator_open'] = str(_fin_mc)
                    except (UnboundLocalError, NameError, Exception):
                        pass
                # —— 4) rounds hint
                try:
                    if not _fin_state.get('total_rounds_hint'):
                        try:
                            # noinspection PyUnboundLocalVariable
                            _fin_rh = _rounds_hint_cur  # type: ignore
                            _fin_state['total_rounds_hint'] = max(1, min(99, int(_fin_rh)))
                        except (UnboundLocalError, NameError, Exception, ValueError):
                            _fin_state['total_rounds_hint'] = 2
                except Exception:
                    _fin_state['total_rounds_hint'] = 2
                # —— 5) active / completed 标志位：除非真做完了，否则一律标可续会
                try:
                    if not _fin_state.get('completed'):
                        _fin_state['active'] = True
                        _fin_state['completed'] = False
                    if not _fin_state.get('phase'):
                        _fin_state['phase'] = 'interrupted' if not _fin_state.get('completed') else 'done'
                except Exception:
                    pass
                # —— 6) DB 持久化（核心！）
                try:
                    _rt_save_state(session, db, _fin_state)
                except Exception:
                    pass
                # —— 7) 历史消息落盘（刷新界面用户能直接看到已保存的发言段）
                try:
                    try:
                        # noinspection PyUnboundLocalVariable
                        _fin_h = history  # type: ignore
                    except (UnboundLocalError, NameError, Exception):
                        _fin_h = []
                    _fin_h = _fin_h if isinstance(_fin_h, list) else []
                    _rt_persist_messages(session, _fin_h,
                                         str(_fin_state.get('topic') or topic or ''),
                                         str(_fin_state.get('moderator_open') or ''),
                                         list(_fin_state.get('done') or []), '')
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})
