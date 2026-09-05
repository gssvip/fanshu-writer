"""通用聊天模式（CHATBOX 风格智驾对话）— 从 chat_collab_bp.py 拆出。

POST /api/ai/chat/general：
- 不强制创作上下文，可聊任何话题；命中写作关键词/句式时首帧 meta 回传命中建议
- node_designer 节点设计师分支（整卷节点生成/断点续会/全卷合并卡）也在这里
- SSE 流式（思考帧/心跳/断流抢救）
依赖：blueprints/chat_collab_bp 的 helper 单向导入（无循环）；node_designer
续会工具在 blueprints/nd_helpers.py。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request, Response, stream_with_context

from llm_gateway import _is_reasoning_frame
from sse_keepalive import gw_stream_with_hb, HEARTBEAT, SSE_HEARTBEAT_COMMENT, _is_stream_retry
from session_persist import (
    load_session_messages,
    _safe_save_session_messages,
    _save_partial_on_disconnect,
)
from blueprints.chat_collab_bp import (
    _DIM_MAX_TOKENS,
    _PERSONAS,
    _RT_CREATE_DIMS,
    _ThinkingSplitter,
    _auto_rank_scan_from_nl,
    _build_toc_block,
    _clean_text_to_plain,
    _get_latest_chapter_info,
    _get_or_create_session_for_book,
    _native_reasoning_kwargs,
    _rt_create_dimension_system,
    _rt_general_dim_request,
    _sync_search_keys_from_preference,
    build_chat_system_prompt,
    parse_cards,
    strip_cards,
)
from blueprints.nd_helpers import (
    _is_nd_continue,
    _is_nd_new_volume_request,
    _nd_build_continue_user_injection,
    _nd_build_full_volume_card,
    _nd_clear_state,
    _nd_collect_all_save_plot_volumes,
    _nd_load_state,
    _nd_save_state,
    _parse_last_chapter_from_text,
    _parse_volume_index_from_text,
)

general_chat_bp = Blueprint('general_chat', __name__)


@general_chat_bp.route('/api/ai/chat/general', methods=['POST'])
def chat_general():
    """通用聊天模式（CHATBOX风格）：
    - 不强制创作上下文，可聊任何话题；
    - 命中写作相关关键词/句式时：
        * 首帧 meta 回传命中建议（前端渲染"是否落入XX维度？"气泡）
        * system_prompt鼓励AI在讨论出明确结论时产出落地卡片
    - body: { book_id?, session_id?, message }  (book_id可选，不给就是纯闲聊不绑定作品)
    - 返回 SSE：delta / meta(hit_suggestions) / card / done / error
    """
    from app import db, AISession, Book, BookBible, AIConfig
    from llm_gateway import LLMGateway, get_llm_config
    # 命中识别 & system_prompt 构建（独立模块）
    try:
        from general_chat_hitter import (detect_dimension_hits, build_general_chat_system_prompt,
                                         wrap_message_with_context)
    except ImportError:
        detect_dimension_hits = None
        build_general_chat_system_prompt = lambda: '你是智驾创作助手，可以聊任何话题。'
        wrap_message_with_context = lambda msg, bt, bb: msg
    # P0 真联网搜索：多引擎调度桥（Tavily/Exa/Brave/DuckDuckGo兜底 + 智谱原生web_search）
    try:
        from web_search_bridge import (should_use_web_search, run_web_search,
                                       format_search_context_for_llm, get_native_websearch_params)
        _search_available = True
    except Exception:
        _search_available = False
        def should_use_web_search(*a, **k): return False
        def run_web_search(*a, **k):
            from dataclasses import dataclass
            @dataclass
            class _SR: ok=False; engine=''; hits=[]; error='import failed'; latency_ms=0
            return _SR()
        def format_search_context_for_llm(sr): return ''
        def get_native_websearch_params(*a, **k): return None

    data = request.json or {}
    book_id = data.get('book_id')
    session_id = data.get('session_id')
    message = (data.get('message') or '').strip()
    # P0-4 通用聊天工具栏（底部一排）透传项：
    #   deep_think: 深度思考程度（0=关闭 1=标准思考 2=深度思考：温度/字数/system 逐级增强）
    #   web_search_enabled: 联网搜索开关（true=强制联网搜索，绕开"创作类话题不搜"的启发式过滤）
    deep_think = min(max((int(data.get('deep_think') or 0)), 0), 2)
    web_search_enabled = bool(data.get('web_search_enabled'))
    # P1-1 会话级切模型：请求体 ai_config_id > 会话 meta_json.ai_config_id > 全局激活
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    # P1-3 内置角色 persona：default/polish/toxic_critic/architect/worldbuilder/marketeer/interviewer
    req_role_id = (data.get('role_id') or '').strip() or None
    if not message:
        return jsonify({'error': '缺少 message'}), 400
    # book_id 为空 = 纯闲聊会话（scope=general_global）
    scope = 'general_global' if not book_id else 'general_per_book'
    book_title = ''
    bb_summary = ''
    book = None
    bb = None
    base_system = ''
    # 最近章节 + 下一章号（与 chat_smart 统一口径，避免通用聊"姜离是主角吗"回答错——因为没读到人物采纳资料）
    recent_chapters: list = []
    next_chapter_num: int | None = None
    toc_block = ''
    if book_id:
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        book_title = book.title or ''
        bb = BookBible.query.filter_by(book_id=book_id).first()
        # ===== 关键修复：通用聊天读取"当前作品已采纳各维度内容"（人物/设定/世界观/大纲…）=====
        # 之前 chat_general 只用了 bb_summary = "已填充维度摘要：人物、世界观" → 纯标签，没有真正把人物内容喂给LLM
        # → 用户截图里说"姜离是主角你怎么忘了"就是因为没注入已采纳的 character_profiles/concept 等内容
        # 改法：直接复用 chat_smart 链路的 build_chat_system_prompt（完整注入9个维度字段 + TOC + 最近章节 + 章节号铁律）
        from app import Chapter, parse_chapter_number
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
        # 复用 chat_smart 的维度感知 system_prompt（完整注入构思/人物/世界观/核心规则/大纲/剧情线/伏笔/地点/文风 + 最近章节 + TOC + 章节号铁律）
        # 用 PromptContextCache 命中，减少 DB → LLM 的 token 浪费（与正文创作链路统一）
        try:
            from prompt_context_cache import PromptContextCache
            _cache = PromptContextCache.get_instance()
            _cache_key = f'general_chat_system:{book_id}'
            def _builder():
                return build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
            base_system = _cache.get_or_build(_cache_key, _builder, ttl_sec=900)
        except Exception:
            base_system = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block)
        # bb_summary 升级成非空的"维度摘要"（命中创作关键词的引用前言要用）：维度名+是否填充，不再是大白话标签
        non_empty_fields = [f for f in [
            ('concept', '核心构思'), ('worldbuilding', '世界观'), ('key_rules', '核心规则'),
            ('character_profiles', '人物'), ('plot_design', '大纲'),
            ('timeline', '剧情线'), ('foreshadowing', '伏笔'),
            ('locations', '地点'), ('style_guide', '文风'),
        ] if getattr(bb, f[0], None) and str(getattr(bb, f[0])).strip()]
        bb_summary = '、'.join(nf[1] for nf in non_empty_fields) if non_empty_fields else '暂无已填充维度'
    # 命中维度检测（零LLM快路径）
    hit_suggestions = detect_dimension_hits(message) if detect_dimension_hits else []

    # 会话（global闲聊：不绑book，通用唯一会话key）
    if not book_id:
        # 纯闲聊会话：用固定scope+uuid，book_id写入None
        session = AISession.query.filter(
            AISession.scope == 'general_global',
            AISession.title == '通用闲聊',
        ).order_by(AISession.updated_at.desc()).first()
        if not session:
            session = AISession(id=str(uuid.uuid4()), scope='general_global',
                                title='通用闲聊', book_id=None,
                                messages_json='[]', created_at=datetime.now(timezone.utc),
                                updated_at=datetime.now(timezone.utc))
            db.session.add(session); db.session.commit()
        session_id = session.id
    else:
        session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=message[:30])
        session_id = session.id

    # 构建 messages
    # P1-3 内置角色 persona 表：id -> (name, system_prompt_extra)
    #（单一定义在模块级 _PERSONAS，通用聊天与圆桌会议共用，避免人格漂移）
    _BUILTIN_ROLES = dict(_PERSONAS)  # 注意：模块级表不含 'default'，这里补上
    _BUILTIN_ROLES['default'] = ('默认助手', '')
    # 选角色：请求 > 会话meta_json.role_id > default
    _session_role_id = None
    try:
        if session and hasattr(session, 'meta_json') and session.meta_json:
            _meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads(session.meta_json or '{}')
            _session_role_id = (_meta.get('role_id') or '').strip() or None
    except Exception:
        _session_role_id = None
    chosen_role_id = req_role_id or _session_role_id or 'default'
    if chosen_role_id not in _BUILTIN_ROLES: chosen_role_id = 'default'
    # 持久化 role_id 到 session.meta_json（下次沿用）
    if req_role_id and chosen_role_id == req_role_id and session:
        try:
            _meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(_meta, dict): _meta = {}
            if _meta.get('role_id') != chosen_role_id:
                _meta['role_id'] = chosen_role_id
                session.meta_json = json.dumps(_meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass
    _role_name, _role_extra = _BUILTIN_ROLES[chosen_role_id]
    # ==================================================================
    # 【榜单分析师角色专属·CRITICAL FIX】
    #   自动扫榜 = 只在 generate() 内部、首帧心跳已经发出之后 执行。
    #   ❌ 绝不能放这里（chat_general 外层=响应头还没返回=连接期卡死=前端fetchWithRetry 60s超时abort）。
    #   所以这里把扫榜需要的参数提前快照下来 → 真正执行放在 generate() 首帧 meta 之后。
    # ==================================================================
    _fc_snapshot: str = ''
    try:
        _bb_fc_snap = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
        if _bb_fc_snap:
            _fc_snapshot = (_bb_fc_snap.concept or _bb_fc_snap.master_outline or '').strip()
    except Exception:
        _fc_snapshot = ''
    _rank_analyst_snapshot: Dict[str, Any] = {
        'active': (chosen_role_id == 'rank_analyst'),
        'message': message,
        'fallback_concept': _fc_snapshot,
        'book_title': book_title or '',
    }
    # P1-3 提示词变量：{date} {time} {current_book} {model_name}，注入到 system_prompt + enriched user message
    # 注意：datetime/timezone/timedelta 一律走模块顶层导入；函数内再局部 import 会让
    # Python 把这些名字判为局部变量 → 早于 import 使用的行直接 UnboundLocalError（v1.0 实际踩过）
    _tz = timezone(timedelta(hours=8))
    _now = datetime.now(_tz)
    _var_ctx = {
        'date': _now.strftime('%Y-%m-%d'),
        'time': _now.strftime('%H:%M'),
        'current_book': book_title or '(未绑定作品)',
        'model_name': (cfg.model if 'cfg' in dir() and cfg else '') or (AIConfig.get_active().model if AIConfig.get_active() else ''),
    }
    def _var_replace(s: str) -> str:
        if not s: return s
        for k, v in _var_ctx.items():
            s = s.replace('{' + k + '}', str(v))
        return s
    # ============== system_prompt 合成：==============
    #   · 纯闲聊（无book_id）：沿用 build_general_chat_system_prompt 的自由聊天规则
    #   · 绑定作品（有book_id）：核心部分复用 build_chat_system_prompt（已完整注入构思/人物/世界观/核心规则/大纲/剧情线/伏笔/地点/文风 + TOC + 最近章节 + 章节号铁律）
    #       再叠加通用聊天专属规则（命中创作关键词→气泡+落卡、不要瞎编榜单、扫榜走Step1工具）
    _general_only = build_general_chat_system_prompt()
    if book_id and base_system:
        # 把通用聊天的"闲聊自由/命中创作话题时的行为/扫榜禁令"取出来，拼到 base_system 末尾（避免 base_system 的写作协作口吻覆盖掉闲聊自由）
        _extra_rule_lines = []
        _capture = False
        for _ln in _general_only.splitlines():
            if _ln.startswith('二、命中创作话题时的行为'):
                _capture = True
            if _capture:
                _extra_rule_lines.append(_ln)
        _extra_rules = '\n'.join(_extra_rule_lines).strip()
        system_prompt = base_system.rstrip() + "\n\n================================\n【通用聊天模式补充说明】\n"
        system_prompt += "- 在【设定/通用】里：作者既可讨论创作，也可能问无关创作的闲聊问题（编程/科普/生活…）。只要不是创作话题，就不要把话题往创作上扯，直接聊对应的话题内容，简洁有人情味。\n"
        system_prompt += f"- 当前作品《{book_title}》已填充维度库：{bb_summary or '暂无已填充维度'}。上述 bible 资料是作者已采纳落地的内容，回答任何创作相关问题时**以落地资料为准，不反着已采纳内容瞎编**（例如：落地资料里主角是姜离，就不要把林玄当主角）。\n"
        if _extra_rules:
            system_prompt += "\n" + _extra_rules + "\n"
    else:
        system_prompt = _general_only
    # 把当前 persona + 上下文变量 注入到 system_prompt 末尾（prepend 变量说明）
    _var_intro = f"【运行时上下文变量，可在回答中按需引用】\n- 今日日期：{_var_ctx['date']}\n- 当前时间：{_var_ctx['time']}\n- 当前绑定作品：{_var_ctx['current_book']}\n- 当前模型：{_var_ctx['model_name']}\n"
    if _role_extra:
        system_prompt = system_prompt.rstrip() + "\n\n【当前人格角色】用户已为本次会话切换到「" + _role_name + "」模式。严格按以下身份说明输出：\n" + _role_extra + "\n\n" + _var_intro
    else:
        system_prompt = system_prompt.rstrip() + "\n\n" + _var_intro
    # ============== 用户消息前置"引用前言"：命中写作相关时，只附加 system_prompt 里没给、但对本轮对话必须精准的资料 ============
    # 原则：system_prompt 里已经有完整 bible(9维度/TOC/最近章标题)，引用前言不重复；
    #       这里只补三样：① 本轮问题命中的"章节正文摘要"（这是 system_prompt 故意没给的，避免塞爆）② 人名速查表 ③ 若还缺具体维度再根据命中关键词补
    try:
        from general_chat_hitter import WRITING_TOTAL_HINTS as _WTH
    except Exception:
        _WTH = []
    _talking_creation = bool(_WTH) and any(h in message for h in _WTH)
    _lead_ref = ''
    if book_id and _talking_creation and bb:
        from app import Chapter, parse_chapter_number
        import re as _re
        lead_parts: list[str] = []
        lead_parts.append('（以下为系统引用：作者本轮问题要用到的精准资料。回答创作相关问题时**先看引用，再结合 system_prompt 里的完整维度**。）')
        # ==== A. 章节号提取 + 对应章节正文摘要注入 ====
        # 解析"第1章/第一章/改第03章/第 8 章/卷一第3章/这一章/本章"
        _msg_low = message
        _ch_num: int | None = None
        # 数字形式：第\s*(\d+)\s*章
        m1 = _re.search(r'第\s*([0-9零一二三四五六七八九十百千万两贰叁肆伍陆柒捌玖拾]+)\s*章', _msg_low)
        if m1:
            raw = m1.group(1).strip()
            try:
                _ch_num = parse_chapter_number(f'第{raw}章')
            except Exception:
                _ch_num = None
        # 汉字常见单独写法兜底
        if _ch_num is None:
            cn_map = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100,'千':1000,
                      '壹':1,'贰':2,'叁':3,'肆':4,'伍':5,'陆':6,'柒':7,'捌':8,'玖':9,'拾':10,'佰':100,'仟':1000,'万':10000}
            m2 = _re.search(r'第\s*([零一二三四五六七八九十百千万两贰叁肆伍陆柒捌玖拾佰仟万]+)\s*章', _msg_low)
            if m2:
                raw = m2.group(1).strip()
                val = 0; cur = 0; unit = 1
                for ch in raw:
                    if ch in cn_map and cn_map[ch] >= 10:
                        u = cn_map[ch]
                        if cur == 0: cur = 1
                        val += cur * u
                        cur = 0; unit = u
                    elif ch in cn_map and 1 <= cn_map[ch] <= 9:
                        cur = cn_map[ch]
                    else:
                        break
                if cur:
                    val += cur if val and unit == 1 else cur
                if val and 1 <= val <= 9999:
                    _ch_num = val
        # 兜底：用户说"这一章/本章/第一章/最后一章/刚写的"→ 取最近章节号 next_chapter_num-1
        if _ch_num is None:
            recent_aliases = ('这一章', '本章', '第一章', '最后一章', '刚写的', '刚改的', '现在写的这章', '你刚才写的', '你上条生成的', '这篇')
            if any(a in _msg_low for a in recent_aliases) and isinstance(next_chapter_num, int) and next_chapter_num > 1:
                _ch_num = next_chapter_num - 1
        _chapter_body_snippet = ''
        _chapter_title = ''
        if isinstance(_ch_num, int) and _ch_num >= 1:
            try:
                all_ch = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
                found = None
                # 优先按 parse_chapter_number 匹配
                for c in all_ch:
                    if parse_chapter_number(c.title or '') == _ch_num:
                        found = c; break
                # 兜底按 order_index == _ch_num 匹配（没写章号的草稿章通常 order_index = 章号）
                if not found:
                    for c in all_ch:
                        if int(getattr(c, 'order_index', 0) or 0) == _ch_num:
                            found = c; break
                if found:
                    _chapter_title = found.title or f'第{_ch_num}章'
                    body = (found.content or '').strip()
                    if body:
                        MAX_PREVIEW = 3800  # 单章正文引用硬上限（避免一章6000字+就撞截断）
                        if len(body) <= MAX_PREVIEW:
                            _chapter_body_snippet = body
                        else:
                            # 前 800 字（首钩子/出场）+ 后 2800 字（结尾/冲突点），中间提示省略（LLM最需要的是首尾两段）
                            head = body[:800]
                            tail = body[-2800:]
                            skip_cnt = len(body) - 800 - 2800
                            _chapter_body_snippet = (
                                head
                                + f'\n\n……【中间{skip_cnt}字已省略，仅保留首段钩子+末段高潮】……\n\n'
                                + tail
                                + f'\n（共{len(body)}字，截前800+后2800注入引用）'
                            )
            except Exception:
                _chapter_body_snippet = ''
                _chapter_title = ''
        if isinstance(_ch_num, int) and _ch_num >= 1:
            if _chapter_body_snippet:
                lead_parts.append(f'\n【引用：第{_ch_num}章正文】（标题：{_chapter_title}，共{len(_chapter_body_snippet)}字预览）\n{_chapter_body_snippet}')
            else:
                lead_parts.append(f'\n【引用：未找到第{_ch_num}章的正文原文】。请直接把该章原文贴到聊天里，或从【正文Tab→章节号{_ch_num}→复制正文后粘贴】。我拿到全文后再改。')
        # ==== B. 人物 JSON → 人名速查表（不重复 system_prompt 的长档案，只给 name + role + identity，LLM 定位人名快 10 倍）====
        _cp = (getattr(bb, 'character_profiles', '') or '').strip()
        if _cp.startswith('['):
            try:
                arr = json.loads(_cp)
                if isinstance(arr, list) and len(arr) > 0:
                    quick: list[str] = []
                    for item in arr:
                        if isinstance(item, dict):
                            nm = str(item.get('name') or '').strip()
                            if not nm:
                                continue
                            _r = str(item.get('role') or '').strip()
                            _id = str(item.get('identity') or '').strip()
                            line = f"- {nm}"
                            if _r: line += f"（{_r}）"
                            if _id: line += f" · {_id}"
                            quick.append(line)
                    if quick:
                        lead_parts.append(f'\n【引用：人名速查表】（落地{len(arr)}人）\n' + '\n'.join(quick[:40]) + (f'\n（省略{len(quick)-40}人）' if len(quick) > 40 else ''))
            except Exception:
                pass
        if len(lead_parts) > 1:
            _lead_ref = '\n'.join(lead_parts) + '\n————————引用结束————————\n【作者原话】\n'
    elif _talking_creation:
        # 纯闲聊命中创作关键词但没绑定作品 → 走 general_chat_hitter 原版前言（提示作品未绑定）
        _lead_ref = None
    if _lead_ref:
        user_with_ref = _lead_ref + message
    else:
        user_with_ref = wrap_message_with_context(message, book_title, bb_summary)
    enriched = _var_replace(user_with_ref)

    # ======= 节点设计师：续会 / 新卷启动 注入上下文 =======
    # 学习圆桌会议续会机制：命中"继续/接着/往下"类纯续会指令 → 加载 state
    # 从 meta_json['node_designer_state'] 拿到 last_ch，拼一段「从Y+1开始不要重复」的系统注入给 LLM
    # 命中明确"第N卷 节点设计"新卷指令 → 清掉旧 state（上卷进度作废，新卷从头来）
    _nd_is_node_role = (chosen_role_id == 'node_designer')
    _nd_state = None
    _nd_meta_for_closure: dict = {}  # 供闭包 generate() 写 state 用
    if _nd_is_node_role:
        new_vi = _is_nd_new_volume_request(message) if message else None
        is_continue = _is_nd_continue(message) if message else False
        # 先从会话历史提取一条"最近一次 AI 输出"，用于续会 state 缺失时兜底解析 last_ch/volume_index
        last_assistant_text = ''
        try:
            _hist_tmp = load_session_messages(session)
            if isinstance(_hist_tmp, list):
                for m in reversed(_hist_tmp):
                    if isinstance(m, dict) and m.get('role') == 'assistant' and str(m.get('content', '')).strip():
                        last_assistant_text = str(m.get('content', '') or '')
                        break
        except Exception:
            last_assistant_text = ''
        if new_vi is not None:
            # 作者明确启动"第N卷节点设计"新任务 → 清旧 state（开始新卷）
            _nd_clear_state(session, db)
        if is_continue:
            _nd_state = _nd_load_state(session)
            # state 缺失的兜底：从最近AI输出解析 last_ch/volume_index
            if not _nd_state:
                _vi = _parse_volume_index_from_text(message + '\n' + last_assistant_text) or 1
                _lc = _parse_last_chapter_from_text(last_assistant_text)
                if _lc > 0:
                    _nd_state = {'volume_index': _vi, 'cpv': 50, 'last_ch': _lc, 'volume_title': '',
                                 'updated_at': datetime.now(timezone.utc).isoformat()}
            if _nd_state:
                # 往 enriched（最终给LLM的用户消息末尾）追加续会上下文
                inject = _nd_build_continue_user_injection(_nd_state)
                if inject:
                    enriched = (enriched or '').rstrip() + '\n' + inject
                _nd_meta_for_closure = dict(_nd_state)
        else:
            # 非续会：启动新请求 → 建立初始 state（从用户消息里解析 volume_index / cpv）
            init_vi = None
            if new_vi is not None:
                init_vi = new_vi
            else:
                init_vi = _parse_volume_index_from_text(message)
            if init_vi is None:
                # 从历史 AI（前一条）里兜底看有没有"第X卷"
                init_vi = _parse_volume_index_from_text(last_assistant_text) or 1
            init_cpv = 50
            try:
                _mcpv = re.search(r'(?:cpv|每卷章数|章节数|共)\s*[=:：]*\s*(\d{1,3})\s*章', message or '')
                if _mcpv:
                    init_cpv = max(10, min(200, int(_mcpv.group(1))))
            except Exception:
                pass
            _nd_meta_for_closure = {
                'volume_index': init_vi or 1,
                'cpv': init_cpv,
                'last_ch': 0,
                'volume_title': '',
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }

    history = load_session_messages(session)
    # 【重新生成】truncate_history_to：仅保留历史前 N 条（含本条前的 user），丢弃旧 AI 回复及其后续，
    # 本消息再重新生成并追加——避免重复堆积；不传则该会话按原样继续。
    _trunc = data.get('truncate_history_to')
    if isinstance(_trunc, int) and not isinstance(_trunc, bool) and 0 <= _trunc < len(history):
        history = history[:_trunc]
    # 【P1-3 meta】把当前角色以 SSE meta 回传前端，便于右上角 chip UI 同步（保证刷新后 UI 显示的角色跟后端真正用到的一致）
    _p13_meta = {'role_id': chosen_role_id, 'role_name': _role_name, 'vars': _var_ctx}
    # 最大上下文：最近50条 + 首条保留（保留模型/角色/话题起始意图），应对长会话连续追问不轻易截断
    #   - 首条保留的原因：如果首条是"我要写都市异能/毒舌读者模式开始/切换模型"，后面聊到一半丢了，LLM 会不知道自己该用啥角色/啥题材。
    #   - 最近50条 ≈ 25 轮 user+assistant。
    #   - 单条正文>1500字：截尾部1500（最后一段才是本轮讨论的重点，截头部会把重要讨论信息丢了）。
    trimmed: list[dict] = []
    if isinstance(history, list):
        keep_head = history[:1] if len(history) > 0 else []
        keep_tail = history[-50:] if len(history) > 50 else history
        candidates = keep_head + [m for m in keep_tail if not (keep_head and keep_head[0] is m)]
        # 去重（避免首条同时出现在 keep_head 和 keep_tail 里导致重复）
        seen_ids: set[int] = set()
        for m in candidates:
            if not isinstance(m, dict):
                continue
            if 'content' not in m:
                continue
            h_id = id(m)
            if h_id in seen_ids:
                continue
            seen_ids.add(h_id)
            c = m.get('content')
            if isinstance(c, str) and len(c) > 1500:
                # 取尾部 1500 字（最后一段才是用户本轮之前的意图/对话），并标注已截尾
                c = '…（会话历史超长已截断，取尾部关键内容）\n' + c[-1500:]
            trimmed.append({'role': m.get('role') or 'user', 'content': c})
    # P0-4 深度思考程度：level>=1 时在 system 末尾按档位追加"先推演再给结论"的指令
    # 思考过程必须用【推理】...【推理结束】包裹 → 后端流式切分并单独展示给作者（可切换查看，不计入正文/落盘）。
    if deep_think >= 1:
        if deep_think >= 2:
            _banner = ("【深度思考·已开启】请先深入推演：拆解关键假设 → 列出逻辑链 → 权衡各方案取舍，再给出最终结论。"
                       "你的推演过程请写在『【推理】』与『【推理结束】』之间（仅作作者回顾，不算作答案正文），"
                       "推演结束后再清晰、可落地地下结论，不因推演而啰嗦。")
        else:
            _banner = ("【标准思考·已开启】先快速理清思路、对齐目标，再给结论。"
                       "请把简要思考过程放在『【推理】』与『【推理结束】』之间（仅作作者回顾，不算作答案正文），"
                       "然后给出简明可用的结论。")
        system_prompt = system_prompt.rstrip() + "\n\n" + _banner
    messages = [{'role': 'system', 'content': system_prompt}]
    # 【通用聊天·明确指令生成维度】作者用自然语明确要求"生成/创作某维度"时，
    # 按该维度的完整格式铁律（与对应维度生成的要求一致）产出长内容 + 标准 CARD 卡片（可采纳落地）。
    # 仅命中明确指令且已绑定作品才注入（未绑定作品无落库对象，就不打断普通聊天）。
    _gen_dim_list = _rt_general_dim_request(message) if book_id else None
    if _gen_dim_list:
        try:
            _gh_bb = BookBible.query.filter_by(book_id=book_id).first() if book_id else None
        except Exception:
            _gh_bb = None
        _gh_extra = []
        for _dk in _gen_dim_list:
            _gh_sys = _rt_create_dimension_system(_dk, book, '', '', '')
            # 去掉圆桌"共识取材"措辞，换成通用聊天的"按作者要求直接创作"
            _gh_sys = _gh_sys.replace('现在要根据一场"圆桌专家讨论"得出的共识', '现在要根据作者的明确要求')
            _gh_sys = _gh_sys.replace('必须以圆桌共识为唯一取材依据', '必须按下列维度的完整格式、直接创作出具体可落地的内容')
            _gh_extra.append(_gh_sys)
        _dim_label = '、'.join(_RT_CREATE_DIMS.get(_dk, [_dk, ''])[0] for _dk in _gen_dim_list)
        _instr = (
            f"\n\n================================\n【本轮为维度生成任务】作者要求生成：{_dim_label}。\n"
            "你只需严格按下面指定维度的完整格式要求，输出该维度的一份完整、具体、可直接写入设定库的长内容。\n"
            "输出完成后，**在回复末尾追加一张落地卡片**，格式严格为：\n"
            "[[CARD:卡片类型|标题|该维度的完整内容]]\n"
            "卡片类型从这些里面选：SAVE_CONCEPT/SAVE_RULE/SAVE_WORLDSETTING/SAVE_CHARACTER/"
            "SAVE_OUTLINE_NODE/SAVE_PLOT/SAVE_FORESHADOW/SAVE_LOCATION/APPLY_STYLE。\n"
            "正文给作者展示可读的排版版本，卡片放完整内容（两者内容一致）。\n"
        )
        for _dk in _gen_dim_list:
            _ct = _RT_CREATE_DIMS.get(_dk, (_dk, 'SAVE_CONCEPT'))[1]
            _instr += f"\n\n---------------【{_RT_CREATE_DIMS.get(_dk,(_dk,''))[0]}·完整格式要求】---------------\n" + _gh_extra[_gen_dim_list.index(_dk)]
        system_prompt = system_prompt.rstrip() + _instr
        messages[0]['content'] = system_prompt
    messages.extend(trimmed)
    messages.append({'role': 'user', 'content': enriched[:8000]})

    # P1-1 会话级切模型：优先级 req_ai_config_id > session.meta_json.ai_config_id > 全局激活
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
        cfg = None  # 指定配置但无key → 回退全局
    if cfg is None:
        cfg = AIConfig.get_active()
    # chat_general 通用闲聊链路：强制 _normalize_llm_base_url（HTTP 500的真凶）
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI'}), 400
    # 把当前选择持久化到 session.meta_json（保证下一轮聊天沿用同一模型，即会话级锁定）
    if chosen_cfg_id and chosen_cfg_id == cfg.id and session:
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict): meta = {}
            if meta.get('ai_config_id') != cfg.id:
                meta['ai_config_id'] = cfg.id
                session.meta_json = json.dumps(meta, ensure_ascii=False)
                db.session.add(session); db.session.commit()
        except Exception:
            pass  # 持久化失败不阻断主流程
    # 关键修复：之前直接 LLMGateway(cfg.base_url, cfg.api_key, cfg.model) 把 _normalize_llm_base_url 绕过了
    # 导致智谱GLM /v4 被强制拼 /v1 -> /v4/v1/chat/completions 404 -> Flask转成HTTP 500抛给前端
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
    # 把 model_name 变量同步为真实值，避免 system_prompt 末尾写"当前模型：(空)"
    _var_ctx['model_name'] = _mg
    gw = LLMGateway(_bg, _kg, _mg)
    # P1-5 MCP: 读 MCP_SERVERS_JSON → 为通用聊天加载 function calling tools（不改动其他维度的创作链路）
    mcp_tools: List[Dict[str, Any]] = []
    _mcp_registry = None
    try:
        from mcp_client import MCPToolRegistry
        _mcp_registry = MCPToolRegistry()
        mcp_tools = _mcp_registry.available_tools_for_llm()
    except Exception:
        mcp_tools = []

    def generate():
        yield ': ping-heartbeat-keepalive\n\n'
        full_text = []
        try:
            # P1-3 首帧 ⓪：把"正在生效的角色+上下文变量"告诉前端（保证刷新后UI显示的角色和后端真实生效的一致）
            yield f'data: {json.dumps({"type": "meta", "kind": "role_applied", "info": _p13_meta}, ensure_ascii=False)}\n\n'
            # 首帧 ①：命中维度建议（前端弹气泡"是否落入XX维度？"）
            if hit_suggestions:
                yield f'data: {json.dumps({"type": "meta", "kind": "hit_suggestions", "info": {"suggestions": hit_suggestions}}, ensure_ascii=False)}\n\n'
            # 首帧 ②：扫榜意图识别（前端自动弹出Step1入口）
            scan_intent = any(k in message for k in [
                '扫榜', '热榜', '爆款', '番茄小说', '起点中文', '七猫', '排行榜',
                '现在什么火', '什么书火', '趋势', '看看榜单',
            ])
            if scan_intent:
                yield f'data: {json.dumps({"type": "meta", "kind": "scan_intent", "info": {"detected": True}}, ensure_ascii=False)}\n\n'
            # P1-5 MCP: 有已注册 tools → 告诉前端"本次对话启用MCP tools N个"，并把 tools 透传给 LLM payload
            _mcp_native_kwargs: Dict[str, Any] = {}
            if mcp_tools:
                yield f'data: {json.dumps({"type": "meta", "kind": "mcp_tools", "info": {"count": len(mcp_tools)}}, ensure_ascii=False)}\n\n'
                _mcp_native_kwargs['tools'] = mcp_tools

            # ============== P0 真联网搜索接入 ==============
            # 0) 先把用户配置的搜索 Key 同步进 env（无 env 时才用；配置路由保存后立即生效）
            _sync_search_keys_from_preference()
            # 1) 是否需要搜：联网开关开启=强制搜（绕开创作类话题不搜的过滤）；未开启=启发式判定
            _need_search = _search_available and (web_search_enabled or should_use_web_search(message, trimmed))
            _search_ctx = ''
            if _need_search:
                try:
                    # 2) 先推"🔍联网搜索中…" meta 帧，前端马上给用户反馈
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_started", "info": {"query": message[:200]}}, ensure_ascii=False)}\n\n'
                    # 3) 同步执行搜索：Tavily/Exa/Brave（有 Key 时）→ DuckDuckGo HTML 兜底
                    _sr = run_web_search(message, num_results=5, timeout_per_engine=6.0)
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_done", "info": _sr.to_dict() if hasattr(_sr, "to_dict") else {"ok": False, "engine": getattr(_sr, "engine", ""), "count": 0, "error": getattr(_sr, "error", "")}}, ensure_ascii=False)}\n\n'
                    # 4) 把搜索结果格式化成 Markdown 列表，追加到用户消息末尾注入给 LLM
                    try:
                        _search_ctx = format_search_context_for_llm(_sr)
                    except Exception:
                        _search_ctx = ''
                except Exception as _se:
                    # 搜索失败绝对不打断主聊天链路
                    yield f'data: {json.dumps({"type": "meta", "kind": "web_search_done", "info": {"ok": False, "engine": "", "count": 0, "error": str(_se)[:300]}}, ensure_ascii=False)}\n\n'
                    _search_ctx = ''
            # 5) 如果搜到资料，追加到本轮 user message 末尾（放在最后，LLM 注意力最高）
            if _search_ctx:
                # messages 是外层变量引用，修改会生效到 gw_stream_with_hb
                if messages and messages[-1].get('role') == 'user':
                    messages[-1]['content'] = (str(messages[-1].get('content', '')) + '\n\n' + _search_ctx)[:12000]
            # 6) 模型原生联网参数（智谱 GLM 开原生 web_search 工具，质量比独立搜索更高；不消耗第三方 Key）
            try:
                _native_p = get_native_websearch_params(_mg, _bg, enabled=_need_search or (scan_intent and _search_available))
                if _native_p and isinstance(_native_p, dict):
                    # deep-merge 到 _mcp_native_kwargs（tools 数组保留，extra_body 解包）
                    if 'extra_body' in _native_p and isinstance(_native_p['extra_body'], dict):
                        _ex = _mcp_native_kwargs.setdefault('extra_body', {})
                        for _kk, _vv in _native_p['extra_body'].items():
                            if _kk == 'tools' and isinstance(_vv, list):
                                _ex['tools'] = list(_ex.get('tools') or []) + list(_vv)
                            else:
                                _ex[_kk] = _vv
                    elif 'tools' in _native_p and isinstance(_native_p['tools'], list):
                        _mcp_native_kwargs['tools'] = list(_mcp_native_kwargs.get('tools') or []) + list(_native_p['tools'])
            except Exception:
                _native_p = None

            # ====================================================================
            # 【榜单分析师专属·自动扫榜·流式响应期内执行（连接稳定=不超时）】
            # ====================================================================
            #   - 外层 _rank_analyst_snapshot.active = True 才进入；其他角色完全不沾
            #   - 先推一帧 roundtable_status（通用聊天也能显示），用户感知"正在抓榜"
            #   - 扫完把报告追加到 messages[0]（system prompt 末尾）→ LLM 立刻能基于风向回答
            #   - 100% try/except：失败 = 当没发生，榜单分析师正常回答（不拍脑袋即可），绝不崩溃原聊天
            # ====================================================================
            nonlocal system_prompt
            try:
                if _rank_analyst_snapshot.get('active'):
                    yield f'data: {json.dumps({"type": "meta", "kind": "roundtable_status", "info": {"text": "🧾 榜单分析师正在扫描番茄/起点新书榜，整理市场风向…（约8-15秒，说起点即扫起点榜，默认番茄）"}}, ensure_ascii=False)}\n\n'
                    _a = _rank_analyst_snapshot
                    _rs_rank, _rs_sse = _auto_rank_scan_from_nl(
                        _a.get('message', ''),
                        fallback_concept=_a.get('fallback_concept', '') or '',
                        book_title=_a.get('book_title', '') or '',
                        explicit_rank_scan=True,
                    )
                    if _rs_rank:
                        def _md_report_gen(rp: dict) -> str:
                            lines: list[str] = []
                            lines.append(f"📈 本轮真实扫榜情报（平台：{rp.get('platform_label','番茄新书榜')}）")
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
                            for key, zh in [('reader_buy_points', '读者买单要素（共性卖点）'),
                                            ('reader_abandon_points', '读者弃文毒点（共性避坑）'),
                                            ('title_formula_examples', '书名公式参考'),
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
                        _report_gen = _md_report_gen(_rs_rank)
                        if _report_gen:
                            # 把扫榜报告追加到 system prompt 末尾（对榜单分析师 persona 再强化一次）
                            _appendix = (
                                "\n\n================================\n"
                                "【★★★ 本轮对话前置·系统已自动扫榜成功 ★★★】\n"
                                "下面是刚从番茄/起点新书榜抓回来的真实榜单风向情报（TOP5/买卖点/书名公式/钩子/建议）——"
                                "**你必须优先吸收：回答开头先给用户展示情报摘要，再基于这些情报回答，不拍脑袋。**\n\n"
                                + _report_gen + "\n================================\n"
                            )
                            system_prompt = system_prompt.rstrip() + _appendix
                            if messages and messages[0].get('role') == 'system':
                                messages[0]['content'] = system_prompt
                            # 把复杂 text 先提变量，彻底避免嵌套 f-string + json.dumps 里写 \"（Python 语法不允许在 f-string {} 内用 backslash）
                            _plat = _rs_rank.get('platform_label', '番茄新书榜') if isinstance(_rs_rank, dict) else '番茄新书榜'
                            _nb = len((_rs_rank.get('books') or []) if isinstance(_rs_rank, dict) else [])
                            _sse_meta_obj = {
                                'type': 'meta',
                                'kind': 'roundtable_status',
                                'info': {
                                    'text': f'✅ 扫榜完成：{_plat}｜命中 {_nb} 本TOP书，接下来基于风向构思。'
                                }
                            }
                            yield f'data: {json.dumps(_sse_meta_obj, ensure_ascii=False)}\n\n'
            except Exception:
                pass  # 扫榜失败 = 静默跳过，不打断任何主流程

            # 原生思考推理程度控制（智谱 GLM）：GLM-5.3 强制思考、思考与正文共享
            # max_tokens——无法"思考不计入消耗"，只能按 deep_think 下发 reasoning_effort
            # 控制思考深度，避免思考先占满 max_tokens 导致正文为空（配合 chat_stream 的
            # "思考耗尽自动翻倍 max_tokens"双重兜底）。
            try:
                _nk = _native_reasoning_kwargs(_mg, deep_think)
                if _nk:
                    _mcp_native_kwargs.update(_nk)
            except Exception:
                pass

            # 通用聊天 max_tokens 按模型能力"不限"：给足 _DIM_MAX_TOKENS(131072)，
            # 交由 llm_gateway._effective_max_tokens 按模型已知/自学习输出上限钳制（deepseek→8192、
            # glm-5.3→131072…），不再按 deep_think 分档缩小；思考型模型正文为空时还有
            # chat_stream 的"思考耗尽自动翻倍 max_tokens"兜底。
            # emit_reasoning=True：思考文本以 SSE meta(kind=reasoning) 单独推前端（可展开看，不混正文）。
            # 【思考切分】deep_think>=1 时额外用 _ThinkingSplitter 把提示词式"【推理】…【推理结束】"
            # 包裹的思考从正文剥离展示；与原生 reasoning_content 两条路径并存，结果 content/卡片/落盘均不含思考。
            _splitter = _ThinkingSplitter() if deep_think >= 1 else None
            for chunk in gw_stream_with_hb(gw, messages, emit_reasoning=True,
                                           temperature=({0: 0.7, 1: 0.5, 2: 0.3}.get(deep_think)), max_tokens=_DIM_MAX_TOKENS, **_mcp_native_kwargs):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                if _is_stream_retry(chunk):
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": chunk.info}, ensure_ascii=False)}\n\n'
                    continue
                if _is_reasoning_frame(chunk):
                    # 原生 reasoning_content 思考 → 单独透传，不 append 进 full_text（结果/卡片/落盘只含最终回复）
                    yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": chunk.text}, ensure_ascii=False)}\n\n'
                    continue
                # 深思考标记式：把正文流切成 body / reason 两部分分发
                for _pk, _pt in (_splitter.feed(chunk) if _splitter else [('body', chunk)]):
                    if _pk == 'reason':
                        yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _pt}, ensure_ascii=False)}\n\n'
                    else:
                        full_text.append(_pt)
                        yield f'data: {json.dumps({"type": "delta", "content": _pt}, ensure_ascii=False)}\n\n'
            # 流结束：冲刷深思考切分器缓存的尾巴（避免正文/思考尾部被丢弃）
            if _splitter is not None:
                for _fk, _ft in _splitter.finish():
                    if _fk == 'reason':
                        yield f'data: {json.dumps({"type": "meta", "kind": "reasoning", "text": _ft}, ensure_ascii=False)}\n\n'
                    else:
                        full_text.append(_ft)
                        yield f'data: {json.dumps({"type": "delta", "content": _ft}, ensure_ascii=False)}\n\n'

            complete = ''.join(full_text)
            # ======= 节点设计师：流正常结束（含中途截断/没生成完）→ 更新续会 last_ch/volume_index =======
            if _nd_is_node_role and session and _nd_meta_for_closure:
                try:
                    _lc = _parse_last_chapter_from_text(complete)
                    _vi = _parse_volume_index_from_text(complete) or int(_nd_meta_for_closure.get('volume_index') or 1)
                    _cpv = max(10, int(_nd_meta_for_closure.get('cpv') or 50))
                    _new_state = dict(_nd_meta_for_closure)
                    _new_state['volume_index'] = _vi
                    _new_state['cpv'] = _cpv
                    if isinstance(_lc, int) and _lc > int(_new_state.get('last_ch') or 0):
                        _new_state['last_ch'] = _lc
                    _new_state['updated_at'] = datetime.now(timezone.utc).isoformat()
                    # 若本次 AI 输出里发现 CPV 信息（例如卷标题里"第X卷（共N章）"）→ 同步升级
                    try:
                        _mx_cpv = re.search(r'(?:共|全|每卷|chapter_count|cpv)\s*[=:：为是]*\s*(\d{1,3})\s*章', complete or '')
                        if _mx_cpv:
                            _cand = max(10, min(200, int(_mx_cpv.group(1))))
                            if _cand >= int(_new_state.get('last_ch') or 0):
                                _new_state['cpv'] = _cand
                    except Exception:
                        pass
                    _nd_save_state(session, db, _new_state)
                except Exception:
                    pass
            cards = parse_cards(complete)
            for c in cards:
                c['content'] = _clean_text_to_plain(c.get('content', ''))
                if c.get('title'):
                    c['title'] = _clean_text_to_plain(c['title'])
            # ======= 节点设计师：全卷已收尾(last_ch>=cpv) 但模型没吐完整全卷卡片 → 后端自动从历史所有分段卡片合并一张全卷统一采纳卡片 =======
            if _nd_is_node_role and _nd_meta_for_closure:
                try:
                    _vi = int(_nd_meta_for_closure.get('volume_index') or 1)
                    _cpv = max(10, int(_nd_meta_for_closure.get('cpv') or 50))
                    _final_lc = int(_parse_last_chapter_from_text(complete) or _nd_meta_for_closure.get('last_ch') or 0)
                    _is_full = _final_lc >= _cpv
                    # 若模型卡片的 nodes 不完整也同样兜底补齐：检查 cards 中 SAVE_PLOT 的总节点数
                    _existing_nodes_count = 0
                    for c in cards:
                        if c.get('type') != 'SAVE_PLOT':
                            continue
                        try:
                            arr = json.loads(c.get('content') or '[]')
                            if isinstance(arr, list):
                                for v in arr:
                                    if isinstance(v, dict) and isinstance(v.get('nodes'), list):
                                        _existing_nodes_count += len(v['nodes'])
                        except Exception:
                            pass
                    if _is_full and (_existing_nodes_count < _cpv or not cards):
                        # 收集所有历史分段卡片 + 本次 complete 里的卡片 → 合并成全卷统一卡片
                        all_vols, dvi, dcpv = _nd_collect_all_save_plot_volumes(history, complete)
                        vi_for_build = dvi or _vi
                        cpv_for_build = dcpv or _cpv
                        if all_vols or _is_full:
                            # 即使 all_vols 为空，只要 _is_full 并且 nodes 数 < cpv，也兜底：把 complete 里的节点再解析一次（走 _build_full_volume → A+C 修复自动补齐占位）
                            built = _nd_build_full_volume_card(all_vols, vi_for_build, cpv_for_build)
                            if built:
                                # 把这张兜底卡片追加到 cards（放在最末，前面模型给的卡片如果节点数不够也保留，不会冲突）
                                built['content'] = _clean_text_to_plain(built.get('content', ''))
                                if built.get('title'):
                                    built['title'] = _clean_text_to_plain(built['title'])
                                # 为避免重复：如果 cards 里已经有一张 SAVE_PLOT 节点数=cpv 并且 volume_index=vi_for_build → 不追加
                                _already_full = False
                                for c in cards:
                                    if c.get('type') != 'SAVE_PLOT':
                                        continue
                                    try:
                                        arr = json.loads(c.get('content') or '[]')
                                        if isinstance(arr, list) and len(arr) == 1 and isinstance(arr[0], dict):
                                            if int(arr[0].get('volume_index') or 0) == int(vi_for_build) and len(arr[0].get('nodes') or []) >= int(cpv_for_build):
                                                _already_full = True
                                                break
                                    except Exception:
                                        pass
                                if not _already_full:
                                    cards.append(built)
                except Exception:
                    pass
            for card in cards:
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            clean_text = _clean_text_to_plain(strip_cards(complete))
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending'} for c in cards]
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text,
                            'cards': persisted_cards})
            _safe_save_session_messages(session, history)
            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            import traceback
            traceback.print_exc()
            # ======= 节点设计师：异常退出（Render断连/超时/模型错误）→ 也要把已输出到的 last_ch 存起来，支持续会 =======
            if _nd_is_node_role and session and _nd_meta_for_closure:
                try:
                    partial = ''.join(full_text)
                    _lc = _parse_last_chapter_from_text(partial)
                    _vi = _parse_volume_index_from_text(partial) or int(_nd_meta_for_closure.get('volume_index') or 1)
                    _new_state = dict(_nd_meta_for_closure)
                    _new_state['volume_index'] = _vi
                    if isinstance(_lc, int) and _lc > int(_new_state.get('last_ch') or 0):
                        _new_state['last_ch'] = _lc
                    _new_state['updated_at'] = datetime.now(timezone.utc).isoformat()
                    _nd_save_state(session, db, _new_state)
                except Exception:
                    pass
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})
