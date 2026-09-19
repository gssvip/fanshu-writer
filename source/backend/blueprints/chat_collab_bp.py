"""聊天驱动创作 Blueprint：维度感知多轮对话 + Action Card 落地 + 进度引导。

把"表单填空"创作模式升级为"边聊边写"：
  - 聊天时自动注入当前书的相关 bible 维度（AI 真懂你的书）
  - AI 回复中可产出结构化“落地卡片”，用户点确认即写入对应维度
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
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from queue import Queue, Empty
from typing import Any, Dict, List, Optional  # 顶层显式导入，兼容CI高版本Python解释器

from flask import Blueprint, jsonify, request, Response, stream_with_context

chat_collab_bp = Blueprint('chat_collab', __name__)


# SSE 保活工具（SSE_HEARTBEAT_COMMENT / SSE_HB_INTERVAL_SEC / gw_stream_with_hb）
# 已抽到 sse_keepalive.py 独立模块，避免 chat_collab_bp.py 巨石继续增长（架构门禁约束）
from sse_keepalive import gw_stream_with_hb, SSE_HEARTBEAT_COMMENT, SSE_HB_INTERVAL_SEC, HEARTBEAT, _is_stream_retry
from llm_gateway import _is_reasoning_frame

# 会话消息持久化（load/_safe_save/断流抢救）已抽到 session_persist.py 独立模块
# （架构门禁约束：chat_collab_bp.py 行数禁止超过基线，新功能必须拆模块）
# 注意：session_persist.py 位于 backend 根目录（与 sse_keepalive.py 同级），
# 必须用绝对导入；`from .session_persist` 会在启动时 ModuleNotFoundError → Render 部署 503
from session_persist import (
    load_session_messages,
    _safe_save_session_messages,
    _save_partial_on_disconnect,
    _archive_load_full,
    _archive_count_map,
    _archive_update_card_status,
)

# 内置人格角色表（单一定义，通用聊天 & 圆桌会议共用）已外置 persona_config.py
from blueprints.persona_config import _PERSONAS, _ROUNDTABLE_ORDER, _MODERATOR_ROLE

# 节点设计师续会工具（断点续会/进度解析/全卷合并卡聚合）已外置 nd_helpers.py
# （对 parse_cards 的反向依赖在 nd_helpers 内部延迟导入，避免循环 import）
from blueprints.nd_helpers import (
    _ND_STATE_KEY,
    _is_nd_continue,
    _is_nd_new_volume_request,
    _parse_last_chapter_from_text,
    _parse_volume_index_from_text,
    _nd_save_state,
    _nd_load_state,
    _nd_clear_state,
    _nd_collect_all_save_plot_volumes,
    _nd_build_full_volume_card,
    _nd_build_continue_user_injection,
)
# SAVE_PLOT（timeline）采纳：卷级字段合并 + 节点 A+C 门禁 + 续会增量合并 已外置 nd_apply_card.py
# （纯 helper，仅懒依赖 node_design_bp / app，无循环 import，降低 chat_collab_bp.py 巨石）
from blueprints.nd_apply_card import (
    _extract_volume_index_safe,
    _volume_field_nonempty,
    _merge_volume,
    _repair_volume_nodes_safe,
    _merge_volume_nodes_incremental,
)


# ============================================================================
# max_tokens 按模型能力"给足"：统一取 _DIM_MAX_TOKENS（智谱 GLM-5.x 最大输出 131072，
# 也覆盖了 gpt-5/o1/claude 等大输出模型）。真正的"按能力不限"由 llm_gateway 落地：
# 已知模型表预钳制（deepseek-chat→8192、deepseek-reasoner→65536…）+ 400 报错自学习
# 真实上限，最终 max_tokens = min(本常量, 模型真实上限)，既不截断也不越界。
# ============================================================================
_DIM_MAX_TOKENS = 131072

# 深度思考的思考标记：deep_think>=1 时系统提示让模型把推演过程写在标记内，
# 前端用独立 reasoning 面板展示，正文/卡片/落盘均剥离（不参与复制/采纳）。
_REASON_START = '【推理】'
_REASON_END = '【推理结束】'

# 圆桌会议续会状态（持久化到 session.meta_json['roundtable_state']）：
# 目标是"开会中途异常/断连/用户手动停止后，说一声『继续』就能接着开会，不重新开始"。
_RT_STATE_KEY = 'roundtable_state'

# 用户"继续"指令识别：部分命中即可，避免误抢普通新话题。
_RT_CONTINUE_HINTS = ('继续', '接着', '续会', '下次开', '往下')
_RT_FULL_RE = re.compile(r'^\s*(?:继续圆桌会议|圆桌会议继续|继续圆桌|继续会议|会议继续|继续讨论|继续开会|接着开会|接着讨论|接着聊|接着|继续吧|继续|续会|没开完|没结束|往下开|往下聊|再来一轮|再来一次|再来|继续这轮|继续这轮[。.!！，,？?]*)\s*[。.!！，,？?]*\s*$')


# =============================================================================
# P0 榜单风向 × 智驾：上下文注入
#   - 前端先 POST /api/rank/scan-for-concept 拿到 RankScanReport（report），
#     再把 report 作为 rank_scan 字段塞进智驾相关 API 请求体。
#   - 后端统一用 _format_rank_context / _apply_rank_meta 两块：
#       1) 在 system_prompt 末尾追加"【市场风向·扫榜情报】"
#       2) 在 actionCard / 返回 meta 上附带 rankSourceLabel（前端副驾 subtitle 小字展示）
# =============================================================================

def _format_rank_context(rank_scan: dict | None) -> str:
    """把 RankScanReport 格式化为一段可被 system_prompt 直接注入的中文块。"""
    if not rank_scan or not isinstance(rank_scan, dict):
        return ''
    try:
        agg_label = str(rank_scan.get('rank_aggregate_label') or '').strip()
        meta = rank_scan.get('meta') or {}
        cats = meta.get('matched_categories') or []
        kws = meta.get('detected_keywords') or []
        snap = rank_scan.get('market_snapshot') or {}
        trend = (snap.get('trend_marker') or {}).get('label')
        tone = (snap.get('trend_marker') or {}).get('tone')
        openings = rank_scan.get('opening_patterns') or []
        populars = rank_scan.get('popular_elements') or []
        landmines = rank_scan.get('landmine_elements') or []
        formulas = rank_scan.get('title_formulas') or []
        gold_fingers = rank_scan.get('golden_finger_types') or []
        intro_f = rank_scan.get('intro_formulas') or []
        setting_sp = rank_scan.get('setting_selling_points') or []
        golden3 = rank_scan.get('golden_three_patterns') or []

        def _join(arr, cap=7):
            xs = [str(x).strip() for x in (arr or []) if str(x).strip()]
            if not xs:
                return '无'
            xs = xs[:cap]
            return '、'.join(xs)

        lines = ['【市场风向·扫榜情报（本轮创作必须对照以下情报）】']
        if agg_label:
            lines.append(f'· 扫榜口径：{agg_label}')
        if cats:
            lines.append('· 匹配分类新书榜：' + '；'.join(str(x) for x in cats[:4]) + ('（等）' if len(cats) > 4 else ''))
        if kws:
            lines.append('· 命中关键词：' + _join(kws, cap=10))
        if trend or tone:
            lines.append(f'· 市场判断：{trend or ""}（{tone or ""}）')
        lines.append('· 开篇钩子套路（新书榜 TOP 常用）：' + _join(openings))
        lines.append('· 读者买单要素（流行卖点）：' + _join(populars))
        lines.append('· 读者弃文毒点（务必回避）：' + _join(landmines))
        lines.append('· 书名公式范例：' + _join(formulas))
        if gold_fingers:
            lines.append('· 上榜书金手指类型拆解：' + _join(gold_fingers))
        if intro_f:
            lines.append('· 上榜书简介写法套路：' + _join(intro_f))
        if setting_sp:
            lines.append('· 上榜书核心设定卖点：' + _join(setting_sp))
        if golden3:
            lines.append('· 黄金三章结构套路：' + _join(golden3))
        lines.append('【执行要求】构思/设定/大纲/多选方案/圆桌讨论时：**优先吸收"读者买单要素"与"开篇钩子套路"并融合；避开"读者弃文毒点"；书名/方案标题可参考"书名公式范例"；金手指设计与黄金三章节奏优先参考"金手指类型拆解/黄金三章套路"**。若情报与用户明确指定相悖，以用户指定为准但需在结论里提示"这样做会偏离市场风向"。')
        return '\n'.join(lines)
    except Exception:
        return ''


def _get_rank_label(rank_scan: dict | None) -> str:
    if not rank_scan or not isinstance(rank_scan, dict):
        return ''
    return str(rank_scan.get('rank_aggregate_label') or '').strip()


def _enrich_card_rank_meta(card: dict, rank_scan: dict | None, extra_meta: dict | None = None) -> dict:
    """给落地卡片加 subtitle/rankSourceLabel 字段，便于前端 actionCard subtitle 展示风向来源。"""
    if not card or not isinstance(card, dict):
        return card
    rank_label = _get_rank_label(rank_scan)
    if rank_label:
        card['rankSourceLabel'] = rank_label
        card['subtitle'] = card.get('subtitle') or f"基于风向：{rank_label}"
    if extra_meta and isinstance(extra_meta, dict):
        for k, v in extra_meta.items():
            if v is not None:
                card[k] = v
    return card


def _persisted_rank_cards(cards, rank_scan):
    out = []
    for c in (cards or []):
        c2 = dict(c)
        _enrich_card_rank_meta(c2, rank_scan)
        out.append(c2)
    return out


# =============================================================================
# P0 榜单风向 × 智驾（自然语言触发）：用户在智驾对话框里用自然语言触发扫榜
#   支持句式：
#     · "先扫下番茄新书榜/起点新书榜再出设定"
#     · "扫榜看看市场风向"
#     · "先扫一下同类题材"
#     · "都市重生文，先扫番茄榜"
#   命中后：自动取「用户消息 + 构思/大纲上下文」作为扫榜 concept，
#           调 _core_rank_scan_for_concept() 拿到 report → 合并写入 _rank_scan
#           → 首帧 SSE 推送 meta kind=rank_scan 让前端实时渲染 RankScanCard
# =============================================================================

_RANK_SCAN_TRIGGER_RE = re.compile(
    r'(扫榜|扫.*榜|扫一下.*榜|看.*榜|查.*榜|分析.*榜|市场风向|风向|爆款分析|新书榜)',
    re.IGNORECASE
)
_RANK_SCAN_PLATFORM_FQ_RE = re.compile(r'(番茄|番茄榜|fanqie|fq|飞卢|小说榜|番茄新书榜)', re.IGNORECASE)
_RANK_SCAN_PLATFORM_QD_RE = re.compile(r'(起点|qidian|qd|起点榜|起点新书榜|起点中文网)', re.IGNORECASE)
_RANK_SCAN_STOP_RE = re.compile(r'(不要扫榜|不用扫榜|别扫榜|跳过扫榜|取消扫榜|忽略榜单|no rank|不扫榜|无需扫榜)', re.IGNORECASE)


def _auto_rank_scan_from_nl(message: str, *,
                            fallback_concept: str = '',
                            book_title: str = '',
                            explicit_rank_scan: dict | None = None) -> tuple[dict | None, dict | None]:
    """自然语言扫榜触发。
    返回 (rank_scan_payload, sse_meta_payload)：
      - rank_scan_payload 用于：_format_rank_context 注入 system prompt / _enrich_card_rank_meta 卡片打标
      - sse_meta_payload   用于：SSE 首帧 `meta kind=rank_scan` 推送，前端渲染 RankScanCard
    未触发时返回 (explicit_rank_scan, None)
    """
    # ① 若前端已通过 body.rank_scan 显式传了报告 → 沿用，但仍尝试 NL 触发做平台切换
    message = (message or '').strip()
    f_concept = (fallback_concept or '').strip()

    triggered = False
    if not _RANK_SCAN_STOP_RE.search(message) and _RANK_SCAN_TRIGGER_RE.search(message):
        triggered = True
    # ② 宽松触发：用户即使没说"扫榜"，直接说「番茄榜」「起点新书榜」+ 创作意图（≥15字）也命中
    if not triggered and (
            _RANK_SCAN_PLATFORM_FQ_RE.search(message) or _RANK_SCAN_PLATFORM_QD_RE.search(message)) \
            and len(message) >= 6:
        triggered = True

    # 平台优先级：NL 提到哪个平台 > explicit_rank_scan 里的 platform > 默认 fanqie
    platform = None
    if triggered:
        if _RANK_SCAN_PLATFORM_QD_RE.search(message):
            platform = 'qidian'
        elif _RANK_SCAN_PLATFORM_FQ_RE.search(message):
            platform = 'fanqie'
        elif explicit_rank_scan and isinstance(explicit_rank_scan, dict):
            platform = explicit_rank_scan.get('platform') or 'fanqie'
        else:
            platform = 'fanqie'

    # ③ 如果有显式传了 rank_scan 且 NL 没有任何新平台/触发意图 → 直接沿用原报告
    if not triggered and explicit_rank_scan:
        # 没触发但有 preset：作为 SSE 首帧仍然推一次，保证前端打开会话时能看到卡片
        report = _report_from_payload(explicit_rank_scan)
        if report:
            sse_meta = _rank_scan_to_sse_meta(explicit_rank_scan, platform or explicit_rank_scan.get('platform') or 'fanqie',
                                               from_nl=False, from_cache=bool(explicit_rank_scan.get('from_cache')),
                                               concept=explicit_rank_scan.get('concept') or f_concept or message)
            return explicit_rank_scan, sse_meta
        return explicit_rank_scan, None

    if not triggered:
        return explicit_rank_scan or None, None

    # ④ 构造扫榜 concept：message 本身（含触发词但不剥离，留作分类匹配信息）+ fallback + book_title
    candidates = [message]
    if f_concept and f_concept not in message:
        candidates.append(f_concept)
    if book_title and f'《{book_title}》' not in message:
        candidates.insert(0, f'《{book_title}》')
    scan_concept = '；'.join(x for x in candidates if x)[:500]

    # ⑤ 调内部核心函数（走同一缓存）
    try:
        from blueprints.novel_rank_bp import _core_rank_scan_for_concept  # 延迟导入避免循环
    except Exception:
        try:
            from novel_rank_bp import _core_rank_scan_for_concept  # type: ignore
        except Exception:
            # 兜底失败：把触发提示写成错误，不阻塞创作
            err_payload = {'ok': False, 'platform': platform, 'concept': scan_concept,
                           'error': '扫榜模块暂时不可用（import失败），创作继续'}
            return err_payload, _rank_scan_to_sse_meta(err_payload, platform or 'fanqie', from_nl=True, from_cache=False, concept=scan_concept)

    result = _core_rank_scan_for_concept(scan_concept, platform=platform or 'fanqie')
    if not result.get('ok'):
        err_payload = {'ok': False, 'platform': platform or 'fanqie', 'concept': scan_concept,
                       'error': result.get('error') or '扫榜失败，创作继续'}
        return err_payload, _rank_scan_to_sse_meta(err_payload, platform or 'fanqie', from_nl=True, from_cache=False, concept=scan_concept)

    report = result.get('report') or {}
    from_cache = bool(result.get('from_cache'))
    # 组装 rank_scan（格式与前端 preset 完全一致，保证 _format_rank_context / _enrich_card 复用）
    rank_scan = {
        'ok': True,
        'from_cache': from_cache,
        'platform': platform or 'fanqie',
        'concept': scan_concept,
        'report': report,
        # 展开扁平化字段：便于 _format_rank_context / 前端 RankScanCard 直接取
        **_flatten_report_fields(report),
    }
    sse_meta = _rank_scan_to_sse_meta(rank_scan, platform or 'fanqie', from_nl=True,
                                      from_cache=from_cache, concept=scan_concept)
    return rank_scan, sse_meta


def _flatten_report_fields(report: dict) -> dict:
    """把 report 里 meta/market_snapshot 字段扁平一层，供 _format_rank_context 的分支直接读。"""
    if not report or not isinstance(report, dict):
        return {}
    meta = report.get('meta') or {}
    snap = report.get('market_snapshot') or {}
    return {
        'rank_aggregate_label': report.get('rank_aggregate_label') or '',
        'meta': meta,
        'market_snapshot': snap,
        'matched_categories': meta.get('matched_categories') or [],
        'matched_books_count': len(report.get('top_books') or []),
        'opening_patterns': report.get('opening_patterns') or [],
        'popular_elements': report.get('popular_elements') or [],
        'landmine_elements': report.get('landmine_elements') or [],
        'title_formulas': report.get('title_formulas') or [],
        'golden_finger_types': report.get('golden_finger_types') or [],
        'intro_formulas': report.get('intro_formulas') or [],
        'setting_selling_points': report.get('setting_selling_points') or [],
        'golden_three_patterns': report.get('golden_three_patterns') or [],
        'sources_label': report.get('rank_aggregate_label') or '',
    }


def _report_from_payload(rank_scan: dict) -> dict | None:
    """从前端 preset 的 rank_scan dict 里取 report（兼容多种结构）。"""
    if not rank_scan or not isinstance(rank_scan, dict):
        return None
    r = rank_scan.get('report')
    if r and isinstance(r, dict):
        return r
    # 兜底：payload 本身就是 report
    if any(k in rank_scan for k in ('rank_aggregate_label', 'market_intel', 'matched_categories')):
        return rank_scan
    return None


def _rank_scan_to_sse_meta(rank_scan: dict, platform: str, *, from_nl: bool,
                           from_cache: bool, concept: str) -> dict:
    """包装为 SSE `type=meta kind=rank_scan` 消息体：前端直接 setRankScan(payload) 渲染 RankScanCard。"""
    report = _report_from_payload(rank_scan) or {}
    ok = bool(rank_scan.get('ok'))
    return {
        'type': 'meta',
        'kind': 'rank_scan',
        'info': {
            'ok': ok,
            'from_nl': from_nl,
            'from_cache': from_cache,
            'platform': platform,
            'concept': concept,
            'error': None if ok else rank_scan.get('error'),
            'report': report,
            'matched_categories': (report.get('meta') or {}).get('matched_categories') or rank_scan.get('matched_categories') or [],
            'matched_books_count': rank_scan.get('matched_books_count') or len(report.get('top_books') or []),
            'rank_aggregate_label': report.get('rank_aggregate_label') or rank_scan.get('rank_aggregate_label') or '',
            'opening_patterns': report.get('opening_patterns') or rank_scan.get('opening_patterns') or [],
            'popular_elements': report.get('popular_elements') or rank_scan.get('popular_elements') or [],
            'landmine_elements': report.get('landmine_elements') or rank_scan.get('landmine_elements') or [],
            'title_formulas': report.get('title_formulas') or rank_scan.get('title_formulas') or [],
            'golden_finger_types': report.get('golden_finger_types') or rank_scan.get('golden_finger_types') or [],
            'intro_formulas': report.get('intro_formulas') or rank_scan.get('intro_formulas') or [],
            'setting_selling_points': report.get('setting_selling_points') or rank_scan.get('setting_selling_points') or [],
            'golden_three_patterns': report.get('golden_three_patterns') or rank_scan.get('golden_three_patterns') or [],
            'market_snapshot': report.get('market_snapshot') or rank_scan.get('market_snapshot') or {},
            'market_intel': rank_scan.get('market_intel') or {},
            'scanned_at': report.get('scanned_at') or rank_scan.get('scanned_at') or '',
        }
    }


def _is_rt_continue(msg: str) -> bool:
    """圆桌会议「继续」指令识别：完全对齐节点设计师 _is_nd_continue 口径。
    - 严格命中 _RT_FULL_RE → True
    - 宽松版：开头是"继续/接着/往下"且整句极短（≤12字），不包含明确新议题关键词（含"讨论/议题：/开几轮/说一下/XXX的设定"等新议题 → 不判 continue）
    """
    m = (msg or '').strip().lstrip('，。,.！!？? ').strip()
    if not m:
        return False
    if _RT_FULL_RE.match(m):
        return True
    # 宽松版：和节点设计师一致口径
    if any(m.startswith(h) for h in ('继续', '接着', '往下开', '往下聊', '续会', '续开', '没开完', '没结束', '接着开')) and len(m) <= 12:
        # 避免"继续讨论一下新话题 XXX的设定"这种明确新议题
        new_issue_signals = ('讨论一下', '讨论：', '议题：', '说一下', '谈谈', '分析', '关于', '新议题', '开会讨论', '开个会')
        if not any(k in m for k in new_issue_signals):
            return True
    return False


# 可自动重试的流式网络异常：连接被上游提前掐断 / 读超时 / 连接层错误。
# LLMGateway 在"已收到部分内容"时会直接抛错不重试，这里在发言层再兜一层。
_RT_RETRY_RE = re.compile(
    r'ChunkedEncodingError|ConnectionError|ConnectionResetError|BrokenPipeError|ReadTimeout'
    r'|Read timed out|premature|RemoteProtocolError|503|502|bad gateway|unavailable|timed out|timeout',
    re.IGNORECASE)


def _rt_retryable(exc) -> bool:
    txt = f'{type(exc).__name__}: {exc}'
    return bool(_RT_RETRY_RE.search(txt))


def _rt_stream_turn(gw, messages, temperature, max_tokens, attempts: int = 2):
    """单次发言的带重试流式封装。
    yield ('hb'|'retry'|'reason'|'body', payload) —— 与上层既有的 SSE 帧拼装解耦。
    - 产出正文前遇到可重试网络异常（Chunked/超时/断连）→ 自动整轮重发（最多 attempts 次）
    - 已流出一段正文后仍异常 → 当场抛出，由上层落状态，等用户"继续"从该发言人续会
    - 正常结束 → 额外 yield ('__done__', 完整正文文本)
    """
    import time as _t
    _attempt = 0
    while True:
        _attempt += 1
        buf: list = []
        splitter = _ThinkingSplitter()   # 每次重试重建，避免残留推理标记缓存
        try:
            for chunk in gw_stream_with_hb(gw, messages, emit_reasoning=True,
                                           temperature=temperature, max_tokens=max_tokens):
                if chunk is HEARTBEAT:
                    yield ('hb', None)
                    continue
                if _is_stream_retry(chunk):
                    yield ('retry', chunk.info)
                    continue
                if _is_reasoning_frame(chunk):
                    yield ('reason', chunk.text)
                    continue
                for _pk, _pt in splitter.feed(chunk):
                    if _pk == 'body':
                        buf.append(_pt)
                    yield (_pk, _pt)
            for _fk, _ft in splitter.finish():
                if _fk == 'body':
                    buf.append(_ft)
                yield (_fk, _ft)
            yield ('__done__', ''.join(buf))
            return
        except Exception as e:
            if buf:
                # 已流出正文 → 不重试，交给上层续会
                raise
            if _attempt < attempts and _rt_retryable(e):
                _t.sleep(min(2 ** _attempt, 4))
                continue
            raise


# ===== 圆桌进度持久化【终极·独立小表方案】=====
# 背景血泪：ai_sessions ORM 类根本没定义 meta_json 列 → PostgreSQL 不存在该列 → 所有写 meta_json=内存属性被 SQLAlchemy 忽略
#   → state 从来没真正落过盘！还报 ProgrammingError: column "meta_json" does not exist。
#   同时 Flask 请求 db.session 在用户点停止（GeneratorExit）时会被回滚，finally commit 被冲掉。
# 方案：单建 roundtable_state 小表，session_id 字符串 PK + state_json 全文 + updated_at 时间戳。
#   用 CREATE TABLE IF NOT EXISTS，连 Alembic 迁移都不需要；读写用全新独立 engine/session，和请求范围彻底隔离。
_RT_STATE_TABLE = 'roundtable_state'


def _rt_ind_connect():
    """返回 (engine, SessionMaker) — 独立DB连接，不被请求回滚。"""
    import os as _os_rt
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker as _sm
    _db_uri = (_os_rt.environ.get('DATABASE_URL')
               or _os_rt.environ.get('SQLALCHEMY_DATABASE_URI')
               or 'sqlite:///' + _os_rt.path.join(_os_rt.path.dirname(_os_rt.path.abspath(__file__)), '..', 'app.db'))
    _eng = _ce(_db_uri, pool_pre_ping=True, connect_args={'check_same_thread': False} if _db_uri.startswith('sqlite') else {})
    _SM = _sm(bind=_eng, autoflush=False, expire_on_commit=False)
    return _eng, _SM


def _rt_ind_ensure_table(ind_sess):
    from sqlalchemy import text as _t
    # 幂等：IF NOT EXISTS，两边 DB 都支持；注意 PG 用 TIMESTAMP，SQLite 用 DATETIME，但写 TEXT 也兼容
    ind_sess.execute(_t(f"""
        CREATE TABLE IF NOT EXISTS {_RT_STATE_TABLE} (
            session_id VARCHAR(36) PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT
        )
    """))
    ind_sess.commit()


def _rt_save_state(session, db, state: dict) -> str:
    """【F2】圆桌进度 100% 持久化：
    (1) 独立连接 + 独立 roundtable_state 小表写入（绝对不回滚、不依赖 ai_sessions.meta_json 列不存在的问题）
    (2) 写完新连接再 SELECT 回来双重校验
    (3) 内存属性再赋值 session.meta_json（兼容其他读 session.meta_json.xxx 的旧代码，本请求生命周期内好使）
    返回："ok:verify_ok,done=N" 或 "fail:原因:详情"
    """
    _sid = str(getattr(session, 'id', '') or '').strip()
    _msg = 'ok:noop'
    try:
        if not _sid:
            return 'fail:no_sid'
        if not isinstance(state, dict):
            return 'fail:state_not_dict'
        _eng, _SM = _rt_ind_connect()
        try:
            _ind_sess = _SM()
            try:
                _rt_ind_ensure_table(_ind_sess)
                from sqlalchemy import text as _t
                _json = json.dumps(state, ensure_ascii=False)
                from datetime import datetime, timezone
                _ts = datetime.now(timezone.utc).isoformat()
                # SQLite/PG 都支持的 upsert 写法：先尝试 UPDATE，受影响=0 再 INSERT
                _up = _ind_sess.execute(
                    _t(f"UPDATE {_RT_STATE_TABLE} SET state_json = :sj, updated_at = :ts WHERE session_id = :sid"),
                    {'sj': _json, 'ts': _ts, 'sid': _sid}
                )
                if getattr(_up, 'rowcount', 0) == 0:
                    _ind_sess.execute(
                        _t(f"INSERT INTO {_RT_STATE_TABLE} (session_id, state_json, updated_at) VALUES (:sid, :sj, :ts)"),
                        {'sid': _sid, 'sj': _json, 'ts': _ts}
                    )
                _ind_sess.commit()
                # ===== 双重校验 =====
                _ind2 = _SM()
                try:
                    _row = _ind2.execute(_t(f"SELECT state_json FROM {_RT_STATE_TABLE} WHERE session_id = :sid LIMIT 1"), {'sid': _sid}).fetchone()
                    if _row is None:
                        _msg = 'fail:verify_no_row_after_upsert'
                    else:
                        try:
                            _re_read = json.loads(str(_row[0]) or '{}')
                            _dn = len(_re_read.get('done') or []) if isinstance(_re_read, dict) else -1
                            if not isinstance(_re_read, dict):
                                _msg = 'fail:verify_json_not_dict'
                            else:
                                _msg = f'ok:verify_ok,done={_dn}'
                        except Exception as _e2:
                            _msg = 'fail:verify_parse:' + str(_e2)[:60]
                finally:
                    try: _ind2.close()
                    except Exception: pass
            finally:
                try: _ind_sess.close()
                except Exception: pass
        finally:
            try: _eng.dispose()
            except Exception: pass
        # —— 兼容：把 state 同时写进内存 session.meta_json 属性（不是DB列，仅本请求生命周期内好使）——
        try:
            _cur = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(_cur, dict): _cur = {}
            _cur[_RT_STATE_KEY] = state
            session.meta_json = json.dumps(_cur, ensure_ascii=False)
        except Exception:
            pass
    except Exception as _e:
        try:
            _msg = 'fail:' + type(_e).__name__ + ':' + str(_e)[:100]
        except Exception:
            _msg = 'fail:unknown'
    return _msg


def _rt_load_state(session):
    """读圆桌进度：
    (1) 优先独立 roundtable_state 小表读（最新真持久化的值）
    (2) 兼容 fallback：读内存 session.meta_json 属性
    """
    _sid = str(getattr(session, 'id', '') or '').strip()
    if _sid:
        _st = _rt_load_state_by_sid_independent(_sid)
        if isinstance(_st, dict):
            # 同步写入内存属性，保持后续读 meta_json 路径一致
            try:
                _cur = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
                if not isinstance(_cur, dict): _cur = {}
                _cur[_RT_STATE_KEY] = _st
                session.meta_json = json.dumps(_cur, ensure_ascii=False)
            except Exception:
                pass
            return _st
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict):
            st = meta.get(_RT_STATE_KEY)
            return st if isinstance(st, dict) else None
    except Exception:
        pass
    return None


def _rt_load_state_by_sid_independent(sid: str):
    """独立新连接从 roundtable_state 小表读 state；表不存在/无行 → 返回 None。"""
    try:
        sid = str(sid or '').strip()
        if not sid: return None
        _eng, _SM = _rt_ind_connect()
        try:
            _s = _SM()
            try:
                _rt_ind_ensure_table(_s)
                from sqlalchemy import text as _t
                _r = _s.execute(_t(f"SELECT state_json FROM {_RT_STATE_TABLE} WHERE session_id = :sid LIMIT 1"), {'sid': sid}).fetchone()
                if _r is None: return None
                _m = json.loads(str(_r[0]) or '{}') if _r[0] is not None else None
                return _m if isinstance(_m, dict) else None
            finally:
                try: _s.close()
                except Exception: pass
        finally:
            try: _eng.dispose()
            except Exception: pass
    except Exception:
        pass
    return None


# ===== 节点设计师续会（学习圆桌会议续会方案）=====
# 进度存放在 session.meta_json['node_designer_state']，字段：
#   volume_index: int       第几卷
#   cpv: int                本卷总章数（默认 50）
#   last_ch: int            已输出到的最大章号（0=未开始，50=整卷完成）
#   volume_title: str       （可选）卷标题
#   updated_at: str         ISO timestamp
# 目标：生成到一半异常/断连/用户手动停止后，用户说「继续/接着/往下生成」就能从 last_ch+1 开始接着写，
#       不会从头再来；最终采纳落地时按章节号增量合并，不重复覆盖之前已采纳的节点。


_RT_TOPIC_PREFIX = '圆桌会议议题：'


def _rt_persist_messages(session, history, topic, moderator_open, done, summary='', summary_cards=None):
    """把圆桌进度渲染成"可复盘"的消息落盘到会话 → 刷新界面不丢。

    幂等：每次都基于 history 里的通用历史 + 当前圆桌状态重建"圆桌块"，
    异常只落已完成的发言（未完成的那位不写入），断连/手动停止后刷新即见。
    只做展示，LLM 续会上下文仍以 meta_json['roundtable_state'] 全量为准。
    """
    import copy as _copy
    try:
        disp = _copy.deepcopy(history) if isinstance(history, list) else []
        if not isinstance(disp, list):
            disp = []
        # 剔除历史里旧的"本次圆桌"块（以最新议题/进度为准，避免重复叠加）
        cut = None
        for i, m in enumerate(disp):
            if isinstance(m, dict) and m.get('role') == 'user' and str(m.get('content', '')).startswith(_RT_TOPIC_PREFIX):
                cut = i
        if cut is not None:
            disp = disp[:cut]
        block = [{'role': 'user', 'content': f'{_RT_TOPIC_PREFIX}{topic}'}]
        if moderator_open:
            block.append({'role': 'assistant', 'content': f'【{_MODERATOR_ROLE[0]}】\n{moderator_open}'})
        for d in (done or []):
            if isinstance(d, dict) and d.get('content'):
                block.append({'role': 'assistant', 'content': f'【{d.get("name","")}】\n{d.get("content","")}'})
        if summary:
            _msg = {'role': 'assistant', 'content': f'【总结报告】\n{summary}'}
            if summary_cards:
                _msg['cards'] = [{'status': 'pending', **c} for c in summary_cards if isinstance(c, dict)]
            block.append(_msg)
        disp.extend(block)
        _safe_save_session_messages(session, disp)
    except Exception:
        pass


class _ThinkingSplitter:
    """流式把『【推理】...【推理结束】』标记内的思考从正文中切出。

    深思考=提示词式（不依赖模型原生 reasoning_content，任意模型都可用）。
    feed() 逐块送入正文增量，yield ('body'|'reason', text)：
      - 'body'   正常正文 → 照常 push delta / 计入 full_text（标记本身被剥离）
      - 'reason' 思考片段 → 单独推 SSE meta(kind=reasoning)，不计入正文
    支持标记被 SSE 分块切断（缓存尾部半个标记）与单轮多次思考。
    """

    __slots__ = ('buf', 'in_reason')

    def __init__(self):
        self.buf = ''          # 缓存可能只含半个标记的尾部
        self.in_reason = False

    def feed(self, chunk: str):
        text = self.buf + chunk
        self.buf = ''
        while True:
            if not self.in_reason:
                idx = text.find(_REASON_START)
                if idx == -1:
                    keep = max(0, len(text) - (len(_REASON_START) - 1))
                    if text[:keep]:
                        yield ('body', text[:keep])
                    self.buf = text[keep:]
                    return
                if text[:idx]:
                    yield ('body', text[:idx])
                self.in_reason = True
                text = text[idx + len(_REASON_START):]
            else:
                idx = text.find(_REASON_END)
                if idx == -1:
                    keep = max(0, len(text) - (len(_REASON_END) - 1))
                    if text[:keep]:
                        yield ('reason', text[:keep])
                    self.buf = text[keep:]
                    return
                if text[:idx]:
                    yield ('reason', text[:idx])
                self.in_reason = False
                text = text[idx + len(_REASON_END):]

    def finish(self):
        """流结束时冲刷缓存尾巴，避免正文/思考尾部被丢掉。"""
        if self.buf:
            yield (('reason' if self.in_reason else 'body'), self.buf)
            self.buf = ''


def _native_reasoning_kwargs(model: str, deep_think: int) -> dict:
    """智谱 GLM 原生思考模型的推理程度控制（OpenAI 兼容顶层参数）。

    【默认关闭思考】deep_think=0（用户开关未开）时一律发 thinking.type=disabled：
      - GLM-4.x/5.0/5.1 本就支持关闭 → 真关闭；
      - GLM-5.2/5.3 若属强制思考模型拒收 disabled，由 llm_gateway.chat_stream 的
        【思考禁用自愈】退回 enabled+reasoning_effort=low 最轻档兜底（只多一次往返）。
    deep_think>=1 时按档位下发 reasoning_effort：
      >=2 → max 深度推理；==1 → high 增强推理。
    仅对 GLM 生效；非 GLM 模型（deepseek-reasoner 等）不注入，防参数报错。
    """
    m = (model or '').lower()
    if 'glm-' not in m:
        return {}
    if 'glm-5.3' in m or 'glm-5.2' in m:
        if deep_think <= 0:
            return {'thinking': {'type': 'disabled'}}
        return {
            'thinking': {'type': 'enabled'},
            'reasoning_effort': {2: 'max', 1: 'high'}.get(deep_think, 'low'),
        }
    # 更早 GLM（4.x / 5.0 / 5.1）：thinking 可开关，deep_think=0 关闭、>=1 开启
    return {'thinking': {'type': 'enabled' if deep_think >= 1 else 'disabled'}}


def _dim_max_tokens(dim_key: str) -> int:
    """维度生成 max_tokens（按模型能力给足 _DIM_MAX_TOKENS，防任何维度截断）。"""
    return _DIM_MAX_TOKENS


def _run_blocking_with_heartbeat(blocking_fn, sse_fn, extra_frames=None):
    """在线程里跑 blocking_fn()，主 generator 按 SSE_HB_INTERVAL_SEC 周期 yield 心跳，直到返回。
    - 先 yield 1 帧心跳立即占坑，再按间隔发后续心跳。
    - sse_fn(payload) -> str：用于发 data 帧；心跳是纯冒号注释帧，直接拼字符串。
    - extra_frames: 可选 list[str] 原始帧（含\\n\\n），在 blocking_fn 跑的过程中穿插发（用于进度通知）。
    - 返回：blocking_fn 的返回值。
    """
    result_box = []
    exc_box = []

    def _worker():
        try:
            result_box.append(blocking_fn())
        except Exception as e:
            exc_box.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    extra_iter = iter(extra_frames or [])
    while True:
        t.join(timeout=SSE_HB_INTERVAL_SEC)
        if not t.is_alive():
            break
        # 插 1 条原始帧再插心跳
        try:
            raw = next(extra_iter)
            yield raw
        except StopIteration:
            pass
        yield SSE_HEARTBEAT_COMMENT
    if exc_box:
        raise exc_box[0]
    return result_box[0] if result_box else None


# ----------------------------------------------------------------------------
# 会话隔离工具：严格按 book_id 校验 session 归属，防【串书记忆混乱】
# - 前端若把 A 书的 session_id 传给 B 书请求（URL/localStorage/state 残留常见）
#   会直接 load 其他书的历史对话（人设+剧情+正文）进 prompt → 污染新书
# - 策略：不匹配就丢弃旧 session，静默创建新 session 并清空历史（历史对话绝不跨书迁移）
# ----------------------------------------------------------------------------
def _get_or_create_session_for_book(session_id, book_id, scope='general', title='新会话'):
    """session 安全获取器：book_id 不匹配时创建新 session。永不跨书迁移历史。"""
    from app import db, AISession
    session = None
    if session_id:
        session = AISession.query.get(session_id)
        # ===== 隔离铁律：session.book_id != 当前请求 book_id → 坚决丢弃（防串书）=====
        if session is not None and str(getattr(session, 'book_id', None)) != str(book_id):
            session = None
    if not session:
        session = AISession(book_id=book_id, scope=scope, title=title[:30], messages_json='[]')
        db.session.add(session)
        db.session.commit()
    return session


# 滑窗上下文：保留最近 N 轮 + 系统提示，超出则保留首尾、中间摘要
MAX_HISTORY_ROUNDS = 8
# 单条消息最大字符（超长截断，防 token 爆炸）
MAX_MSG_CHARS = 2000

# ============================================================================
# 统一去AI味规则（正文写作/正文修改/去AI Tab 三处共用，保证口径一致）
# ============================================================================
DEAI_RULES = """去AI味与行文消杀（写作正文时主动规避，去AI时多做减法/替换，少做润色扩写）

> 执行原则：去AI味是做减法与替换，严禁二次润色扩写。优先级：删 > 换 > 调。

1. 【禁令0·最高优先级】AI修正式否定句密集症（全章禁绝，违者重写）
- 严禁任何形态的否定修正句：
  - ❌ 「不是X是Y / 不是X。是Y / 不是X，准确说是Y / 不是A也不是B——是C」
  - ❌ 嵌套≥2层的修正句。
  - ❌ 动作/感知排比：「更X，更Y，更Z」「看见…看见…」「举起…落下…」。
  - ❌ 「但/至少不该/其实/准确说/严格来说」类自我修正、抠字眼句。
- 处理方式：一律改写为直接陈述客观事实的单句。

2. 四大核心AI病句消杀
- 消灭解释腔：
  - 删去一切借角色或旁白解释世界观/因果的句子。凡删掉后不影响剧情理解的，一律删净。
  - 严禁用总结句收尾，段尾一律停在动作、物理反馈或环境细节上。
- 消灭伪对白：
  - 角色在紧张/隐瞒/愤怒时严禁说逻辑完整的大长句，剔除自报家门、自我剖析式的背书台词。
- 打破工整句式：
  - 严禁连续多段等长、严禁排比三连、严禁书面路标词串联。语言要有自然的停顿、粗糙感与跳跃感。
- 消灭悬空动作及被动句：
  - 禁止身体部位当主语独立成句。动作必须有人扛——如"谁的手、谁在看、谁在说"，动作的发出者必须明确。
  - 每个动作句都要让读者知道"谁在干什么"。主语可以是承前省略的人称，但不能凭空冒出一个悬空的部位或感官结果。
  - 禁用被动句来写抽象吃亏，这些必须改成具体画面；被动句一律改写为主动句。

3. 绝对禁用词库（黑名单）
- 必删修饰与伪动作词：
  `一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为、甚为、极为、缓缓、淡淡、轻轻、微微、毫无疑问、毋庸置疑、不言而喻、深吸一口气、眼中闪过一丝、心中暗想、心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、淡漠、漠然、眸子、嘴角微微上扬、周身、周遭、气息、威压、那道身影、说话间、话音未落、当即、顿时、瞬时、有意思、深深一眼`
- 必删书面逻辑连接词与废话词：
  `然而、同时、此外、更重要的是、总而言之、总的来说、综上所述、换句话说、换言之、也就是说、不难看出、可以说、可以这么说、需要指出的是、需要说明的是、值得注意的是、值得一提的是、不可否认、显而易见、众所周知、因此、显然、事实上、本质上、由此可见、归根结底、从某种意义上说`
- 必删书面关联句式：「虽然…但是…」「不仅…而且…」「首先…其次…最后…」三段式列举、破折号「——」、同句「了」字堆砌≥3个、排比三连句，一律 0 次命中。
- 必删「X地」副词模板：冷冷地/悄悄地/快速地/慢慢地/死死地/狠狠地/默默地/静静地 全部替换为动作、声音、触感描写；被字句全章 ≤1 处，一律翻成主动语态（"杯子被他捏碎了"→"他捏碎了杯子"）。
- 必删分析报告术语（正文绝不能出现，像AI把PPT硬塞进小说）：
  `核心动机、信息边界、信息落差、利益最大化、底层逻辑、认知差、降维打击`
- 必删套话比喻（含"像/如"字根的隐喻一并计入）：
  `像、如、如同、宛如、犹如、恍若、宛若 + 大海/巨龙/深渊/星河/铁水/碴子/寒流（及任意组合）`
  比喻/拟人全章 ≤1 处，多写在关键节点，禁止处处设喻、禁止隐喻连缀凑排比。
- 惊讶标记词限流：仿佛/忽然/竟/竟然/猛地/猛然/蓦然/骤然 全章合计 ≤1 次。
- 必删结尾升华词：
  `成长、命运、人性、选择、未来、从此、那一刻、这一场……`

4. 行文消杀自检（生成前底层自校）
1. 画面感检验：细节是否包含真实的温度、气味、触感、声音？不明确的虚词是否已删净？
2. 连接词硬切：句子之间是否已去掉"于是、因此、便、就"，直接用句号硬切推进？
3. 动词粗化：是否使用了最直接、粗粝、具体的动作动词，而非抽象概括词？

5. 爽点情绪克制（禁止情绪全盘外露）
- 爽点/高潮处禁止直呼情绪词（狂喜、兴奋到极点、怒不可遏、热血沸腾、按捺不住、心潮澎湃）。
- 情绪一律靠动作、停顿、留白、旁人震惊侧写去带，最多给"半句内心碎片"，禁止整段情绪铺陈与排比抒情。

6. 数值反馈节律（防"打脸即刷屏"，**仅对含系统面板/数值流的小说生效；无系统、纯武力/境界设定的文不适用本条**）
- 若本书存在"上线刷系统面板/数值"的表现体系：禁止"每次打脸/每次升级后都即时弹系统数值面板"。
- 数值/数据反馈须克制：一章内系统数值面板最多启用 1 次，数字只给大节点；其余用体感代替（气血翻涌、经脉跳动、呼吸发烫），同项数值不重复复读弹窗。
- 判定书是否适用本条：看全书体系是否含系统无障碍、数据面板、经验/属性点这类数值流设定，是则生效，否则跳过。

7. 对白短糙带情绪（正面铁律）
- 对白必须"短、糙、带情绪"：一两句收束，口语粗粝、不修边幅、带明显情绪与潜台词。
- 禁止正式书面长台词、禁止高密度"精确数字+威胁"的台词本腔；对白靠动作与停顿带语气，不靠形容词堆砌。

8. 描写减法（环境近乎归零 + 禁倒装 + 禁碎动作链）
- 环境描写近乎为零：凡与当前动作/情绪无直接因果的景物、体征、物什清单直接删净；确需的环境一笔带过，并入动作或对白。
- 严禁倒装句：禁止把动作/感情成分前置的书面倒装（如"眼前一亮的他"），一律改回主语在前的直陈。
- 严禁碎动作链：禁止把一个连续动作切成多段短句逐个断句（"他转身、抬手、抓住、用力"一节一逗）；动作链要合并成带主语的连续动作句，谁也不许单句独行。
（注：全章词频/句式命中由平台后置校验器统一扫描，作者只管写对，不必自查计数。）""".strip()

# ----------------------------------------------------------------------------
# 【阶段隔离·规则拆分】不同阶段只注入该阶段需要的规则：
#   GENERAL_CORE_RULES（三阶段通用总则）/ CONCEPTION_EXTRA_RULES（构思 JSON 约束）
#   WRITING_STYLE_RULES（正文行文）/ DEAI_ONLY_RULES（去AI 专用）。
# DEAI 阶段不注入 WRITING_STYLE_RULES，避免禁词表/执行流程等大块内容重复 2 遍。
# ----------------------------------------------------------------------------

DEAI_ONLY_RULES = DEAI_RULES  # 别名：DEAI_RULES 就是"去AI阶段专用"的完整规则

GENERAL_CORE_RULES = """
创作总则（架构与叙事底色，正文、大纲、设定、人物、世界观、伏笔等全纬度必须遵守本规则）

1. 冰山理论与情节结构
- 水上 1/8（明线反馈）：情节推进、核心爽点、战力/升级反馈、资源获取必须直白清晰，直接喂给读者，严禁过度隐晦。
- 水下 7/8（暗线伏笔）：深层动机、世界观隐秘、伏笔、历史创伤只露一角（通过反常行为、古籍残卷、旁人失态暗示），留给读者脑补，严禁写成设定说明书。
- 起承转合：
  - 起：直接切入冲突、危机、反常或金手指，严禁慢热铺垫与大段世界观宣讲。
  - 承：按情绪节拍推进，每个小场景均需具备微爽感或小悬念，不准空转。
  - 转：转折来自已埋伏笔或人物核心动机，严禁天降巧合。
  - 合：结尾留钩子或完成闭环升级，同时抛出新冰山一角。
- 契诃夫之枪：特写物品、特殊能力、反复提及的符号，后续剧情必须闭环回收。

2. 人物与系统设计
- 拒绝贴标签：严禁直接定义人物"冷酷/温柔/腹黑"，必须通过具体的反常动作、口是心非与微表情体现。
- 立体人设：角色有瑕疵、会纠结、动机合理，严禁全知全能的完美工具人或无脑纯反派。
- 系统人格化：从以下模板中选定一种与主角性格形成反差或互补，全书保持一致：
  (毒舌嘲讽 / 话痨跑题 / 高冷惜字 / 欠揍皮痒 / 萌新学习 / 赌徒骰子 / 社恐胆怯 / 戏精剧场 / 打工人怨念 / 老板PUA / 恋爱脑撮合 / 碎碎念老妈 / 中二病晚期 / 佛系随缘 / 杠精抬杠)

3. 爽点与伏笔运作
- 爽点打造：以"信息差碾压、战力装逼、打脸反派、逆袭翻盘"为主，配合"旁人震惊、不敢置信、事后细思极恐"进行侧面衬托。
- 伏笔晒宝：三章内让读者意识到异常细节，水下线索在爽点事件中自然展露，三章内兑现闭环。
""".strip()

WRITING_STYLE_RULES = """
正文写作规范（正文写作时执行）

1. 篇幅与段落呼吸感
- 字数：单章 2300-2500 字（中文汉字含标点），以写"事"与"行动"为主。
- 自然段落：主力段落 10-50 字（占 80%），70% 的段落 ≤70 字（手机端三行以内）；叙述长段（＞50字）仅用于信息密集/群像场（≤20%）；对白段一句一段是常态。
- 段内句号硬限：每自然段 ≤2 个句号（以 1 个为主，=最多两句完整话），含 ≥3 个句号的段落必须为 0；≤15 字短段只能含 1 个句号；禁止一段堆 3-6 个小短句（漫画分镜式碎段是最浓AI味来源之一）。
- 句长：叙述句平均 12-18 字（短线为主），七成以上为逗号长句（逗号串 1-2 个动作单元收在一个句号）；单句 ＞55 字必须拆开；连续 3 句以上 ＜12 字的句号短句必须合并成逗号长句。
- 呼吸感排布：自然长段与短段交错；一段只承载一个动作或信息变化；相邻三句同镜头同POV必须合并；不允许把一个自然动作链切成 4+ 句独立段。
- 多以对话推动剧情，对话密度 20%–60%，根据不同剧情自然、有感情地分布。

2. 开头与结尾铁律
- 开头：直接从【时间、动作、对话、地点、事件】五选一暴力切入，严禁环境描写、心理活动或世界观介绍开场。
- 结尾：必须停留在动态动作、人物视线、脚步移动或悬念对白上；严禁抒情、升华、复盘、总结或陈述式点破答案。

3. 镜头感与对白技法
- 叙事手感：旁白是"场边嘀咕"，不是"讲台朗读"。情绪靠具体动作带出，严禁直呼情绪词（愤怒、悲伤、屈辱）。
- 摄像机词限额：「看见/看着/听见/注意到/盯着/望向」全章合计 ≤3 次，超额改用动作、物象、感官细节直接呈现。
- 对白精简：对话短而完整，富有潜台词。连续三句对白中必须穿插动作/微镜头。
- 群像区分：三人以上对话，角色之间必须在用词粗细、长短句、口癖、语气词上有明显差异。
- 去提示语：严禁使用"XX地说"，通过前后的独立动作短句自然带出说话语气。

4. 叙事口语化与网感注入
- 口语化替换参考：
  - 因此 → 所以 | 颇为 → 特别/贼/巨 | 随即 → 马上/下一秒 | 显而易见 → 说白了 | 或许 → 估计/大概 | 之/其/乃/遂 → 的/他/就是/于是
- 高频生活口语库：
  `合着、整半天、好家伙、说白了、不是……你、得了吧、拉倒吧、至于么、啥玩意、啥情况、搁这、没跑了、差不离、差不多得了、说实话、说真的、怎么说呢、你别说、还真别说`
- 精选网梗与网感词（适度点缀，不破坏剧情氛围）：
  `离谱、离大谱、属实是、破防了、蚌埠住了、好家伙、这合理吗、差不多得了、寄、润了、杀疯了、卧槽、这波血赚、绷不住了、绝了、麻了`

5. 决策链铁律（防"行为机器"，严禁违反）
- 主角遇到重大事件（穿越/系统到账/生死关/重大背叛）必须有 ≥2 句内心碎片反应（错愕/不信/快速消化），禁止无缝进入战斗或执行模式。
- 主角每个重大决策前必须有"决策瞬间"——赌什么/为什么敢/怕什么，半句也行。原主挨打七个月不敢还手、穿越者第一天就动手，中间必须补"为什么现在敢"。
- 主角主动决策每章 ≥2 次（做选择/出手/布局/拒绝），禁止全程被外力推着走。

6. 爽点公式与节奏呼吸（高潮/冲突段必备）
- 爆发段必须写足连环反应：主角体感 1 句 + 至少 2 个视角的围观反差（反派从嚣张到狼狈的对照/强者动容/围观哗然）。
- 打脸之后必须留 ≥2 拍余震（旁人议论/反派挣扎/主角补一刀）再收章，禁止爆发完直接跳收尾台词。
- 禁止纯事件播报（闷响→砖石崩开→金光铺展→敌人跌落 A/B/C/D 报完就完）——每个事件节点必须附着人的反应。
- 每 2 个冲突波之间留半段闲笔（环境一笔/小动作/一句废话），全程紧绷无喘息 = 节奏灾难；喘息段同时承担信息增量或伏笔，不是纯废笔。

7. 对白金句限流（防编剧腔）
- 金句式对白（双关/宣言/威胁句式）每章 ≤3 处，必须留给真正的关键节点。
- 每 5 段对白至少 1 句口语碎片（废话/打岔/语气词/答非所问），真人对话 30% 是废话，100% 功能性对白 = 剧本。
- 配角台词禁止全是精准数字+威胁句式的"台词本腔"。

8. 章法要求
- 每章开头尽快进入场景或事件（时间/动作/对话/地点/事件五选一暴力切入，禁环境/心理/世界观开场，见第2条）。
- 每章中段用对白和行动推进信息。
- 每章结尾落在新风险、新线索或新决策上。
- 章尾三禁：禁环境描写收尾、禁主角心理独白收尾、禁无意义配角台词收尾。钩子必须紧扣本章冲突与爽点方向。
- 不用空泛总结收尾。不用连续大段设定说明。
- 【人物出场·禁公式化排比对比（命中直接改，不许保留原句式）】
  禁"相比X，Y像A；比起B，Y又B；比起C，Y连C都没有"这种≥2次"相比/比起"连用的三段式排比（AI最爱人物出场模板）；一律改成→
  • 只给 1 个物象对比锚：「陆沉舟鞋底干净得能照出灯。陈烨的鞋缝里卡着七号街的油泥。」
  • 再给 1 个动作对比锚：「陆沉舟走路鞋跟响。陈烨走路鞋底啪嗒，踩过水洼带起半尺泥。」
  • 最多 2 条对比（不要≥3 条排比），不用"相比/比起"字样，直接摆动作/物品，读者自然能比出来。

9. 视角与信息控制铁律（严禁违反）
- **禁止上帝视角**：不得出现"他不知道，此时远在千里之外……""与此同时，另一边……"等全知叙事。单章锁定1-2个视角人物，只写视角人物能感知、能推断、能目睹的内容。
- **禁止剧透式叙述**：不得写"此时的他还不知道，这个决定将改变他的命运""他未曾料到，眼前这人日后会成为他最大的对手"等预告式点评。未来要发生的事，让未来的章节去写。
- **禁止上帝点评**：不得写"命运就是这样奇妙""冥冥之中自有天意""历史的车轮滚滚向前"等作者跳出来升华的句子。
- **禁止全知全能角色**：主角和配角都应有信息盲区。反派不能什么算计都提前料中，主角不能每件事都猜对。该被骗就被骗，该失算就失算，该走弯路就走弯路。
- **信息差即张力**：读者知道的 ≠ 主角知道的 ≠ 配角知道的。善用信息差制造"替角色着急"的代入感。可以让读者比主角多知道一点（戏剧反讽），也可以让主角比读者多藏一张底牌（悬念）。
- **伏笔隐性埋设**：伏笔必须伪装成日常细节、闲笔、环境描写、人物口头禅，不得标记"这是伏笔""此处埋线""后面会回收"。埋的时候像随手一写，回收的时候读者才恍然大悟。
- **禁止伏笔一股脑算盘写出**：不得在同一章集中铺设大量伏笔并明示其用途。伏笔分散在不同章节，自然穿插，每章最多1-2处暗线，多了就是剧透清单。
- **视角切换有界**：如需切换视角，用分场（空行+地点/人物标头）明确分隔，单场内不再跳视角。禁止同一段落内频繁切换不同人物心理。
""".strip()


# ============================================================================
# 【阶段隔离·System Prompt 构建函数】按阶段注入：构思（master包）/ 正文（style包）/
# 去AI审稿（review包 + 独立去AI手册），通用核心三阶段共用
# ============================================================================

def build_conception_rules(skill_pack_ids=None, mode='agent', extra_master_note: str = '', book=None) -> str:
    """构思阶段专属规则：屏蔽文风/去AI/一致性，只保留通用核心+构思格式+master技能包。
    - book 可选：传入时自动从 book.master_skill_ids 取已持久化ID，与请求 skill_pack_ids 取并集。"""
    parts = [GENERAL_CORE_RULES, CONCEPTION_EXTRA_RULES]
    master_note = ''
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        book_ids = _resolve_skill_ids_by_category(book, 'master') if book else []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_ids)))  # 有序并集
        master_note = _get_skill_prompts_by_category(merged_ids, 'master', mode=mode) or ''
    except Exception:
        master_note = ''
    if extra_master_note:
        master_note = (master_note + '\n\n' + extra_master_note).strip()
    if master_note:
        parts.append("【构思类·技能包专属方法论】\n" + master_note)
    return "\n\n".join(parts).strip()


def build_writing_rules(book=None, skill_pack_ids=None, mode='agent',
                        extra_style_pack: str = '', extra_style_note: str = '') -> str:
    """正文阶段专属规则：通用核心 + 行文规范 + 去AI味消杀 + 文风类(style)技能包。
    - book 必传：用于取已持久化文风包，并按 genre_target 匹配题材。
    - 文风技能包【只注入一次】：merged_ids = 请求传参 ids ∪ book.style_skill_ids（有序并集去重），
      不再重复走 _get_enabled_style_pack 那条路径，避免同一包出现 2 遍。
    - DEAI_ONLY_RULES（去AI味与行文消杀）在正文生成阶段也注入：DEAI_RULES 首句即"写作正文时主动规避"，
      让模型从源头规避禁词/病句/副词，而非等审校阶段再做减法（审校仍会再次注入做兜底清洗）。"""
    parts = [GENERAL_CORE_RULES, WRITING_STYLE_RULES, DEAI_ONLY_RULES]
    style_note = ''
    book_genre = getattr(book, 'genre', None) if book is not None else None
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        # 合并 ids：请求 skill_pack_ids ∪ book.style_skill_ids（持久化的），有序并集去重
        if book is not None:
            book_style_ids = _resolve_skill_ids_by_category(book, 'style')
        else:
            book_style_ids = []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_style_ids)))
        style_note = _get_skill_prompts_by_category(merged_ids, 'style', mode=mode, book_genre=book_genre) or ''
    except Exception:
        style_note = ''
    if extra_style_pack or extra_style_note:
        extra = '\n\n'.join(s for s in [extra_style_pack, extra_style_note] if s).strip()
        style_note = (style_note + ('\n\n' + extra if style_note else extra)).strip()
    if style_note:
        parts.append("【文风类·技能包专属规则】\n" + style_note)
    return "\n\n".join(parts).strip()


def build_chat_chapter_rules(book=None, skill_pack_ids=None, mode='agent',
                             extra_style_pack: str = '', extra_style_note: str = '') -> str:
    """智驾聊天里用户明确要求"写正文/写第X章/接着写"时的补充注入。

    与 build_writing_rules 同源：注入正文行文规范 WRITING_STYLE_RULES + 去AI味消杀 DEAI_ONLY_RULES + 文风类技能包，
    但【跳过 GENERAL_CORE_RULES】——因为 chat_smart 的 system prompt 已内置 GENERAL_CORE_RULES，
    这里再注入会造成同一段文字在 prompt 里出现两遍（用户感知"啰嗦重复"）。
    用于补齐 chat_smart 目前"只讨论不写正文"的缺口，保证在智驾里写正文同样命中行文/去AI硬卡。
    """
    parts = [WRITING_STYLE_RULES, DEAI_ONLY_RULES]
    style_note = ''
    book_genre = getattr(book, 'genre', None) if book is not None else None
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        if book is not None:
            book_style_ids = _resolve_skill_ids_by_category(book, 'style')
        else:
            book_style_ids = []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_style_ids)))
        style_note = _get_skill_prompts_by_category(merged_ids, 'style', mode=mode, book_genre=book_genre) or ''
    except Exception:
        style_note = ''
    if extra_style_pack or extra_style_note:
        extra = '\n\n'.join(s for s in [extra_style_pack, extra_style_note] if s).strip()
        style_note = (style_note + ('\n\n' + extra if style_note else extra)).strip()
    if style_note:
        parts.append("【文风类·技能包专属规则】\n" + style_note)
    return "\n\n".join(parts).strip()


# 智驾聊天里判定用户是否明确要求"写正文/写第X章/接着写"——命中则需注入正文行文规范
_WRITE_CHAPTER_INTENT_RE = None


def _is_write_chapter_intent(text: str) -> bool:
    """检测智驾聊天输入是否为明确的写正文意图（否则只在通用提示词下讨论，不注入行文规范）。

    覆盖：写正文 / 写第N章 / 接着写 / 续写 / 继续写 / 写一章 / 把第N章写出来 / 开始写第N章 等。
    命中返回 True；纯讨论（改设定/问走向/构思）不命中。
    """
    if not text:
        return False
    import re as _re
    t = text.strip()
    # 明确包含"写"+ 章节/正文对象，或"接着写/续写/继续写/写正文"动词
    patterns = [
        r'写\s*第\s*[0-9一二三四五六七八九十百千万]+',   # 写第N章 / 写第3卷
        r'写.{0,4}(本章|正文|这一章|下一章|一章|新章节)',   # 写正文 / 写这一章
        r'(接着|继续|往下)?(写|续写|码).{0,3}(正文|章节|本章|一章)',  # 接着写正文 / 续写本章
        r'把\s*第\s*[0-9一二三四五六七八九十百千万]+\s*章.{0,4}(写|码|续)',
        r'(开始|快?)写正文',
    ]
    return any(_re.search(p, t) for p in patterns)


def build_review_rules(skill_pack_ids=None, mode='agent',
                       prompt_keys_filter=None, extra_review_note: str = '', book=None) -> str:
    """去AI/审稿阶段专属规则：通用核心 + 独立去AI手册 + 审查类(review)技能包。
    ⚠️ 去AI阶段【不再注入 WRITING_STYLE_RULES】：
       DEAI_ONLY_RULES 内部已经完整覆盖解释腔/对白/工整句式/禁词表/执行流程等行文规范，
       再注入 WRITING_STYLE_RULES 会让同内容出现 2 遍（用户感知"啰里啰嗦重复好几遍"）。"""
    parts = [GENERAL_CORE_RULES, DEAI_ONLY_RULES]
    review_note = ''
    try:
        from app import _get_skill_prompts_by_category, _resolve_skill_ids_by_category
        book_ids = _resolve_skill_ids_by_category(book, 'review') if book else []
        merged_ids = list(dict.fromkeys(list(skill_pack_ids or []) + list(book_ids)))
        if prompt_keys_filter:
            review_note = _get_skill_prompts_by_category(
                merged_ids, 'review', prompt_keys_filter, mode=mode
            ) or ''
        else:
            review_note = _get_skill_prompts_by_category(
                merged_ids, 'review', mode=mode
            ) or ''
    except Exception:
        review_note = ''
    if extra_review_note:
        review_note = (review_note + '\n\n' + extra_review_note).strip()
    if review_note:
        parts.append("【审查类·技能包专属规则】\n" + review_note)
    return "\n\n".join(parts).strip()


# ============================================================================
# 用户说话意图识别 → 自动同步核心创作参数（卷数/每卷章数/题材/风格）到 Book + BookBible
# 解决：用户在智驾里说“改成25卷”时，不能只当一句对话，要真正落地到 DB，
#       否则后续 prompt 中的【核心创作参数铁律】读的还是旧值，等于用户白说。
# ============================================================================

# 卷数：正则命中即提取数字（允许：总卷数/全书/一共/计划/改成/设为/按/做 等词 + N + 卷）
# 例："改成25卷"、"全书按30卷来写"、"总卷数15卷"、"一共8卷"、"做60卷"、"写10卷"、"搞18卷"、"按20卷规划"
_RE_TV = re.compile(
    r'(?:总卷数|全书|全本|整本书|一共|总共|合计|总计|计划|准备|打算|想|要|需要|改成|改为|设置为|设为|调整为|调成|调为|按|做成|写成|做|写|搞|设计成|规划成|规划|控制在|就|那就|那就按|就按|至少|最多|左右|大概|约|差不多)'
    r'\s*(\d{1,4})\s*卷',
)
# 反向宽松：数字+卷 在句中且含"卷"的意图词（兜底）；配合负向词表避免误判叙事
_RE_TV_LOOSE = re.compile(r'(?:^|[,，。；！？\s])(\d{1,4})\s*卷', re.IGNORECASE)

# 每卷章数：例 "每卷60章"、"改成每卷 80 章"、"每卷按40章规划"
_RE_CPV = re.compile(
    r'(?:每卷|一卷|单卷|一册)\s*(?:改成|改为|设置为|设为|调整为|按|做成|写成|控制在|计划|一共|大约|约)?\s*(\d{1,4})\s*章'
)
_RE_CPV_LOOSE = re.compile(r'每卷.*?(\d{1,4})\s*章', re.IGNORECASE)

# 防止被"第12卷"、"卷三"、"10卷公交"这种非总卷数/章数意图的纯叙事描述命中：负向关键词
_NEG_TV_TOKENS = re.compile(r'(第\s*\d+\s*卷|卷[一二三四五六七八九十百千零\d]+|回|话|公交|公卷|问卷|试卷|答卷|卷宗|卷(起|发|入|尺|子|心菜|心菜|曲|烟|叶|铺盖|包|云|))', re.IGNORECASE)


def _auto_sync_params_from_user_message(book, bb, message: str):
    """从用户最新一条聊天消息中识别“卷数/章数调整”意图，真正同步到 DB。

    Returns:
      list[str]：本次实际同步成功的 human-readable 说明（供前端 SSE meta 回显）。
                 空列表表示没有识别到需要同步的参数。
    """
    import app as app_module
    from app import db, _sync_book_meta_to_bible  # 复用现有同步机制，保证口径一致
    if not book or not message:
        return []
    msg = (message or '').strip()
    if not msg:
        return []
    synced_notes = []

    # -------- 1. 总卷数：提取并落 Book.total_volumes --------
    def extract_tv(text):
        m = _RE_TV.search(text)
        if m:
            return int(m.group(1))
        # 负向过滤：含“第N卷/卷X”字样时不再走宽松匹配，避免"我现在在写第25卷"被误判为改总卷数
        if _NEG_TV_TOKENS.search(text):
            return None
        m2 = _RE_TV_LOOSE.search(text)
        if m2:
            return int(m2.group(1))
        return None

    tv_new = extract_tv(msg)
    if tv_new is not None and 1 <= tv_new <= 2000:  # 合理性区间，防止"1卷"这种错别字
        try:
            cur_tv = int(getattr(book, 'total_volumes', 0) or 0)
        except Exception:
            cur_tv = 0
        if cur_tv != tv_new:
            # 先写 Book（权威口径）
            book.total_volumes = tv_new
            # 再调用同步机制把 Book → BookBible（内部已处理 Case A/B，不会把用户 Bible 手工修改覆盖）
            if bb is None:
                from app import BookBible as _BB
                bb = _BB.query.filter_by(book_id=book.id).first()
                if bb is None:
                    bb = _BB(book_id=book.id)
                    db.session.add(bb)
            _sync_book_meta_to_bible(book, bb)
            db.session.commit()
            # 卷数变了 → 失效该作品的 system prompt 缓存（general_chat/圆桌共用的铁律块要立刻读到新卷数）
            try:
                from app import PromptContextCache
                PromptContextCache.get().invalidate_book(book.id)
            except Exception:
                pass
            synced_notes.append(f'【已同步】检测到你要求“{tv_new}卷”，已自动将作品总卷数从 {cur_tv or "未设定"} 更新为 {tv_new} 卷（后续五幕总纲/分卷规划/正文写作都会严格按此卷数执行）')

    # -------- 2. 每卷章数：提取并落 Book.chapters_per_volume（若有该字段） --------
    def extract_cpv(text):
        m = _RE_CPV.search(text)
        if m:
            return int(m.group(1))
        m2 = _RE_CPV_LOOSE.search(text)
        if m2:
            return int(m2.group(1))
        return None
    cpv_new = extract_cpv(msg)
    if cpv_new is not None and 5 <= cpv_new <= 500:
        # chapters_per_volume 字段在不同版本项目里可能叫 chapters_per_volume / chapters_every_volume / 不存在
        field_candidates = ('chapters_per_volume', 'chapters_every_volume', 'chapters_per_book')
        cur_cpv = 0
        for f in field_candidates:
            if hasattr(book, f):
                try:
                    cur_cpv = int(getattr(book, f, 0) or 0)
                except Exception:
                    cur_cpv = 0
                break
        if cur_cpv != cpv_new:
            for f in field_candidates:
                if hasattr(book, f):
                    setattr(book, f, cpv_new)
                    break
            if bb is None:
                from app import BookBible as _BB
                bb = _BB.query.filter_by(book_id=book.id).first()
                if bb is None:
                    bb = _BB(book_id=book.id)
                    db.session.add(bb)
            _sync_book_meta_to_bible(book, bb)
            db.session.commit()
            try:
                from app import PromptContextCache
                PromptContextCache.get().invalidate_book(book.id)
            except Exception:
                pass
            synced_notes.append(f'【已同步】检测到你要求“每卷 {cpv_new} 章”，已自动将每卷章数从 {cur_cpv or "默认"} 更新为 {cpv_new} 章（后续总章数上限会按 总卷数 × {cpv_new} 章计算）')

    return synced_notes


# ============================================================================
# Action Card 协议
# ============================================================================

# 卡片类型 → 目标维度字段 + 落地方式（append 覆盖/追加, character 走独立表, chapter 走章节表）
CARD_REGISTRY = {
    'SAVE_WORLDSETTING': {'field': 'worldbuilding', 'mode': 'append', 'label': '世界观'},
    'SAVE_SETTING':      {'field': 'concept',        'mode': 'append', 'label': '设定'},
    'SAVE_CHARACTER':    {'field': 'character_profiles', 'mode': 'character', 'label': '人物'},
    'SAVE_FORESHADOW':   {'field': 'foreshadowing', 'mode': 'append', 'label': '伏笔'},
    'SAVE_OUTLINE_NODE': {'field': 'plot_design', 'mode': 'append', 'label': '大纲'},
    'SAVE_PLOT':         {'field': 'timeline', 'mode': 'timeline', 'label': '剧情线'},
    'SAVE_LOCATION':     {'field': 'locations', 'mode': 'append', 'label': '地点'},
    'SAVE_RULE':         {'field': 'key_rules', 'mode': 'append', 'label': '核心规则'},
    'APPLY_STYLE':       {'field': 'style_guide', 'mode': 'append', 'label': '文风'},
    'SAVE_CONCEPT':      {'field': 'concept', 'mode': 'append', 'label': '核心构思'},
    'SAVE_CHAPTER':      {'field': 'chapter', 'mode': 'chapter', 'label': '章节正文'},
}

# 通用聊天关键词命中的维度key → 落地卡片类型（供圆桌总结"是否采纳到各维度"用）
_DIM_KEY_CARD = {
    'concept': 'SAVE_CONCEPT', 'key_rules': 'SAVE_RULE', 'worldbuilding': 'SAVE_WORLDSETTING',
    'plot_design': 'SAVE_OUTLINE_NODE', 'timeline': 'SAVE_PLOT', 'character_profiles': 'SAVE_CHARACTER',
    'foreshadowing': 'SAVE_FORESHADOW', 'locations': 'SAVE_LOCATION', 'style_guide': 'APPLY_STYLE',
}

# ============================================================================
# 圆桌会议"创作模式"：作者要求"按讨论结果创作各维度/某维度"时，
# 复用与"各维度生成"一致的格式（维度标签 + 卡片类型 + 生成长度上限），
# 产出标准可采纳卡片（格式与用户直接在对应维度生成时完全一致）。
# ============================================================================
_RT_CREATE_DIMS = {
    # key → (维维度标签, 卡片类型)
    'concept':            ('核心构思', 'SAVE_CONCEPT'),
    'key_rules':          ('核心规则', 'SAVE_RULE'),
    'worldbuilding':      ('世界观', 'SAVE_WORLDSETTING'),
    'character_profiles': ('人物档案', 'SAVE_CHARACTER'),
    'plot_design':        ('剧情大纲', 'SAVE_OUTLINE_NODE'),
    'timeline':           ('时间线', 'SAVE_PLOT'),
    'foreshadowing':      ('伏笔', 'SAVE_FORESHADOW'),
    'locations':          ('地点', 'SAVE_LOCATION'),
    'style_guide':        ('文风指南', 'APPLY_STYLE'),
}
# 默认"全部维度"顺序（与用户在各维度面板看到的顺序一致）
# 人物在大纲之后：大纲先定各卷功能位/人物方向，人物按大纲锚定设计（同 DIMENSION_DEPENDENCIES）
_RT_CREATE_ALL = ['concept', 'key_rules', 'worldbuilding', 'plot_design',
                  'character_profiles', 'timeline', 'foreshadowing', 'locations', 'style_guide']

# 各维度对应的 BB 字段（读已有内容 / 采纳时写入）
_RT_CREATE_FIELD = {
    'concept': 'concept', 'key_rules': 'key_rules', 'worldbuilding': 'worldbuilding',
    'character_profiles': 'character_profiles', 'plot_design': 'plot_design',
    'timeline': 'timeline', 'foreshadowing': 'foreshadowing',
    'locations': 'locations', 'style_guide': 'style_guide',
}


def _rt_parse_create_dims(text: str):
    """解析作者"按讨论结果创作…"指令，返回目标维度 key 列表。

    - 命中"全部/各/所有/多维/来一遍" → 返回全部维度（_RT_CREATE_ALL）
    - 命中具体维度关键词 → 只返回匹配的那些
    - 完全没命中创作意图 → 返回 None（由调用方走普通圆桌流程）
    """
    t = (text or '').strip()
    if not t:
        return None
    # 创作意图触发词（命中任一才进入创作模式）
    trigger = re.search(r'(?:按|根据|基于|照|把|将)?\s*讨论\s*(?:的)?\s*(?:结果|共识|结论|收获|成果|建议)?\s*(?:创作|生成|产出|起草|落地|写|做|搞)|\b创作\s*(?:各|全部|所有|相应|对应)?\s*(?:维度|设定)|按讨论结果', t)
    if not trigger:
        return None
    # 全部维度
    if re.search(r'(?:创作|生成|产出|起草|落地|写|做|搞|整)\s*(?:全部|所有|全部维度|各维度|各|多维)|各维度|全部维度', t):
        return list(_RT_CREATE_ALL)
    # 具体维度关键词
    kw_map = [
        ('concept', r'(?:核心?构思|核心创意|点子|logline|一句话故事)'),
        ('key_rules', r'(?:核心规则|核心设定|力量体系|规则|设定)'),
        ('worldbuilding', r'(?:世界观|世界设定|世界背景)'),
        ('plot_design', r'(?:剧情大纲|全书大纲|大纲|五幕)'),
        ('character_profiles', r'(?:人物档案|人物设定|人物|角色)'),
        ('timeline', r'(?:剧情线|时间线|剧情|情节|卷剧情)'),
        ('foreshadowing', r'(?:伏笔|埋线)'),
        ('locations', r'(?:地点|地图|地图设定|场景)'),
        ('style_guide', r'(?:文风|文风指南|行文风格|写作风格)'),
    ]
    hit = []
    for k, pat in kw_map:
        if re.search(pat, t):
            if k not in hit:
                hit.append(k)
    return hit  # 可能为空列表（命中创作意图但没识别出具体维度 → 由调用方按"全部"兜底）


def _rt_general_dim_request(text: str):
    """通用聊天自然语：识别"明确要求生成某维度"的指令。

    与 _rt_parse_create_dims（圆桌"按讨论结果创作")不同，这里是通用聊天里作者直接用自然语要求
    生成某一维度长内容（"帮我生成世界观/主角/大纲/伏笔/文风…"）。命中返回目标维度 key 列表。
    - 明确要求"全部/所有维度" → 返回全部核心维度
    - 命中具体维度关键词 → 只返回匹配的
    - 非明确生成指令 → 返回 None（走普通聊天，不注入维度格式，避免打断闲聊）
    """
    t = (text or '').strip()
    if not t or len(t) < 3:
        return None
    # 必须有"生成/创作/写...设定/内容/维度"等明确动作词 + 一个维度目标，才是明确生成指令
    _act = re.search(r'(?:帮我|请|替我|为(?:我|这本书))?\s*(?:生成|创作|产出|起草|草拟|制定|编排|设计|写|建立|搭|规划|形成|整理)?\s*(?:一份|一套|一个)?\s*(?:完整的|详细的|全面的)?', t)
    if not _act:
        return None
    # 动作词必须确凿（常见 AI 违例词首字符）——否则如"帮我看看"不触发
    # 注：写 走 "写出来/写一下/写个" 等确凿搭配，避免"写得怎么样"这类评价句误触发
    if not re.search(r'(?:生成|创作|产出|起草|草拟|制定|编排|设计|建立|搭|规划|形成|整理|写(?:出来|一下|个|好|份|完))\s*(?:一份|一套|一个|完整的|详细的|全面的)?', t):
        return None
    # 明确"全部/所有维度"
    if re.search(r'(?:全部|所有|各|多|一系列|整\s*套)\s*(?:维度|设定|内容|方案)', t):
        return list(_RT_CREATE_ALL)
    # 具体维度关键词（从强到弱，取命中数最多/最高置信）
    kw_map = [
        ('concept', r'(?:核心?构思|创意|logline|卖点|一句话(?:故事|梗))'),
        ('key_rules', r'(?:核心规则|核心设定|力量体系|规则|设定)'),
        ('worldbuilding', r'(?:世界观|世界设定|世界背景|世界架构)'),
        ('plot_design', r'(?:全书大纲|剧情大纲|大纲|分卷大纲)'),
        ('character_profiles', r'(?:人物档案|人物设定|人物|角色|主角|配角|反派)'),
        ('timeline', r'(?:剧情线|时间线|情节|卷剧情|剧情)'),
        ('foreshadowing', r'(?:伏笔|埋线|伏?笔)'),
        ('locations', r'(?:地点|地图|区域|场景设定|地理)'),
        ('style_guide', r'(?:文风|行文风格|写作风格|叙事风格|语言风格|文风指南)'),
    ]
    # 若动作目标是"维度"但用户直接说"生成世界观/写男主"这类（维度词紧跟动作词且无额外话题）→ 命中
    hit = []
    for k, pat in kw_map:
        if re.search(pat, t):
            if k not in hit:
                hit.append(k)
    if not hit:
        return None
    return hit  # 明确生成指令且命中具体维度 → 返回


def _rt_create_dimension_system(dim_key: str, book, iron: str, consensus: str, existing: str) -> str:
    """构造圆桌"创作模式"某维度的 system prompt。

    复用/对齐与"各维度生成"一致的格式要求（concept/key_rules/worldbuilding/
    character_profiles/plot_design 走详实分节铁律；timeline 走 JSON 卷数组；
    style_guide/foreshadowing/locations 走结构化纯文本），
    以圆桌讨论共识为核心素材，保证产出内容和直接在该维度生成时的格式一致。
    """
    parts = []
    parts.append('你是资深网文创作副驾。现在要根据一场"圆桌专家讨论"得出的共识，为一部小说创作一个维度设定。')
    parts.append('必须以圆桌共识为唯一取材依据，内容要具体可落地、可直接采纳进对应维度，禁止"待设定/后续再定"等空话。')
    if iron:
        parts.append(iron)
    parts.append(f'\n【圆桌讨论共识（唯一取材来源）】\n{consensus[:14000]}')
    if existing:
        parts.append(f'\n【已有该维度内容（可在其基础上按共识完善，不要简单重复）】\n{existing[:2000]}')
    _book_part = f'为小说《{book.title}》' if book else '为这部小说'
    parts.append(f'\n请{_book_part}产出下列维度的完整内容。')

    d = dim_key
    if d == 'concept':
        parts.append('''\n【核心构思铁律·禁止两句话】必须输出 10 节：
①一句话故事核Logline ②主题曲线(起点→反诘→抉择→终局) ③核心冲突三角(主角×对手×世界规则)
④目标分层(短/中/长/终极+失败代价) ⑤核心爽感机制(3-5种主爽点+触发→爆发→余波+卷1/3/5/终局排布)
⑥金手指/外挂(类型+核心能力+分级+硬约束代价+贴合执念+终极风险)
⑦主角魅力公式(记忆符号+三重反差+具体创伤+核心执念) ⑧对手/反派魅力(前/中/终局三级反派)
⑨世界观3-5个独特卖点钩子 ⑩全书情感底色+读者定位+文风力向。
总字数不少于 1200 字，每节必须写具体可落地内容。''')
    elif d == 'key_rules':
        parts.append('''\n【设定铁律·禁止只写境界表】输出最前面必须先给【备选书名】3 个（风格差异化，各自点出核心卖点）与【小说简介】（150 字以内，讲清主角处境、核心冲突与最大爽点）。随后必须输出 11 节：
①力量总体系(2主1辅+克制) ②等级阶梯表(命名+战力差+突破门槛+社会地位+寿元) ③至少2主1偏的提升路径
④功法/技能树(5类分级+代表性技能+配搭+获取) ⑤资源与货币体系(通用货币+等价物+10项价格表+产地)
⑥装备/法宝/载具 ⑦至少2-3种副职业 ⑧硬约束+反噬代价 ⑨种族/职业/阵营总表+矛盾 ⑩至少8条世界硬规则禁忌
⑪文明水平总览。总字数不少于 1500 字，必须有具体数字和例子。''')
    elif d == 'worldbuilding':
        parts.append('''\n【世界观铁律·禁止只写四大域】必须输出 15 节（最少 2000 字）：
①世界总览 ②创世元史三段+至少3纪元大事 ③至少6大地理分块 ④气候天象体系 ⑤至少8大主要势力
⑥完整阶级金字塔 ⑦政治律法 ⑧经济贸易 ⑨至少5个智慧种族 ⑩至少2正统+1邪教宗教信仰
⑪语言文字度量衡历法 ⑫风俗礼仪服饰饮食建筑 ⑬军事体系 ⑭交通通讯 ⑮至少5个世界未解之谜/禁忌之地。''')
    elif d == 'character_profiles':
        parts.append('''\n【人物铁律·禁止只给姓名+一句话身份】至少写出 主角 + 1女主/重要女配 + 2核心配角 + 1前期反派 + 1中期反派：
每个角色按 15 项写满（姓名/性别年龄/外貌特征含记忆符号/身份地位/性格三原色/核心价值观/人生三目标/
深层动机/具体核心创伤/恐惧软肋/能力体系/战斗风格/背景故事/关键关系网/角色弧线）。
每个角色至少 300 字，合计不少于 1800 字；纯中文按字段分行输出。''')
    elif d == 'plot_design':
        parts.append('''\n【大纲铁律·禁止只写几句话五幕】必须写满：
五幕(立身/立足/立势/立威/立命)对应到连续卷号 + 每卷6项指标
(①本卷爽点4小1大 ②人物方向 ③地点动线 ④修炼/事业/财富/关系/势力五项进展 ⑤伏笔主题方向 ⑥卷尾得到/失去/新任务)；
结尾附【跨卷尾钩子承接总览】。总字数不少于 1500 字。''')
    elif d == 'timeline':
        parts.append('''\n【剧情线铁律】输出按卷组织的 JSON 数组（可直接写入剧情线维度）：
每条卷对象含：卷名 volume、卷概要 summary(150-250字)、卷主线 main_plot(100-160字)、核心冲突 core_conflict、
卷尾钩子 ending_hook、主要剧情事件 main_events[]（含 title/summary/bury/payoff）、情节节点 nodes[]（含 title/summary/chapters/type/爽点）。
卷数必须与核心参数铁律一致，卷与卷之间尾钩自然承接。只输出 JSON，不要 markdown 代码块。''')
    elif d == 'foreshadowing':
        parts.append('''\n【伏笔铁律】按伏笔分组输出，每组：①伏笔名 ②埋设章/幕（含具体场景） ③引出的事件
④预期回收章/幕 ⑤回收效果 ⑥状态（已埋/待收/已收）。覆盖主线、支线、人物、世界观几类，列表清晰可采纳。''')
    elif d == 'locations':
        parts.append('''\n【地点铁律】按地图/场景分组输出，每组：①地点名 ②隶属（地域/势力） ③地理描述
④功能用途 ⑤关键剧情事件标注 ⑥出入限制/危险。至少覆盖主线涉及的核心场景，结构清晰。''')
    elif d == 'style_guide':
        parts.append('''\n【文风铁律】输出结构化文风指南，包含：①叙事视角与口吻 ②节奏控制（爽点/铺垫/爆点的章内排布）
③描写偏好（环境/动作/心理/对话比例） ④修辞与语言调性 ⑤禁用习惯（AI味/书面腔/水字数句式）
⑥对标参考风格。做成可直接指导写作的可执行规范。''')
    else:
        parts.append('\n【输出要求】结构化分节输出该维度完整设定，具体可采纳。')
    parts.append('\n【排版】使用纯中文层级输出，去掉 * 与 # 装饰符号；绝对禁止输出 JSON 数组/对象、英文键名和 [ ] { } " : , 等英文符号（剧情线维度按专属铁律输出 JSON 除外）。')
    return '\n'.join(parts)

# 安全提取卷号：接受 dict 或 str，返回 int 或 0
# LLM 输出的卡片标记格式：
#   [[CARD:SAVE_CHARACTER|标题|内容]]
#   [[CARD:SAVE_FORESHADOW|标题|内容]]
# 支持内容中含 | 时用最后一个 | 分隔（标题不含 |）
# (?!\]) 防吞尾：卡片内容常以 JSON 数组结尾（…}] + 闭合 ]]）→ 文本尾部是 "…}]]]"，
# 旧正则非贪婪 \]\] 会把「JSON 末位 ] + 卡片闭合第1个 ]」当成闭合 → content 丢失最后一个 ]，
# JSON.parse 全线失败（节点设计进度/采纳落地/前端渲染全部受害）。加负向前瞻后：
# "…}]]]" 中第2、3个 ] 匹配 \]\] 但后面还有第4个 ] → 回溯，用第3、4个 ] 真正闭合。
CARD_RE = re.compile(r'\[\[CARD:([A-Z_]+)\|([^\|]*)\|([\s\S]*?)\]\](?!\])')


def _repair_card_json_tail(content: str) -> str:
    """存量坏卡片修复：旧 CARD_RE 吞掉 JSON 结尾最后一个 ] 导致解析失败。
    以 [ 或 { 开头、json.loads 失败、括号不平衡 → 补齐缺失尾部闭合符（能变合法才改）。"""
    if not content or content[0] not in '[{':
        return content
    try:
        json.loads(content)
        return content  # 本来就合法
    except Exception:
        pass
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in content:
        if esc:
            esc = False
            continue
        if ch == '\\':
            if in_str:
                esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in '[{':
            stack.append(ch)
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
    if stack:
        cand = content + ''.join(']' if c == '[' else '}' for c in reversed(stack))
        try:
            json.loads(cand)
            return cand
        except Exception:
            return content
    return content


def parse_cards(text: str) -> list[dict]:
    """从 LLM 文本中解析出所有 Action Card。"""
    cards = []
    for m in CARD_RE.finditer(text):
        ctype, title, content = m.group(1), m.group(2).strip(), m.group(3).strip()
        content = _repair_card_json_tail(content)
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


def _build_toc_block(book_id, max_items: int = 200) -> str:
    """构建章节目录（按卷分组）块，供智驾聊天 system prompt 注入。
    让 AI 知道完整目录，用户提"第N章/某卷"时可精准定位，不再索要资料。
    输出：
      第1卷《XXX》
        第1章 XXX（2350字）
        ...
    """
    try:
        from app import Chapter, parse_chapter_number
        from sqlalchemy import or_
        rows = Chapter.query.filter_by(book_id=book_id).all()
        if not rows:
            return ''
        # 按 order_index 排序（卷+章统一 order）
        rows_sorted = sorted(rows, key=lambda c: (c.parent_id or '', c.order_index or 0))
        vol_map = {v.id: v for v in rows_sorted if v.is_volume}
        # 分组：卷 -> 该卷章节
        from collections import OrderedDict
        groups: 'OrderedDict[str, list]' = OrderedDict()
        orphans = []
        for c in rows_sorted:
            if c.is_volume:
                groups.setdefault(c.id, [])
                continue
            pid = c.parent_id
            if pid and pid in vol_map:
                groups.setdefault(pid, []).append(c)
            else:
                orphans.append(c)
        lines = []
        total = 0
        for vid, chs in groups.items():
            vol = vol_map.get(vid)
            if vol:
                lines.append(f'第{vol.order_index or 1}卷《{vol.title or "未命名卷"}》')
            for ch in chs:
                if total >= max_items:
                    break
                wc = ch.word_count or 0
                lines.append(f'  {ch.title or ""}（{wc}字）')
                total += 1
            if total >= max_items:
                break
        if orphans and total < max_items:
            if groups:
                lines.append('【未分卷章节】')
            for ch in orphans[:max_items - total]:
                wc = ch.word_count or 0
                lines.append(f'  {ch.title or ""}（{wc}字）')
        if not lines:
            return ''
        return '\n'.join(lines)
    except Exception:
        return ''


def _core_params_iron_block(bb, book):
    """构建“核心创作参数·铁律·不可违反”块（所有创作链路统一注入）。

    比 _build_core_params_block 更强：
    - 标为“铁律·不可违反”放在 system prompt 最上方，避免被下文淹没
    - 追加“越界拦截警示”：大纲/剧情/卷规划必须严格等于总卷数；正文章号不得超过总章数上限
    - 所有 8 条创作链路都要调用（chat_smart / smart_general / smart_generate /
      smart_dimension_edit / smart_batch / smart_deai / chat_smart_action / _action_chapter）

    返回空串表示获取参数失败（静默降级，不阻断主流程）。
    """
    try:
        from app import _get_total_volumes, _get_genre_label, _get_novel_styles_text, _get_chapters_per_volume
        tv = _get_total_volumes(bb, book)
        genre_label = _get_genre_label(book, bb)
        styles_text = _get_novel_styles_text(bb, book)
        cpv = _get_chapters_per_volume(bb, book)
        max_chapters = (tv or 0) * cpv  # 总章数上限 = 总卷数 × 每卷章数（tv 未设置时为 0，下文铁律跳过）
        bt = getattr(book, 'book_type', 'novel') or 'novel'
        parts = ['【核心创作参数·铁律·不可违反】']
        if tv and tv >= 1:
            parts.append(f'1. 总卷数：{tv} 卷（全书所有分卷/五幕总纲/剧情大纲严格按此卷数规划，不得多不得少）')
        else:
            # 用户仍未显式设置总卷数 → 不给任何默认暗示（十/五/十二都不出现），
            # 等用户真正设定后，后续调用会按上面那条真正绑定。
            parts.append('1. 总卷数：由作者定义（创作时请按作者已经给定的分卷规模规划；若作者尚未指定分卷，请先把分卷规模显式写在方案里给作者确认）')
        parts.append(f'2. 题材：{genre_label}（人物设定、世界观、剧情走向、爽点类型须契合该题材的读者期待）')
        if bt == 'novel':
            if tv and tv >= 1:
                parts.append(f'3. 每卷章数：约 {cpv} 章/卷（全书总章数上限约 {max_chapters} 章，总字数约 {tv*12} 万字）')
            else:
                parts.append(f'3. 每卷章数：约 {cpv} 章/卷（总章数上限按作者后续指定的总卷数 × {cpv} 章来计算；在作者未指定前，请先把分卷规模明确写在方案里）')
        else:
            if tv and tv >= 1:
                parts.append(f'3. 短篇结构：{tv} 个单元/幕')
            else:
                parts.append('3. 短篇结构：单元/幕数由作者定义，先在方案里明确给出再推进。')
        if styles_text:
            parts.append(f'4. 风格流派：{styles_text}（人物塑造、节奏、爽点设计、叙事手法须契合所选流派，这是硬约束）')
        # 越界拦截：只有 tv 明确设置后才给具体 N 卷/N 章的硬上限，避免把 0/默认 当成越界依据
        if tv and tv >= 1:
            parts.append('')
            parts.append('【越界拦截警示·生成前自检】')
            parts.append(f'1. 生成五幕总纲/分卷大纲/剧情线时，卷数必须严格等于 {tv} 卷，多一卷或少一卷都不合格，必须重写。')
            parts.append(f'2. 生成正文章节时，章节号上限为第 {max_chapters} 章（= {tv} 卷 × {cpv} 章/卷），禁止产出超过此上限的章节号。')
            parts.append('3. 讨论规划或生成卡片前先对照上述铁律，若你的方案会突破卷数/章数上限，请立刻自我修正到范围内再输出。')
        else:
            parts.append('')
            parts.append('【分卷规则·用户定义优先】')
            parts.append('1. 在作者未显式给出总卷数前，禁止在方案/卡片/正文里擅自默认"十卷/五卷/八卷/十二卷/十余卷/5-8卷"等固定数字，必须先把"全书建议按 N 卷规划"写清楚让作者确认，或直接沿用作者方案里的数字。')
            parts.append('2. 生成正文章节时，章号连续、不重复、不跳号即可；等作者指定总卷数后再按上限收紧。')
        return '\n'.join(parts)
    except Exception:
        return ''


def build_chat_system_prompt(book, bb, recent_chapters: list = None, next_chapter_num: int = None, toc_block: str = None,
                            rank_scan: dict | None = None) -> str:
    """构建维度感知的聊天 system_prompt。

    注入当前书的全部 bible 维度 + 章节目录 + Action Card 使用说明 + 创作进度。
    可选 rank_scan：榜单风向扫榜情报，追加为"本轮市场风向执行要求"。
    维度内容完整注入，不截断（避免信息缺失导致错乱）。
    recent_chapters: 最近章节列表（dict: title/word_count/order_index），由 chat_smart 注入
    next_chapter_num: 下一章应使用的章节号（与写作/修改/去AI统一口径）
    toc_block: 按卷分组的章节目录（可选，_build_toc_block 生成）
    """
    parts = [
        '你是一位资深网文创作副驾，正在和作者协作创作一部小说。你的职责：',
        '1. 像同行一样讨论创作问题（人物、剧情、世界观、文风）',
        '2. 当讨论中形成明确结论时，主动产出“落地卡片”让作者一键采纳',
        '3. 感知创作进度，主动引导下一步该做什么',
        '',
        f'【当前作品】《{book.title or "未命名"}》',
    ]

    # =====================================================================
    # 【核心创作参数·铁律·不可违反】（用户创建小说时的总卷数/题材/风格，注入到最上方，避免被下文淹没）
    # =====================================================================
    core_iron = _core_params_iron_block(bb, book)
    if core_iron:
        parts.append('\n' + core_iron)

    # =====================================================================
    # 【三阶段通用铁律】（智驾聊天=跨阶段讨论场景：只注入 GENERAL_CORE_RULES 通用铁律，
    # 不注入 WRITING_STYLE_RULES 正文行文规范，也不注入 DEAI_ONLY_RULES 去AI手册
    # ——正文写作/去AI有独立接口精准注入专属规则，这里不一股脑全加载。）
    # =====================================================================
    parts.append('\n' + GENERAL_CORE_RULES)

    # 注入最近章节（让 AI 知道作者正在写哪一章，便于讨论"接下来怎么写"）
    if recent_chapters:
        parts.append('\n【最近章节】')
        for ch in recent_chapters[-5:]:
            title = ch.get('title') or f'第{ch.get("order_index", "?")}章'
            wc = ch.get('word_count', 0)
            parts.append(f'- {title}（{wc}字）')
        parts.append('作者可能在写最新章节的后续，讨论时可结合上文衔接。')

    # 注入下一章应使用的章节号（与写作/修改/去AI统一口径，避免产出重复章号的卡片）
    if next_chapter_num is not None:
        parts.append(
            f'\n【章节号铁律】当前正文章节维度下最新章节号已到第{next_chapter_num - 1}章。'
            f'产出 SAVE_CHAPTER 卡片时，新章节标题必须用“第{next_chapter_num}章”开头'
            f'（如：第{next_chapter_num}章 章节名），不得重复使用已有的章节号。'
            f'修改已有章节时，保持原章节号不变。'
        )

    # 注入 bible 维度（完整注入，不截断，避免信息缺失导致错乱）
    if bb:
        dims = [
            ('核心构思', 'concept', 2600),
            ('世界观', 'worldbuilding', 2600),
            ('核心规则', 'key_rules', 1600),
            ('人物档案', 'character_profiles', 3200),
            ('大纲', 'plot_design', 2600),
            ('剧情时间线', 'timeline', 1800),
            ('伏笔', 'foreshadowing', 1200),
            ('地点', 'locations', 1000),
            ('文风指南', 'style_guide', 1000),
        ]
        filled = []
        empty = []
        for label, field, cap in dims:
            val = (getattr(bb, field, '') or '').strip()
            if val:
                # 【纯文字铁律】JSON 存储维度注入前一律转自然语言，避免 AI 模仿 JSON 格式输出
                if field == 'character_profiles' and val.startswith('['):
                    val = _character_profiles_to_text(val)
                elif field == 'timeline' and (val.startswith('[') or val.startswith('{')):
                    val = _json_to_plain_text(val)
                # 系统 prompt 9 维度注入字符硬上限（避免单维度 10K+ 导致 3 轮对话就撞 LLM ctx 上限）
                if len(val) > cap:
                    # 先在语义分界处（双换行/句号）截断，不破坏结构
                    cut = val[:cap]
                    last_break = max(cut.rfind('\n\n'), cut.rfind('。'), cut.rfind('\n'))
                    val = (cut[:last_break] if last_break > cap // 2 else cut) + f'\n…（已截前{cap}字，完整落地维度请查创作界面，或引用前言精准索取）'
                parts.append(f'\n【已设定·{label}】\n{val}')
                filled.append(label)
            else:
                empty.append(label)

        if not filled:
            parts.append('\n【创作状态】这是一本新书，所有维度都还空白，需要从头讨论设定。')
        else:
            parts.append(f'\n【创作进度】已完成维度：{"、".join(filled)}')
            if empty:
                parts.append(f'待补充维度：{"、".join(empty)}（可引导作者讨论这些）')

        # 防遗忘检查报告回注：让智驾聊天也能感知已诊断出的一致性违规/待回收伏笔/叙事债务
        try:
            from app import _collect_anti_forget_alerts
            _af = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
            if _af:
                parts.append('\n【防遗忘检查诊断】（最近检查发现的问题，讨论与产出卡片时必须主动规避，不可重犯）')
                parts.append(_af)
        except Exception:
            pass
    else:
        parts.append('\n【创作状态】这是一本新书，还没有任何设定，需要从头讨论。')

    # 章节目录（按卷分组）：让AI根据章号/标题精准定位，不再向用户索要资料
    if toc_block:
        parts.append('\n【章节目录】（用户提"第N章""某卷""某章标题"时，你必须基于此定位章节，直接输出修改方案/续写，绝不要让用户"把资料发给你"）')
        parts.append(toc_block)

    # 维度入口：让AI知道改某维度内容直接用对应卡片/或基于已有内容输出
    parts.append("""
【维度与章节定位铁律·永远遵守】
1. 绝对禁止回复"请把大纲/设定/人物/章节资料发给我""你需要先提供XXX资料"这类话。
   上面【已设定·XXX】和【章节目录】中已有完整数据；若某个维度确实为空，直接说"这个维度还是空白，咱们从零开始…"，然后给出方案建议即可。
2. 用户提到以下关键词 → 直接基于对应的【已设定·XXX】内容进行讨论/修改：
   - "构思/故事核/核心冲突" → 核心构思
   - "世界观/世界设定/地理" → 世界观
   - "设定/规则/体系/能力/修炼" → 核心规则
   - "人物/角色/主角/配角/XXX（人名）" → 人物档案
   - "大纲/剧情线/主线/支线/五幕" → 大纲
   - "时间线/时间/年代" → 剧情时间线
   - "伏笔/铺垫/伏笔回收" → 伏笔
   - "地点/场景/XXX（地名）" → 地点
   - "文风/叙事风格" → 文风指南
3. 用户提到"第N章/某章标题/某卷"：
   - 若要求"修改/调整/润色/改改"：基于该章位置上下文讨论具体改动建议，产出 SAVE_CHAPTER 卡片或详细修改方案；
   - 若要求"接着写/续写"：严格按【章节号铁律】写新章节；
   - 不要让用户再把正文发给你（系统后续会自动把该章原文注入上下文）。
4. 当确实缺少具体细节（如改第5章但要改某句特定措辞），只问具体要改什么，不要笼统要资料。
""".strip())

    # Action Card 使用说明
    parts.append(_CARD_INSTRUCTIONS)

    # 平台级纯文字排版铁律（禁止 * 和 #）
    parts.append(PLAIN_TEXT_LAYOUT_RULES)

    # 用户采纳的"系统学习与优化建议"补丁 → 作为铁律段追加到 system prompt 末尾，
    # 保证智驾对话、后续维度生成都能按用户定制规则约束自身输出
    if bb:
        try:
            from meta_optimizer import build_active_patch_text
            _pp = build_active_patch_text(bb)
            if _pp:
                parts.append('\n' + _pp)
        except Exception:
            pass

    # P0 榜单风向：如果本轮有扫榜情报（match_categories/hot_elements/openings/landmines/title_formulas/top_books），
    # 作为最终段注入。让智驾的通用聊天与副驾全部能自动对齐市场。
    _rank_ctx = _format_rank_context(rank_scan)
    if _rank_ctx:
        parts.append('\n' + _rank_ctx)

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
- SAVE_CHAPTER 仅在用户明确要求"写一章""接着写正文"时产出，内容必须是完整的章节正文，且严格遵循【字数绝对铁律】：2400字±100（即 2300-2500 字区间，字数口径为中文字符+中文标点，不含标题）。低于 2300 字须扩写场景细节补足；超过 2500 字须精简删减。这是不可违反的硬约束。
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
    priority_order = ['concept', 'key_rules', 'worldbuilding', 'plot_design', 'character_profiles', 'timeline', 'foreshadowing', 'style_guide']
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
# 章节号统一口径：写作 / 修改 / 去AI 三者共用
# ============================================================================

def _get_latest_chapter_info(book_id):
    """获取最新章节的统一口径信息（写作/修改/去AI共用）。

    返回: { latest_num, next_num, latest_chapter }
      - latest_num: 最新章节号（优先 parse_chapter_number(title)，回退 order_index+1）
      - next_num:   下一章应使用的章节号 = latest_num + 1（无章节时为 1）
      - latest_chapter: 最新章节对象（按章节号/ order_index 排序的最后一章）

    解决问题：order_index 与标题章节号不一致时，三者用不同口径导致
    "已有第1章，写作又生成第1章"。
    """
    from app import Chapter, parse_chapter_number
    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    if not chs:
        return {'latest_num': 0, 'next_num': 1, 'latest_chapter': None}
    # 优先按标题章节号排序；无章节号者回退 order_index
    def sort_key(c):
        n = parse_chapter_number(c.title or '')
        return (0, n) if n is not None else (1, c.order_index)
    chs_sorted = sorted(chs, key=sort_key)
    latest = chs_sorted[-1]
    latest_num = parse_chapter_number(latest.title or '')
    if latest_num is None:
        latest_num = latest.order_index + 1
    return {'latest_num': latest_num, 'next_num': latest_num + 1, 'latest_chapter': latest}


# ============================================================================
# 章节标题剥离 + 字数统计统一口径
# 解决：AI 输出"标题+空行+正文"整体入 card.content → 正文混标题、字数口径不一致。
# 统一：content 只存纯正文；字数用 _count_cn_chars（去空白含标点）
# ============================================================================

def _strip_chapter_title(content, fallback_title=''):
    """从 AI 输出中剥离章节标题行，返回 (title, body)。

    AI 被要求输出格式：第一行标题，第二行空行，第三行起纯正文。
    本函数防御性处理：
      - 首行像章节标题（以"第N章/Chapter N"开头 + 短行≤30 + 非句末标点）
        且第二行为空行（匹配 AI 被要求的"标题+空行+正文"格式）→ 剥离标题及后续空行
      - 自动去除标题行首的 markdown # 标记（如 "# 第四章 左臂开狱" → "第四章 左臂开狱"）
      - 否则视为纯正文，title 回退到 fallback_title，body 为原文

    用于：正文写作/润色/去AI 产出 SAVE_CHAPTER 卡片前剥离标题，保证 card.content 为纯正文。
    "第二行必须为空行"的约束可避免误剥叙事句（如正文首行"第三章的秘密终于揭晓"）。
    """
    if not content:
        return fallback_title, ''
    text = content.strip()
    lines = text.split('\n')
    first_raw = lines[0].strip() if lines else ''
    # 去除行首 markdown # 标记（#、##、###...）
    first = re.sub(r'^#+\s*', '', first_raw).strip()
    from app import parse_chapter_number
    # 章节标题判定：以"第N章/Chapter N"开头 + 短行(≤30) + 非句末标点
    starts_with_chapter = bool(
        re.match(r'^第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*[章节回卷部篇话集幕折更段讲课夜日年季场]', first)
        or re.match(r'^(?:chapter|ch|episode|ep)\.?\s*\d+', first, re.IGNORECASE)
    )
    is_title_line = (
        starts_with_chapter
        and bool(parse_chapter_number(first))
        and len(first) <= 30
        and not first.endswith(('。', '！', '？', '；', '.', '!', '?', '"', '"', "'", "'", '…'))
    )
    # 必须满足"标题+空行+正文"格式：第二行（lines[1]）为空行，才剥离
    has_blank_after = len(lines) >= 2 and not lines[1].strip()
    if is_title_line and has_blank_after:
        title = first or fallback_title
        # 跳过标题后的所有空行
        body_start = 1
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
        body = '\n'.join(lines[body_start:]).strip()
        return title, body
    # 首行不像标题或不满足格式：原文整体作为正文，标题用兜底值
    return fallback_title, text


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
    # P1-1 会话级切模型：请求体 ai_config_id > 会话 meta_json.ai_config_id > 全局激活
    req_ai_config_id = (data.get('ai_config_id') or '').strip() or None
    # P1-3 内置角色 persona：default/polish/toxic_critic/architect/worldbuilder/marketeer/interviewer
    req_role_id = (data.get('role_id') or '').strip() or None
    # 持久化在会话级 meta_json.role_id（下次沿用，除非用户切）
    scope = data.get('scope', 'general')
    # P0 榜单风向：前端在聊智驾前先扫榜，把扫榜报告 rank_scan 塞进来；我们注入到 system_prompt 和所有落地卡片 subtitle
    _rank_scan = data.get('rank_scan') if isinstance(data.get('rank_scan'), dict) else None

    if not book_id or not message:
        return jsonify({'error': '缺少 book_id 或 message'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ===== 【卷数/章数意图·落地前置】先于 LLM 调用执行 =====
    # 用户在智驾里说"改成25卷/每卷60章"时，必须真正写入 DB，
    # 否则后续 build_chat_system_prompt → _core_params_iron_block 读到的还是旧值，用户等于白说。
    params_sync_notes = _auto_sync_params_from_user_message(book, bb, message)
    # 同步后重新取一次 bb（可能刚刚新增了一条，避免后续 None 判空出问题）
    if bb is None:
        bb = BookBible.query.filter_by(book_id=book_id).first()

    # P4：加载最近 5 章标题（让 AI 懂作者正在写哪一章）+ 统一口径下一章号
    recent_chapters = []
    next_chapter_num = None
    try:
        # 统一口径：从章节表提取最新章节号（与写作/修改/去AI共用）
        ch_info = _get_latest_chapter_info(book_id)
        next_chapter_num = ch_info['next_num']
        # 最近 5 章：按章节号排序（与统一口径一致）
        from app import parse_chapter_number
        recent = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        def _recent_key(c):
            n = parse_chapter_number(c.title or '')
            return (0, n) if n is not None else (1, c.order_index)
        recent = sorted(recent, key=_recent_key)[-5:]
        recent_chapters = [{'title': c.title, 'word_count': c.word_count or 0,
                            'order_index': c.order_index} for c in recent]
    except Exception:
        pass

    # 获取或创建会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）
    session = _get_or_create_session_for_book(session_id, book_id, scope=scope, title=message[:30])
    session_id = session.id

    # 构建 system_prompt + 上下文（注入章节目录）
    toc_block = _build_toc_block(book_id)
    system_prompt = build_chat_system_prompt(book, bb, recent_chapters, next_chapter_num, toc_block, rank_scan=_rank_scan)

    # ===== 写正文意图·注入正文行文规范 =====
    # chat_smart（维度感知聊天）默认只注入 GENERAL_CORE_RULES，不注入 WRITING_STYLE_RULES（设计见
    # build_chat_system_prompt。但当用户明确要"写第X章/写正文/接着写"时，若仍不注入行文规范，
    # 产出的 SAVE_CHAPTER 正文卡会在没有任何行文/去AI硬卡约束下自由发挥 → AI特征居高不下。
    # 这里命中写作意图则同源注入 build_chat_chapter_rules（WRITING_STYLE_RULES+文风技能包，跳过
    # 已重复的 GENERAL_CORE_RULES），与正文 Tab(chat_smart_action) / ai_continue 保持一致。
    if _is_write_chapter_intent(message):
        try:
            _chat_chapter_rules = build_chat_chapter_rules(book, mode='agent')
            if _chat_chapter_rules:
                system_prompt = system_prompt + '\n\n' + _chat_chapter_rules
        except Exception:
            pass  # 注入失败不阻断主流程，退回通用聊天提示词

    # 自动上下文注入：根据用户输入识别提及的章节/维度，将原文/维度内容作为"引用前言"前置到用户消息中
    # 同时生成命中信息，在 SSE 首个 meta 事件中回传给前端做"已定位"提示
    auto_ctx_block, auto_ctx_info = _build_auto_context_block(message, book_id, bb)
    enriched_user_message = message
    if auto_ctx_block:
        enriched_user_message = (
            '（以下为系统根据作者输入自动从当前书库载入的引用资料，用于辅助回答；作者原话为最后的"【作者原话】"段。\n'
            '回答时直接基于这些资料讨论/修改，严禁再让作者"把资料发给我"；若引用中的某维度为空，直接说明为空并给出建议。）\n\n'
            f'{auto_ctx_block}\n\n'
            '——————————————————\n'
            '【作者原话】\n'
            f'{message}'
        )

    history = load_session_messages(session)
    messages = build_context_messages(system_prompt, history, enriched_user_message)

    # 获取 LLM 配置 + gateway
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
    # chat_smart（维度感知聊天链路）：归一化URL（防智谱GLM 404/HTTP 500）
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
    # chat_smart 统一过 URL 归一化（智谱/v4必须走 _normalize_llm_base_url，否则会撞 /v4/v1）
    import os as _os_cs1
    from llm_gateway import _normalize_llm_base_url as _nl1
    import app as _mod1
    try:
        _act1 = _mod1.AIConfig.get_active()
        _act1_id = getattr(_act1, 'id', None) if _act1 else None
    except Exception:
        _act1_id = None
    _is_act1 = (_act1_id and chosen_cfg_id and _act1_id == chosen_cfg_id) or (not chosen_cfg_id)
    if _is_act1:
        _b1, _k1, _m1 = get_llm_config(_mod1)
        if cfg.model and cfg.model != _m1:
            _m1 = cfg.model
    else:
        _b1 = _nl1(cfg.base_url or _os_cs1.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1'), cfg.model)
        _k1 = cfg.api_key or _os_cs1.environ.get('USER_LLM_API_KEY', '')
        _m1 = cfg.model or _os_cs1.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    gw = LLMGateway(_b1, _k1, _m1)

    def generate():
        # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧，占住连接防 Render 30s idle timeout ===
        yield ': ping-heartbeat-keepalive\n\n'
        full_text = []
        try:
            # SSE 首帧 ①：核心创作参数同步结果（若用户这条消息触发了卷数/章数调整，先告诉前端已落地）
            if params_sync_notes:
                yield f'data: {json.dumps({"type": "meta", "kind": "params_sync", "info": {"notes": params_sync_notes}}, ensure_ascii=False)}\n\n'
            # SSE 首帧 ②：返回命中的章节/维度（用于前端回显"已定位并注入…"提示）
            if auto_ctx_info['chapters'] or auto_ctx_info['dims']:
                yield f'data: {json.dumps({"type": "meta", "kind": "auto_context", "info": auto_ctx_info}, ensure_ascii=False)}\n\n'

            # 智驾通用对话 max_tokens 完全不设限（None → payload 不发送该字段）：
            # 部分网关把 max_tokens 当配额预留额度，发 131072 秒撞 TPM 限流掐流报 network error；
            # 不设则按模型默认输出上限执行，反而更稳（流式生成不受影响）。
            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=None):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT  # 裸注释心跳帧：前端自动忽略，不进正文
                    continue
                if _is_stream_retry(chunk):
                    yield f'data: {json.dumps({"type": "meta", "kind": "stream_retry", "info": chunk.info}, ensure_ascii=False)}\n\n'
                    continue
                full_text.append(chunk)
                yield f'data: {json.dumps({"type": "delta", "content": chunk}, ensure_ascii=False)}\n\n'

            # 解析卡片
            complete = ''.join(full_text)
            cards = parse_cards(complete)
            # 统一纯文本清理（卡片内容、卡片标题、回复正文）
            for c in cards:
                c['content'] = _clean_text_to_plain(c.get('content', ''))
                if c.get('title'):
                    c['title'] = _clean_text_to_plain(c['title'])
            for card in cards:
                _enrich_card_rank_meta(card, _rank_scan)
                yield f'data: {json.dumps({"type": "card", "card": card, "session_id": session_id}, ensure_ascii=False)}\n\n'

            # 持久化对话（剥离卡片标记后存历史，cards 单独存以便历史会话恢复）
            clean_text = _clean_text_to_plain(strip_cards(complete))
            # 卡片持久化时标记为 pending，前端历史会话加载后可继续采纳
            persisted_cards = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                                'content': c['content'], 'target': c['target'],
                                'status': 'pending',
                                'rankSourceLabel': c.get('rankSourceLabel') or '',
                                'subtitle': c.get('subtitle') or ''} for c in cards]
            history.append({'role': 'user', 'content': message})
            history.append({'role': 'assistant', 'content': clean_text,
                            'cards': persisted_cards})
            _safe_save_session_messages(session, history)

            yield f'data: {json.dumps({"type": "done", "session_id": session_id}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _persist_card_status(session_id, card_id, new_status, new_content=None):
    """更新会话 messages_json 中指定卡片的 status（采纳/编辑/忽略后持久化）。

    用于解决：重新打开聊天界面时卡片又恢复为 pending 的问题。
    同时同步全量存档表（覆盖 messages_json 瘦身后被裁掉的老消息里的卡片）。
    """
    if not session_id or not card_id:
        return
    try:
        from app import db, AISession
        session = AISession.query.get(session_id)
        if not session:
            return
        msgs = load_session_messages(session)
        changed = False
        for m in msgs:
            if m.get('role') != 'assistant' or not m.get('cards'):
                continue
            for c in m['cards']:
                if c.get('id') == card_id:
                    c['status'] = new_status
                    if new_content is not None:
                        c['content'] = new_content
                    changed = True
        if changed:
            _safe_save_session_messages(session, msgs)
    except Exception:
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass
    # 存档表同步（不管 messages_json 里是否命中都执行：老卡片可能在瘦身版里已被裁掉）
    _archive_update_card_status(session_id, card_id, new_status, new_content)


@chat_collab_bp.route('/api/ai/chat/smart/apply-card', methods=['POST'])
def apply_card():
    """采纳 Action Card，落地到对应维度。

    body 新增 mode 字段（四按钮协议）：
      - mode='overwrite'（采纳/编辑后覆盖）→ 覆盖该维度原内容（人物卡=清空后写入；
        剧情卡=以卡片卷为准整体重建 timeline）
      - mode='append'（追加）→ 追加到该维度原内容后，不覆盖（人物卡=合并追加；
        剧情卡=按 volume_index upsert 增量合并）
      - 未传 mode 时向后兼容旧前端：status='edited' → 覆盖；默认 → 追加

    SAVE_CHAPTER 模式（mode 不生效，章节固定按章号/标题 upsert）：
      - 自动识别章节号（parse_chapter_number）
      - 同章节号（或同标题）的章节存在 → 覆盖内容，不再追加
      - 不存在 → 新建章节
      - 落地后调用 resort_chapters_by_title(rebin_volumes=True)
        按章节号自动排序，按 50 章/卷自动新建/归入卷
    落地成功后会持久化卡片 status（adopted=覆盖落地 / appended=追加落地），
    避免重开聊天又提示采纳。
    """
    from app import db, BookBible, Character, Chapter, parse_chapter_number, resort_chapters_by_title, AISession
    data = request.json or {}
    book_id = data.get('book_id')
    card = data.get('card', {})
    ctype = card.get('type', '')
    content = (card.get('content') or '').strip()
    # 【纯文字铁律·落地端防线】非剧情线卡片内容若为 JSON（历史污染/旧会话卡片重放），
    # 落地前转纯文本，杜绝 JSON 符号写入 Bible 维度字段
    content = _plain_json_fallback(ctype, content)
    title = card.get('title', '')
    card_id = card.get('id', '')
    session_id = data.get('session_id')
    # 四按钮协议：mode 显式指定覆盖/追加；未传时兼容旧前端（edited→覆盖，默认→追加）
    mode = data.get('mode')
    if mode not in ('overwrite', 'append'):
        mode = 'overwrite' if card.get('status') == 'edited' else 'append'
    is_edit_overwrite = (mode == 'overwrite')
    # 落地后要回写的卡片状态（覆盖落地=adopted，追加落地=appended）
    new_card_status = 'adopted' if is_edit_overwrite else 'appended'

    # ====== 空判断/ctype校验 拆成独立分支，报错更精确，避免一刀切"无效的卡片或内容为空"排查困难 ======
    if not ctype:
        return jsonify({'error': '无效的卡片：缺少卡片类型(type)字段。请检查前端传入的 action card 结构。'}), 400
    if ctype not in CARD_REGISTRY:
        _valid = ', '.join(sorted(CARD_REGISTRY.keys()))
        return jsonify({'error': f'无效的卡片类型"{ctype}"（不在系统CARD_REGISTRY白名单）。有效类型：{_valid}。'}), 400
    if not content:
        return jsonify({'error': '卡片内容为空(quick_fill未传递或解析失败)：命中气泡的方案内容未正确填充到card.content，后端无法落地。'}), 400

    spec = CARD_REGISTRY[ctype]

    # 平台级后处理：统一清理 Markdown 符号 * 和 #，保证落地内容为好看的纯文字排版
    # timeline 模式跳过清理（content 是 JSON 数组，清理会破坏 JSON 结构）
    if spec.get('mode') != 'timeline':
        content = _clean_text_to_plain(content)
    title = _clean_text_to_plain(title) if title else title

    result_extra = {}

    # 章节正文卡：落地到 Chapter 表（覆盖同章节号/同标题，自动分卷排序）
    if spec['mode'] == 'chapter':
        # ====== 落地二次校验：章号越界坚决不入库（最后一道门） ======
        from app import Book, _get_total_volumes, _get_chapters_per_volume
        _book = Book.query.get(book_id)
        _bb = BookBible.query.filter_by(book_id=book_id).first()
        _tv = _get_total_volumes(_bb, _book)
        _cpv = _get_chapters_per_volume(_bb, _book)
        _max_chapters = _tv * _cpv
        # 防御性剥离标题行：保证 chapter.content 为纯正文
        # 兜底场景：chat_smart 产出的 SAVE_CHAPTER 卡片或历史会话恢复的卡片可能仍含标题
        stripped_title, body_content = _strip_chapter_title(content, fallback_title=title)
        # 若剥离出更具体的标题（含章节名），优先用剥离结果
        if stripped_title and stripped_title != title:
            title = stripped_title
        # 字数统计与章节保存 API（app.py count_words）口径一致，避免落地后字数跳变
        from app import count_words
        wc = count_words(body_content)
        ch_num = parse_chapter_number(title)
        # 最后一道门：章号解析成功且越界 → 拒绝落地
        if ch_num is not None and ch_num > _max_chapters:
            return jsonify({'error': (f'【落地拦截·总章数越界】全书设定总卷数 {_tv} 卷 × 每卷 {_cpv} 章 = 总章数上限 {_max_chapters} 章，'
                                    f'“{title or f"第{ch_num}章"}”(第{ch_num}章) 已超出上限，未保存。若需要继续，请先到作品基本信息中调大总卷数。')}), 400
        existing_ch = None
        # 优先按章节号匹配（覆盖同章节号的章节）
        if ch_num is not None:
            candidates = Chapter.query.filter_by(
                book_id=book_id, is_volume=False
            ).all()
            for c in candidates:
                if parse_chapter_number(c.title or '') == ch_num:
                    existing_ch = c
                    break
        # 兜底：按完全相同标题匹配（覆盖同章节名的章节）
        if not existing_ch and title:
            existing_ch = Chapter.query.filter_by(
                book_id=book_id, is_volume=False, title=title
            ).first()

        if existing_ch:
            # 覆盖模式：同章节号/同标题存在，更新内容、标题、字数
            existing_ch.title = title or existing_ch.title
            existing_ch.content = body_content
            existing_ch.word_count = wc
            existing_ch.updated_at = datetime.now(timezone.utc)
            ch = existing_ch
            action = 'updated'
        else:
            # 新增模式：不存在同章节号/同标题，追加新章节
            max_idx = db.session.query(db.func.max(Chapter.order_index)) \
                .filter_by(book_id=book_id, is_volume=False).scalar() or 0
            ch = Chapter(
                book_id=book_id,
                title=title or f'第{max_idx + 1}章',
                content=body_content,
                order_index=max_idx + 1,
                word_count=wc,
                status='draft',
                is_volume=False,
                parent_id='',
            )
            db.session.add(ch)
            action = 'created'
        db.session.commit()

        # 自动按章节号排序 + 按 50 章/卷重新归入卷（新建卷及章节归属）
        try:
            resort_chapters_by_title(book_id, rebin_volumes=True)
            db.session.commit()
        except Exception:
            db.session.rollback()

        # M1a: 章节入库后自动抽取事件 → EventLog，并索引本章埋/收伏笔
        # P1-1 升级：关键章（卷首/高潮/卷末）自动启用 LLM 抽取；普通章走正则；并返回关键章信息给前端
        try:
            from event_log_manager import append_chapter_events_auto
            from llm_gateway import LLMGateway, get_llm_config as _get_cfg
            known_actors = [c.name for c in Character.query.filter_by(book_id=book_id).all() if c.name]
            known_locations = []
            try:
                locs = json.loads(_bb.locations or '[]') if _bb else []
                if isinstance(locs, list):
                    known_locations = [str(x) for x in locs if x]
            except Exception:
                pass
            total_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).count()
            # 取 LLM 配置（若用户未配置则 fallback 正则，不抛错）
            _gw = None
            _base = _api = _model = ''
            try:
                _base, _api, _model = _get_cfg()
                if _base and _api and _model:
                    _gw = LLMGateway(_base, _api, _model)
            except Exception:
                _base = _api = _model = ''
            ev_result = append_chapter_events_auto(
                _bb, ch, body_content,
                known_actors=known_actors,
                known_locations=known_locations,
                total_chapters=total_chapters,
                gw=_gw, base_url=_base, api_key=_api, model=_model,
            )
            result_extra['event_log'] = {
                'added': ev_result.get('events_added', 0),
                'ids': (ev_result.get('event_ids') or [])[:5],
                'use_llm': ev_result.get('use_llm_actual', False),
                'key_chapter': ev_result.get('key_chapter'),
            }
            # 索引本章埋/收伏笔（从 DAG 反查）
            if _bb and _bb.foreshadowing_graph:
                try:
                    from foreshadowing_manager import ForeshadowingGraph
                    graph = ForeshadowingGraph.from_dict(json.loads(_bb.foreshadowing_graph))
                    hooks = graph.get_nodes_for_chapter(ch.order_index)
                    ch.hooks_set_json = json.dumps({
                        'setup': [n.id for n in hooks.get('setup', [])],
                        'payoff': [n.id for n in hooks.get('payoff', [])],
                    }, ensure_ascii=False)
                except Exception:
                    pass
            db.session.commit()
        except Exception:
            db.session.rollback()

        result_extra = {
            'action': action,
            'chapter_id': ch.id,
            'chapter_title': ch.title,
            'word_count': wc,
            'order_index': ch.order_index,
        }
        # 持久化卡片状态（避免重开聊天又提示采纳/保存为新章节）
        _persist_card_status(session_id, card_id, new_card_status, body_content)
        # bible 可能不存在，但 progress 仍要返回
        bb = BookBible.query.filter_by(book_id=book_id).first()
        return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                        'applied_mode': mode, 'card_status': new_card_status,
                        'progress': build_progress_map(bb),
                        **result_extra})

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    try:
        if spec['mode'] == 'character':
            # 人物卡：解析"姓名|身份|性格|动机|背景|关系|能力|物品"或按行/【字段】解析
            # 前端人物及关系维度期望 character_profiles 为 JSON 数组，每元素含：
            # name/role/identity/personality/motivation/background/relationships/abilities/items
            # 一次生成多个人物时，content 含多个人物块（空行分隔），全部解析后追加
            char_list = _parse_character_card_multi(title, content)

            # 覆盖模式（采纳/编辑后覆盖）——清空原人物列表后写入新人物
            # 追加模式——保留原人物后追加新人物
            if is_edit_overwrite:
                # 覆盖：删除原 Character 表记录 + 重置 character_profiles
                try:
                    Character.query.filter_by(book_id=book_id).delete()
                except Exception:
                    pass
                existing_list = []
            else:
                # 追加：保留原数据
                existing_list = []
                try:
                    parsed = json.loads(bb.character_profiles or '[]')
                    if isinstance(parsed, list):
                        existing_list = parsed
                except Exception:
                    existing_list = []

            for char_data in char_list:
                _role_raw = char_data.get('role') or ('protagonist' if '主角' in (title or '') or '主角' in content else 'supporting')
                _role = _normalize_character_role(_role_raw, title, content)
                _desc = char_data.get('identity') or ''
                if _role_raw != _role:
                    # 长角色定位并入 description（Text 无长度限制），保证内容不丢
                    _desc = (_desc + '\n' + _role_raw).strip() if _desc else _role_raw
                db.session.add(Character(
                    book_id=book_id,
                    name=_clean_character_name(char_data.get('name')) or '未命名',
                    role=_role,
                    description=_desc,
                    personality=char_data.get('personality') or '',
                    background=char_data.get('background') or '',
                ))
                existing_list.append(char_data)
            bb.character_profiles = json.dumps(existing_list, ensure_ascii=False)
        elif spec['mode'] == 'timeline':
            # 剧情线卡：content 是 JSON 数组（按卷），需按 volume_index upsert 到已有 timeline
            # 修复 main_events/summary 丢失：合并时保留旧 nodes，缺字段时用 summary 回填 main_plot，
            # 确保 summary/main_events/nodes 三层结构在 DB 里完整保留，不被旧 UI 判定为"只采纳了概要"。
            raw = content.strip()
            # 剥离可能的 markdown 代码块包裹
            fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
            if fence:
                raw = fence.group(1).strip()
            try:
                new_vols = json.loads(raw)
                if not isinstance(new_vols, list):
                    new_vols = [new_vols] if isinstance(new_vols, dict) else []
            except (json.JSONDecodeError, ValueError, TypeError):
                # JSON 解析失败：退回纯文本模式存储
                new_vols = []

            if new_vols:
                # 解析已有 timeline
                existing_vols = []
                try:
                    parsed_tl = json.loads(bb.timeline or '[]')
                    if isinstance(parsed_tl, list):
                        existing_vols = parsed_tl
                except (json.JSONDecodeError, ValueError, TypeError):
                    existing_vols = []
                # 剧情线（timeline）永远是【按卷 upsert 增量合并】，不整条清空：
                # SAVE_PLOT 卡片通常只覆盖单个卷（节点设计一次产一卷），若采纳时
                # 把 existing_vols 清空重建，会导致其它卷的卷大纲/剧情节点被误删。
                # 同卷命中时 _merge_volume_nodes_incremental 按章号增量合并 + _merge_volume 保卷级字段；
                # 不同卷原样保留；is_edit_overwrite 仅影响卡片 status（adopted）与前端文案。

                # 按 volume_index upsert（覆盖同卷时走 _merge_volume 保字段）
                for nv in new_vols:
                    if not isinstance(nv, dict):
                        continue
                    # 修复节点 A+C：单章单节点 · 无重叠无跳章 · 50章/卷
                    if isinstance(nv.get('nodes'), list) and nv['nodes']:
                        nv = _repair_volume_nodes_safe(nv)
                    nv_idx = nv.get('volume_index') or _extract_volume_index_safe(nv)
                    matched = False
                    for i, ev in enumerate(existing_vols):
                        if not isinstance(ev, dict):
                            continue
                        ev_idx = ev.get('volume_index') or _extract_volume_index_safe(ev)
                        if str(ev_idx) == str(nv_idx):
                            # 增量合并 nodes（续会/单章修改 卡片只带部分章 → 新覆盖老+老保留新未命中）
                            nv, _ = _merge_volume_nodes_incremental(ev, nv)
                            # 增量合并后：再次跑 A+C 门禁修复（保证最终入库仍然单章单节点+无重叠+无跳章）
                            if isinstance(nv.get('nodes'), list) and nv['nodes']:
                                nv = _repair_volume_nodes_safe(nv)
                            existing_vols[i] = _merge_volume(ev, nv)
                            matched = True
                            break
                    if not matched:
                        existing_vols.append(_merge_volume({}, nv))

                # 按 volume_index 排序
                existing_vols.sort(key=lambda v: (
                    v.get('volume_index') or _extract_volume_index_safe(v) or 0
                    if isinstance(v, dict) else 0
                ))
                bb.timeline = json.dumps(existing_vols, ensure_ascii=False)
            else:
                # JSON 解析失败，退回纯文本存储
                entry = f'【{title}】\n{content}' if title else content
                if is_edit_overwrite:
                    bb.timeline = entry
                else:
                    existing_tl = (bb.timeline or '').strip()
                    bb.timeline = f'{existing_tl}\n\n{entry}'.strip() if existing_tl else entry
        else:
            field = spec['field']
            existing = (getattr(bb, field, '') or '').strip()
            entry = f'【{title}】\n{content}' if title else content
            if is_edit_overwrite:
                # 采纳(全覆盖)/编辑后落地：覆盖原内容（以本次卡片内容为准）
                setattr(bb, field, entry)
            else:
                # 追加：追加到原内容后，不覆盖
                setattr(bb, field, f'{existing}\n\n{entry}'.strip() if existing else entry)

            # M1a: 伏笔维度落地后自动解析为 DAG（结构化状态追踪）
            if field == 'foreshadowing':
                try:
                    from foreshadowing_manager import parse_text_to_dag
                    final_text = getattr(bb, field, '') or ''
                    graph = parse_text_to_dag(final_text)
                    if graph.nodes:
                        bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
                except Exception:
                    pass

        db.session.commit()

        # ====== 落地卡片保存到 Bible 后，立即同步实体注册表 ======
        # 用户反馈：实体注册表经常"识别不到智驾刚采纳落地/各维度已写入"的实体，
        # 原因是之前要等用户手动打开实体 Tab 调 list_entities 或触发 planner sync。
        # 现在每张落地卡片保存后就抽一次，实体注册表会立刻更新：
        #  含人物卡(character_profiles JSON) / 剧情 timeline / 世界观 / 势力 / 功法 / 地点……
        try:
            from app import Chapter
            from entity_registry import extract_and_save_registry
            recent_chs = (
                Chapter.query.filter_by(book_id=book_id, is_volume=False)
                .order_by(Chapter.order_index.desc())
                .limit(10)
                .all()
            ) or []
            extract_and_save_registry(bb, chapters_query=recent_chs)
            db.session.commit()
        except Exception:
            db.session.rollback()

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'落地失败：{str(e)}'}), 500

    # 持久化卡片状态（避免重开聊天又提示采纳）
    _persist_card_status(session_id, card_id, new_card_status, content)

    # ====== 新增：落卡成功后写入对应AISession的记忆（system消息），下次聊天LLM知道已落卡 ======
    # 根因：用户反馈"落卡了但继续聊天，LLM完全不知道我已经落过卡"，
    # 因为之前 apply-card 只改 bible + 卡片 status，没有把落卡这件事写到 session.messages_history，
    # 导致 chat_general/chat_smart 下一轮构造 messages 时完全没有「已落卡」这一条关键事实。
    if session_id:
        try:
            sess_obj = AISession.query.get(session_id)
            if sess_obj:
                spec_label = spec.get('label') or ctype
                # 摘要：只取标题+前150字，避免单条记忆塞太长
                preview = content[:150].replace('\n', ' ')
                if len(content) > 150:
                    preview += '…'
                mem_text = (
                    f'【系统上下文·落卡成功通知（无需对用户复述，仅作内部记忆参考）】\n'
                    f'时间：{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}\n'
                    f'落地维度：{spec_label}（字段={spec.get("field","")}，卡片类型={ctype}）\n'
                    f'卡片标题：{title or "(未命名卡片)"}\n'
                    f'落地模式：{"覆盖（采纳/编辑后全覆盖）" if is_edit_overwrite else "追加（保留原内容，追加写入）"}\n'
                    f'内容摘要：{preview}\n'
                    f'→ 以后聊到相关内容时，请以此为"智驾已写入"的事实依据，不要重复从零讨论已落过卡的相同话题。'
                )
                sess_hist = load_session_messages(sess_obj)
                sess_hist.append({'role': 'system', 'content': mem_text, '_source': 'card_applied_memory'})
                _safe_save_session_messages(sess_obj, sess_hist)
        except Exception as _me:
            # 记忆写入失败不影响主流程（卡片落地成功才是硬指标），记录但不抛错
            import traceback as _tb
            _tb.print_exc()

    return jsonify({'ok': True, 'field': spec['field'], 'label': spec['label'],
                    'applied_mode': mode, 'card_status': new_card_status,
                    'progress': build_progress_map(bb),
                    **result_extra})


def _split_multi_names_from_title(title):
    """从描述性档案标题中提取多人名列表。

    例："核心人物档案：顾晨、顾曦与赵阔" → ['顾晨','顾曦','赵阔']
    非描述性标题（纯人名）返回 []。
    """
    if not title:
        return []
    t = title.strip()
    m = re.search(r'(?:档案|人物|角色|群像|人设)(?:介绍|设定|合集)?\s*[:：]\s*(.+)$', t)
    if not m:
        m = re.search(r'[:：]\s*(.+)$', t)
    if not m:
        return []
    names_part = m.group(1).strip()
    parts = [p.strip() for p in re.split(r'[、，,/]|\s*和\s*|\s*与\s*', names_part) if p.strip()]
    # 过滤明显不是人名的超长片段
    parts = [p for p in parts if len(p) <= 20]
    return parts if len(parts) >= 2 else []


def _clean_character_name(name):
    """人物名清洗：描述性档案标题（如"核心人物档案：顾晨、顾曦与赵阔"）→ 提取名字区；
    超长 → 截断到 50（对齐生产库 characters.name varchar(50)，防止落库 StringDataRightTruncation）。"""
    name = (name or '').strip()
    if not name:
        return ''
    if re.search(r'(?:档案|人物|角色|群像|人设)', name):
        names = _split_multi_names_from_title(name)
        if names:
            return names[0]
        m = re.search(r'[:：]\s*(.+)', name)
        if m:
            name = m.group(1).strip()
    # 防御：name 中不允许出现换行（会拼出 "未命名\n姜雪" 脏名）
    name = name.split('\n', 1)[0].split('\r', 1)[0].strip()
    return name[:50]


def _normalize_character_role(role_raw, title='', content=''):
    """角色定位归一化：characters.role 是 varchar(50) 枚举语义（protagonist/antagonist/supporting）。

    LLM 卡片"角色：xxx"常给一句长描述（如"前期的核心资源提款机与脑补反差源…"），
    直接落库会触发 StringDataRightTruncation。超长时按上下文归类为简短枚举，
    完整描述由调用方并入 description（Text 无长度限制），保证内容不丢。
    """
    role = (role_raw or '').strip()
    if len(role) <= 50:
        return role
    ctx = role + ' ' + (title or '') + ' ' + (content or '')
    if '反派' in ctx or '敌' in ctx or '对手' in ctx or 'boss' in ctx.lower() or '最大威胁' in ctx:
        return 'antagonist'
    if '主角' in ctx:
        return 'protagonist'
    return 'supporting'


def _parse_character_card_multi(title, content):
    """解析可能含多个人物的内容，返回人物字典列表。

    拆分策略：
      1) | 分隔的纯文本 → 单个人物
      2) 按"姓名：xxx"行作为每个人物的起始边界拆块（最常见格式）
      3) 每个块再走 _parse_character_card 解析
      4) 无"姓名："引导时，整段作为单个人物
    """
    text = (content or '').strip()
    if not text:
        return []

    # 策略1：| 分隔的单行纯文本（无换行）→ 单个人物
    if '|' in text and '\n' not in text:
        return [_parse_character_card(title, text)]

    # 策略2：按"姓名："行拆块。匹配"姓名：xxx"作为新人物块的起始。
    lines = [l.rstrip() for l in text.split('\n')]
    blocks = []
    cur_block = []
    name_re = re.compile(r'^(?:【|\[)?(姓名|名字|名称)(?:】|\])?[:：]\s*\S')
    for line in lines:
        if name_re.match(line.strip()) and cur_block:
            blocks.append('\n'.join(cur_block).strip())
            cur_block = []
        cur_block.append(line)
    if cur_block:
        tail = '\n'.join(cur_block).strip()
        if tail:
            blocks.append(tail)

    # 策略2.5：标题含多人名（如"核心人物档案：顾晨、顾曦与赵阔"）→ 按【人名】/人名：/人名行切块
    names_from_title = _split_multi_names_from_title(title)
    if len(names_from_title) >= 2:
        pname_re = re.compile(r'^(?:【|\[)?(' + '|'.join(re.escape(n) for n in names_from_title) + r')(?:】|\])?[:：]?\s*')
        blocks_n = []
        cur_n = []
        for line in lines:
            if pname_re.match(line.strip()) and cur_n:
                blocks_n.append('\n'.join(cur_n).strip())
                cur_n = []
            cur_n.append(line)
        if cur_n:
            tail_n = '\n'.join(cur_n).strip()
            if tail_n:
                blocks_n.append(tail_n)
        if len(blocks_n) >= 2:
            result = []
            for blk in blocks_n:
                parsed = _parse_character_card('', blk)
                first_line = blk.split('\n', 1)[0].strip()
                mm = pname_re.match(first_line)
                if mm:
                    parsed['name'] = mm.group(1)
                if parsed.get('name') and parsed['name'] != '未命名':
                    result.append(parsed)
            if len(result) >= 2:
                return result

    # 若只拆出1块（或0块），回退为整段单人物
    if len(blocks) <= 1:
        return [_parse_character_card(title, text)]

    # 每块解析为人物字典
    result = []
    for i, blk in enumerate(blocks):
        # 第一块继承卡片标题；后续块用块内"姓名："作标题（在 _parse_character_card 内会取到）
        blk_title = title if i == 0 else ''
        parsed = _parse_character_card(blk_title, blk)
        # 兜底：若解析后姓名为空或"未命名"，取块首行
        if not parsed.get('name') or parsed['name'] == '未命名':
            first_line = blk.split('\n', 1)[0].strip()
            m = re.match(r'^(?:【|\[)?(姓名|名字|名称)(?:】|\])?[:：]\s*(.+)$', first_line)
            if m:
                parsed['name'] = m.group(2).strip()
        if parsed.get('name') and parsed['name'] != '未命名':
            result.append(parsed)
    return result if result else [_parse_character_card(title, text)]


def _parse_character_card(title, content):
    """解析人物卡片内容为结构化字段（与前端 CharacterData 对齐）。
    支持格式：
      1) 姓名|身份|性格|动机|背景|关系|能力|物品  （| 分隔）
      2) 姓名：xxx\\n身份：xxx\\n...  （字段名引导）
      3) 【姓名】xxx\\n【身份】xxx\\n...
      4) 【Silly Tavern 角色卡导入】标准套版：
         【角色名】 / 【性格/人格】 / 【外貌/背景描述】 / 【所处剧情场景/当前局面】 /
         【对白示例】 / 【角色开场第一句话/动作】 / 【创作者备注】
      5) 纯文本：首行/标题为姓名，其余为性格
    """
    fields = ['name', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items']
    # 字段关键词映射（支持中文标签 + Silly Tavern 导入专用标签）
    key_map = {
        'name':        ['姓名', '名字', '名称', '角色名'],
        'role':        ['角色', '定位', '角色定位'],
        'identity':    ['身份', '职业'],
        'personality': ['性格', '个性', '人格'],
        'motivation':  ['动机', '目的'],
        'background':  ['背景', '来历', '外貌', '外貌/背景描述', '背景描述', '外貌描述'],
        'relationships': ['关系', '人际关系', '所处剧情场景', '当前局面', '所处剧情场景/当前局面'],
        'abilities':   ['能力', '技能', '金手指', '对白示例', '说话风格', '对白风格'],
        'items':       ['物品', '装备', '持有物'],
        # 额外字段（用 result_extra 承载，不与标准 8 字段混用，避免覆盖）
        '__extra_first_line': ['角色开场第一句话', '角色开场第一句话/动作', '开场第一句'],
        '__extra_notes':      ['创作者备注', '备注', '作者备注'],
        '__extra_source_fn':  ['Silly Tavern 角色卡源文件名', '源文件名'],
    }
    result = {f: '' for f in fields}
    result['name'] = _clean_character_name(title)
    result['role'] = ''
    result_extra: dict = {}

    text = content.strip()
    # 策略1：| 分隔
    if '|' in text and '\n' not in text:
        parts = [p.strip() for p in text.split('|') if p.strip()]
        for i, f in enumerate(fields):
            if i < len(parts):
                result[f] = parts[i]
        if result['name'] and title:
            result['name'] = _clean_character_name(title)
        return result

    # 策略2/3：按行解析，匹配字段关键词
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    matched_any = False
    # 当前收集目标字段（用于多行值，遇到下一行引导行则结束）
    cur_field: str | None = None
    cur_buf: list[str] = []
    def _flush():
        nonlocal cur_field, cur_buf
        if cur_field and cur_buf:
            v = '\n'.join(cur_buf).strip()
            if not v:
                pass
            elif cur_field.startswith('__extra_'):
                # 额外字段 → 存到 result_extra
                result_extra[cur_field] = (result_extra.get(cur_field) or '') + v
            elif cur_field == 'name':
                # name 是唯一标识：覆盖初始 title/'未命名'，不做换行累加
                # （否则 title 为空时会产生 "未命名\n林晚" 这类脏名）
                result['name'] = v
            else:
                prev = (result.get(cur_field) or '').strip()
                result[cur_field] = (prev + '\n' + v).strip() if prev else v
        cur_field = None
        cur_buf = []
    all_labels = [label for labels in key_map.values() for label in labels]
    lead_re = re.compile(
        r'^(?:【|\[)?(' + '|'.join(re.escape(x) for x in sorted(all_labels, key=len, reverse=True)) +
        r')(?:】|\])?[:：]?\s*(.*)$'
    )
    for line in lines:
        m = lead_re.match(line)
        if m:
            _flush()
            matched_any = True
            label = m.group(1)
            value = m.group(2).strip()
            # 找出归属字段（先取命中的字段key）
            target = None
            for f, labels in key_map.items():
                if label in labels:
                    target = f
                    break
            if target is None:
                # 兜底：标准字段直接按原行识别
                cur_field = None
            else:
                cur_field = target
                if value:
                    cur_buf = [value]
                else:
                    cur_buf = []
        else:
            if cur_field:
                cur_buf.append(line)
            else:
                # 未在任何引导字段下：当做 personality 的补充文本（常见纯文本场景）
                cur_field = 'personality'
                cur_buf = [line]
    _flush()
    if matched_any:
        if title and not result['name']:
            result['name'] = _clean_character_name(title)
        elif not result['name'] or result['name'] == '未命名':
            result['name'] = _clean_character_name(title) or lines[0][:50]
        # 把额外字段（开场/对白示例风格/备注/源文件）合并到标准字段，避免写 DB 时丢失：
        #   abilities 字段追加 对白示例+说话风格
        if result_extra.get('__extra_first_line'):
            # 开场第一句 → 塞到 background 末尾（便于人物出场直接引用）
            add = f"【出场第一句话/动作】{result_extra['__extra_first_line']}"
            result['background'] = (result['background'] + '\n' + add).strip() if result['background'] else add
        if result_extra.get('__extra_notes'):
            add = f"【创作者备注】{result_extra['__extra_notes']}"
            # 备注 → 塞到 background + personality 末尾（避免丢）
            for slot in ('background', 'personality'):
                prev = (result.get(slot) or '').strip()
                result[slot] = (prev + '\n' + add).strip() if prev else add
        if result_extra.get('__extra_source_fn'):
            add = f"【来源】Silly Tavern 角色卡：{result_extra['__extra_source_fn']}"
            prev = (result.get('background') or '').strip()
            result['background'] = (prev + '\n' + add).strip() if prev else add
        return result

    # 策略4：纯文本兜底
    result['name'] = _clean_character_name(title) or (lines[0][:50] if lines else '未命名')
    result['personality'] = text
    if '主角' in (title or '') or '主角' in text:
        result['role'] = '主角'
    return result


@chat_collab_bp.route('/api/ai/chat/smart/update-card-status', methods=['POST'])
def update_card_status():
    """更新卡片状态（用于忽略等不落地的操作持久化）。

    body: { session_id, card_id, status: 'ignored'|'adopted'|'appended'|'edited' }
    返回: { ok: true }
    """
    data = request.json or {}
    session_id = data.get('session_id')
    card_id = data.get('card_id')
    new_status = data.get('status', 'ignored')
    if not session_id or not card_id:
        return jsonify({'error': '缺少 session_id 或 card_id'}), 400
    if new_status not in ('ignored', 'adopted', 'appended', 'edited'):
        return jsonify({'error': '无效的 status'}), 400
    _persist_card_status(session_id, card_id, new_status)
    return jsonify({'ok': True})


@chat_collab_bp.route('/api/books/<book_id>/ai/progress', methods=['GET'])
def get_progress(book_id):
    """创作进度地图。"""
    from app import BookBible
    bb = BookBible.query.filter_by(book_id=book_id).first()
    return jsonify(build_progress_map(bb))


@chat_collab_bp.route('/api/books/<book_id>/ai/sessions', methods=['GET'])
def list_sessions(book_id):
    """列出该书所有聊天会话。message_count 优先取全量存档数（messages_json 是瘦身版会偏小）。"""
    from app import AISession
    sessions = AISession.query.filter_by(book_id=book_id).order_by(AISession.updated_at.desc()).all()
    counts = _archive_count_map([s.id for s in sessions])
    return jsonify({'sessions': [
        {'id': s.id, 'scope': s.scope, 'title': s.title,
         'updated_at': s.updated_at.isoformat() if s.updated_at else None,
         'message_count': counts.get(s.id, len(json.loads(s.messages_json or '[]')))}
        for s in sessions
    ]})


@chat_collab_bp.route('/api/ai/sessions/<session_id>/messages', methods=['GET'])
def get_session_messages(session_id):
    """获取单个聊天会话的全部消息（用于历史会话切换时加载聊天记录）。

    全量存档优先（含未采纳卡片的完整正文，刷新后完整恢复）；无存档（旧会话/存档失败）
    回退 messages_json 瘦身版；存档非空时把 messages_json 尾部存档缺失的消息补上
    （个别轮次存档写入失败的兜底），保证历史对话永不为空。

    返回：{ id, title, scope, messages: [...] }
    messages 元素结构：{ role, content, cards? }
    """
    from app import AISession
    session = AISession.query.get(session_id)
    if not session:
        return jsonify({'error': '会话不存在'}), 404
    archived = _archive_load_full(session_id)
    if archived is None:
        msgs = load_session_messages(session)
    else:
        msgs = archived
        mj = load_session_messages(session)
        if isinstance(mj, list) and mj:
            have_seqs = {m.get('_seq') for m in archived if isinstance(m.get('_seq'), int)}
            for m in mj:
                if not isinstance(m, dict):
                    continue
                sq = m.get('_seq')
                if isinstance(sq, int) and sq in have_seqs:
                    continue
                d = {k: v for k, v in m.items() if k != '_seq'}
                msgs.append(d)
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
# 支持的动作：master_create 批量生成设定（每维度产一张卡）/ continue 续写本章 /
#             polish 润色本章（均调 ai-continue/stream，正文产 SAVE_CHAPTER 卡）
# SSE 副驾协议：delta 流式正文 / card 落地卡片 / done 结束 / error 错误
# ============================================================================

# 动作 → 默认维度（master_create 用）
_ACTION_DIMENSIONS = {
    # 人物在大纲之后：批量生成时大纲先进 generated 上下文，人物按大纲各卷目标锚定设计
    'master_create': ['concept', 'key_rules', 'worldbuilding', 'plot_design', 'character_profiles'],
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

    body: { book_id, action, session_id?, instruction?, target_chapter_num?, prev_chapter_content?, skill_pack_ids? }
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
    skill_pack_ids = data.get('skill_pack_ids') or []

    if not book_id or action not in ('master_create', 'continue', 'polish'):
        return jsonify({'error': '参数无效，action 必须为 master_create/continue/polish'}), 400

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404

    # 复用或创建会话（【会话隔离铁律】：session.book_id != book_id 就丢弃，不让旧书历史污染新书）
    title_map = {'master_create': '批量生成设定', 'continue': '续写本章', 'polish': '润色本章'}
    session = _get_or_create_session_for_book(session_id, book_id, scope='general',
                                              title=title_map.get(action, 'AI动作'))
    session_id = session.id

    # ===== 【卷数/章数意图·落地前置】副驾快捷按钮的 instruction 也可能含卷数/章数要求 =====
    bb = BookBible.query.filter_by(book_id=book_id).first()
    params_sync_notes_action = _auto_sync_params_from_user_message(book, bb, instruction or '')
    if bb is None:
        bb = BookBible.query.filter_by(book_id=book_id).first()

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
            # === SSE 双兜底·第 1 层：函数第一行先发心跳注释帧 + start受理帧，占住连接防超时 ===
            yield ': ping-heartbeat-keepalive\n\n'
            yield sse({'type': 'meta', 'kind': 'start', 'info': {'action': action, 'session_id': session_id}})
            # 副驾首帧：参数同步说明（若有）
            if params_sync_notes_action:
                yield sse({'type': 'meta', 'kind': 'params_sync', 'info': {'notes': params_sync_notes_action}})
            if action == 'master_create':
                yield from _action_master_create(book, session, instruction, gw, sse)
            elif action == 'continue':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='continue',
                                           base_url=base_url, api_key=api_key, model=model,
                                           skill_pack_ids=skill_pack_ids)
            elif action == 'polish':
                yield from _action_chapter(book, session, instruction, gw, sse,
                                           target_chapter_num, prev_chapter_content, mode='polish',
                                           base_url=base_url, api_key=api_key, model=model,
                                           skill_pack_ids=skill_pack_ids)
        except Exception as e:
            try:
                yield sse({'type': 'error', 'error': f'{type(e).__name__}: {e}'})
            except Exception:
                # yield 本身失败说明连接已彻底断开，静默即可
                pass
        finally:
            try:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            except Exception:
                # finally 里的 commit/rollback 再失败也不能裸抛，否则 SSE 帧格式畸形
                pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


def _action_master_create(book, session, instruction, gw, sse):
    """批量生成设定：逐维度调用 LLM，每维度产出一张落地卡片。"""
    from app import BookBible
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()
    dims = _ACTION_DIMENSIONS['master_create']

    # 构建各维度上下文（已生成维度作为下游上下文）
    generated = {}
    emitted_cards = []  # 收集已发出的卡片对象（id 与前端一致，用于持久化）
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
            # 【纯文字铁律】JSON 存储维度注入前一律转自然语言（人物/剧情线），防下游模仿 JSON
            if k == 'character_profiles' and v.startswith('['):
                v = _character_profiles_to_text(v)
            elif k == 'timeline' and (v.startswith('[') or v.startswith('{')):
                v = _json_to_plain_text(v)
            ctx_parts.append(f'【{_DIM_LABELS.get(k, k)}】\n{v}')
        ctx_block = '\n\n'.join(ctx_parts) if ctx_parts else '（暂无）'

        # 注入核心创作参数铁律（批量设定也要遵守总卷数/题材/风格）
        core_iron = _core_params_iron_block(bb, book)

        # =====【P0 修复·统一短 prompt 也加细项约束·禁止两句话就结束】=====
        # 各维度的分节清单/字数下限/细项强制说明，master_create 与 smart_generate 共享口径
        concept_more = (
            '\n\n【构思维度铁律·禁止两句话】必须输出 10 节：'
            '①一句话故事核Logline ②主题曲线(起点→反诘→抉择→终局) ③核心冲突三角(主角×对手×世界规则) '
            '④目标分层(短/中/长/终极+失败代价) ⑤核心爽感机制(3-5种主爽点+触发→爆发→余波+卷1/3/5/终局排布) '
            '⑥金手指/外挂(类型+核心能力+分级+硬约束代价+贴合执念+终极风险) '
            '⑦主角魅力公式(记忆符号+三重反差+具体创伤+核心执念) ⑧对手/反派魅力(前/中/终局三级反派的合理诉求/创伤/赢面/软肋/镜像点) '
            '⑨世界观3-5个独特卖点钩子 ⑩全书情感底色+读者定位+文风力向。'
            '总字数不少于 1200 字，每节必须写具体可落地内容，禁止"待设定/后续再定"。'
        ) if dim == 'concept' else ''
        key_rules_more = (
            '\n\n【设定维度铁律·禁止只写境界表】必须输出 11 节：'
            '①力量总体系(2主1辅+克制) ②等级阶梯表(命名+战力差+突破门槛+社会地位+寿元+战力天花板) '
            '③至少2主1偏的提升路径差异 ④功法/技能树(5类分级+代表性技能+配搭+获取方式) '
            '⑤资源与货币体系(通用货币+等价物+至少10项物品价格表+资源产地+修炼成本贫富差) '
            '⑥装备/法宝/载具(分级+获取+认主+损耗+3-5件代表作) '
            '⑦至少2-3种副职业(炼丹/炼器/阵法/符箓/御兽/编程/制造)+代表物品配方价格 '
            '⑧硬约束+反噬代价(越级/禁术/心魔/境界掉落+金手指冷却/资源消耗) '
            '⑨种族/职业/阵营总表+历史矛盾+跨种族禁忌 ⑩至少8条世界硬规则禁忌(执法者+灰色地带) '
            '⑪文明水平总览(科技树/修炼文明层级)。'
            '总字数不少于 1500 字，必须有具体数字和例子。'
        ) if dim == 'key_rules' else ''
        worldbuilding_more = (
            '\n\n【世界观铁律·禁止只写四大域】必须输出 15 节（最少 2000 字）：'
            '①世界总览(宇宙/位面/主舞台轮廓) ②创世元史三段(神话版/正史版/隐藏版) + 至少3纪元大事 '
            '③至少6大地理分块(含开局/中期/禁地/天堑4类区域) ④气候天象体系(周期+对修炼/战争/经济影响+节日仪式) '
            '⑤至少8大主要势力(名/领袖/地盘/战力/经济/理念/剧情位) ⑥完整阶级金字塔(顶/中/底/禁忌群体+流动通道) '
            '⑦政治律法(国体/法律/执法/审判/灰色地带) ⑧经济贸易(产业/商路/3个交易都市/黑市/货币权) '
            '⑨至少5个智慧种族(外貌/寿命/栖息/优势/短板/文化/关系/混血地位) '
            '⑩至少2正统+1邪教宗教信仰(神系/教会结构/信仰力量/教权皇权关系/邪教土壤) '
            '⑪语言文字度量衡历法(通用语/古语/识字率/度量/节日) ⑫风俗礼仪服饰饮食建筑 '
            '⑬军事体系(组织/兵种/阵法/战争规模/战后恢复) ⑭交通通讯(跑图速度成本+传信速度保密) '
            '⑮至少5个世界未解之谜/禁忌之地/上古遗留(每谜都要写与主线关联)。'
        ) if dim == 'worldbuilding' else ''
        character_more_master = (
            '\n\n【人物维度铁律·禁止只给姓名+一句话身份】至少写出 主角 + 1女主/重要女配 + 2核心配角 + 1前期反派 + 1中期反派：'
            '每个角色按以下 15 项写满：'
            '1)姓名(含字号/外号/别名) 2)性别/年龄(开篇时) 3)外貌特征(含专属记忆符号：疤痕/旧物/佩饰/小动作/口头禅) '
            '4)身份地位(出身/职业/阶层/职称) 5)核心性格三原色(主色/辅色/应激色)+优点/缺点/道德底线 '
            '6)核心价值观+行为准则 7)人生三目标(短/中/长) 8)深层动机+执念来源 '
            '9)具体核心创伤(哪年哪日谁做了什么/失去了什么/留下什么身体或心理印记) 10)恐惧与软肋(最怕什么/被捏住什么就失控) '
            '11)能力体系(主职能力+辅助能力+金手指权限等级/掌握度/战力量化) '
            '12)擅长战斗/不擅长战斗的情况、习惯武器/法器、战斗风格 '
            '13)人物背景故事(家庭/成长经历/教育经历/重要事件/形成原因300字以上) '
            '14)关键关系网(家人/师友/爱人/仇敌/上司/下属 至少8人，每人关系类型+态度+羁绊来源+利益交集) '
            '15)角色弧线：开篇状态 → 转折事件 → 中期转变 → 终局归宿/结局。'
            '每个角色的功能位与出场卷次须与【大纲】各卷人物方向对应。'
            '每个角色至少 300 字，合计不少于 1800 字；纯中文按字段分行输出，禁止 JSON 符号。'
        ) if dim == 'character_profiles' else ''
        plot_design_more = (
            '\n\n【大纲维度铁律·禁止只写几句话五幕】必须写满：'
            '五幕(立身/立足/立势/立威/立命)对应到连续卷号 + 每卷6项指标 '
            '(①本卷爽点4小1大 ②人物方向1句话(具体名单归人物/剧情维度) ③地点动线方向1句话(具体清单归剧情/地图维度) '
            '④修炼/事业/财富/关系/势力五项进展 ⑤伏笔主题方向1-2句(具体条目归剧情/伏笔维度) ⑥卷尾得到/失去/新任务)；'
            '结尾附一张【跨卷尾钩子承接总览】(卷1尾 ←接→ 卷2头 ...)。'
            '总字数不少于 1500 字。'
        ) if dim == 'plot_design' else ''

        # 构思阶段·专属规则（通用核心+构思格式约束+master技能包，屏蔽正文行文规范/去AI手册）
        master_conception_rules = build_conception_rules(mode='agent', book=book)
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》生成"{"label"}"设定。'
            f'\n\n{core_iron}'
            f'\n\n【构思阶段·平台内置规则（只注入通用核心+构思格式，不注入正文行文规范，不一股脑全加载）】'
            f'\n{master_conception_rules}'
            f'\n\n已有设定参考：\n{ctx_block}'
            f'\n用户补充要求：{instruction or "无"}'
            f'{concept_more}{key_rules_more}{worldbuilding_more}{character_more_master}{plot_design_more}'
            f'\n请直接输出该维度的完整设定内容（构思≥1200、设定≥1500、世界观≥2000、人物≥1800、大纲≥1500字；文风/伏笔/地图如有也不少于1000字），不要寒暄，不要解释，严格按上面分节依次输出，禁止跳节，禁止出现"待设定/后续再定"这类空话。'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
        )
        if existing:
            sys_prompt += f'\n\n已有内容（可在其基础上补充完善，不要简单重复）：\n{existing[:400]}'

        messages = [{'role': 'system', 'content': sys_prompt},
                    {'role': 'user', 'content': f'请生成{label}'}]
        content = ''
        try:
            # master_create 多维度分节清单更长，统一走按维度配额（见 _DIM_MAX_TOKENS 注释）
            _max_tok = _dim_max_tokens(dim)
            for chunk in gw_stream_with_hb(gw, messages, temperature=0.8, max_tokens=_max_tok):
                if chunk is HEARTBEAT:
                    yield SSE_HEARTBEAT_COMMENT
                    continue
                if _is_stream_retry(chunk):
                    yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
                    continue
                content += chunk
                yield sse({'type': 'delta', 'content': chunk})
        except GeneratorExit:
            # 客户端断开：同步抢救当前维度已生成部分（已完成的维度已随卡片发出，前端有）
            _save_partial_on_disconnect(session, f'批量设定·{label}', instruction or '', content)
            raise
        except Exception as e:
            yield sse({'type': 'error', 'error': f'{label}生成失败：{e}'})
            continue

        content = _clean_text_to_plain(content)
        generated[dim] = content
        # 产出落地卡片
        card = {
            'id': str(uuid.uuid4())[:8],
            'type': card_type,
            'title': f'{label}（AI生成）',
            'content': content,
            'target': _CARD_TARGET.get(card_type, label),
        }
        emitted_cards.append(card)
        yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话（用已发出的卡片对象，保留 id 与前端一致）
    yield from _persist_action_session(session, f'批量生成设定：{instruction or "默认五维度"}',
                                       generated, dims, cards_out=emitted_cards)


# ============================================================================
# 视点感知注入（第三人称有限视角）：正文写作时只给AI看POV角色能知道的信息
# 目标：减少token + 防剧透（不注入未来卷剧情、伏笔谜底、无关人物）
# ============================================================================

def _infer_pov_character(book_id, target_chapter_num, prev_chapter_content):
    """推断当前章的视点人物（POV）。
    策略：从上一章内容中找出现频率最高的已注册人物名；回退到主角；再回退到None。
    """
    from app import Character
    try:
        all_chars = Character.query.filter_by(book_id=book_id).all()
        if not all_chars:
            return None
        # 主角兜底
        protagonist = next((c for c in all_chars if c.role == 'protagonist'), None)
        if not prev_chapter_content:
            return protagonist.name if protagonist else (all_chars[0].name if all_chars else None)
        # 统计每个人物名在上一章出现的次数
        counts = {}
        for c in all_chars:
            n = c.name or ''
            if n and len(n) >= 2:
                counts[n] = prev_chapter_content.count(n)
        # 取出现次数最多的（至少出现1次）
        best = max(counts.items(), key=lambda x: x[1], default=(None, 0))
        if best[1] > 0:
            return best[0]
        return protagonist.name if protagonist else None
    except Exception:
        return None


def _filter_characters_by_pov(book_id, pov_name):
    """人物维度过滤：只注入POV角色 + POV关系网中的人 + 主角。
    返回自然语言文本。大幅减少token，且避免无关人物干扰当前视角。
    """
    from app import Character
    try:
        all_chars = Character.query.filter_by(book_id=book_id).all()
        if not all_chars:
            return ''
        # 确定要注入的人物集合
        target_names = set()
        if pov_name:
            target_names.add(pov_name)
        protagonist = next((c for c in all_chars if c.role == 'protagonist'), None)
        if protagonist:
            target_names.add(protagonist.name)
        # POV的关系网：从relationships_json提取关联人物名
        if pov_name:
            pov_char = next((c for c in all_chars if c.name == pov_name), None)
            if pov_char:
                try:
                    rels = json.loads(pov_char.relationships_json or '[]')
                    for r in rels:
                        if isinstance(r, dict):
                            tn = r.get('target_name') or r.get('name') or r.get('with') or ''
                            if tn:
                                target_names.add(tn)
                except (json.JSONDecodeError, ValueError):
                    pass
        # 过滤 + 转自然语言
        blocks = []
        for c in all_chars:
            if c.name not in target_names:
                continue
            lines = []
            if c.role and c.role != 'supporting':
                role_label = {'protagonist': '主角', 'antagonist': '反派'}.get(c.role, c.role)
                lines.append(f'角色定位：{role_label}')
            for label, val in [('身份描述', c.description), ('外貌', c.appearance),
                               ('性格', c.personality), ('背景', c.background)]:
                v = (val or '').strip()
                if v:
                    lines.append(f'{label}：{v}')
            try:
                rels = json.loads(c.relationships_json or '[]')
                if rels:
                    rel_lines = []
                    for r in rels:
                        if isinstance(r, dict):
                            tn = r.get('target_name') or r.get('name') or r.get('with') or ''
                            rel = r.get('relation') or r.get('type') or ''
                            if tn:
                                rel_lines.append(f'{tn}（{rel}）' if rel else tn)
                    if rel_lines:
                        lines.append('关系：' + '、'.join(rel_lines))
            except (json.JSONDecodeError, ValueError):
                pass
            if lines:
                blocks.append(f'姓名：{c.name}\n' + '\n'.join(lines))
        return '\n\n'.join(blocks)
    except Exception:
        return ''


def _filter_timeline_for_chapter(timeline_raw, target_chapter_num):
    """剧情维度过滤：只注入当前卷及之前卷的剧情。
    当前卷内注入：已发生节点 + 当前节点 + 卷尾钩子（写作目标）。
    不注入后续卷（防剧透）。返回 (文本, 是否截断了后续卷)。
    """
    if not timeline_raw or not timeline_raw.strip():
        return '', False
    text = timeline_raw.strip()
    # 尝试解析为JSON卷列表
    try:
        vols = json.loads(text)
        if not isinstance(vols, list):
            return text, False
        kept = []
        truncated = False
        for v in vols:
            if not isinstance(v, dict):
                continue
            vol_idx = v.get('volume_index') or v.get('volume_id') or '?'
            vol_name = v.get('volume', f'第{vol_idx}卷')
            # 判断该卷是否在target之前或当前
            nodes = v.get('nodes') or []
            vol_has_current = False
            vol_is_past = False
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                ch_range = str(n.get('chapters', ''))
                nums = re.findall(r'\d+', ch_range)
                if len(nums) >= 2:
                    if int(nums[-1]) < target_chapter_num:
                        vol_is_past = True
                    if int(nums[0]) <= target_chapter_num <= int(nums[-1]):
                        vol_has_current = True
            # 只保留"有已发生或当前节点"的卷
            if vol_is_past or vol_has_current:
                vol_lines = [f'第{vol_idx}卷《{vol_name}》']
                main_plot = v.get('main_plot') or ''
                if main_plot:
                    vol_lines.append(f'本卷主线：{main_plot}')
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    ch_range = n.get('chapters', '')
                    summary = n.get('summary') or n.get('plot') or ''
                    if summary:
                        nums = re.findall(r'\d+', str(ch_range))
                        # 当前卷内：只注入已发生+当前节点，未来节点不注入
                        if vol_has_current and len(nums) >= 2:
                            if int(nums[0]) > target_chapter_num:
                                continue  # 跳过本卷未来节点
                        vol_lines.append(f'  · [{ch_range}] {summary}')
                ending_hook = v.get('ending_hook') or ''
                if ending_hook and (vol_is_past or vol_has_current):
                    vol_lines.append(f'卷尾钩子：{ending_hook}')
                kept.append('\n'.join(vol_lines))
            else:
                truncated = True  # 有后续卷被跳过
        return ('\n\n'.join(kept) if kept else text, truncated)
    except (json.JSONDecodeError, ValueError):
        # 纯文本timeline：无法结构化过滤，返回原文但标记
        return text, False


def _get_chapter_plot_node(timeline_raw, outline_hierarchy_raw, target_chapter_num):
    """精确命中本章情节节点（写作/润色时注入，让AI知道"本章该写什么剧情"）。

    优先级：
    1. outline_hierarchy（四级层级，精确到单章+戏剧位置起/承/转/合）
    2. timeline JSON（卷>情节节点，按 nodes.chapters 章节范围匹配单个节点）
    3. 都拿不到 → 返回空串（由调用方走 _filter_timeline_for_chapter 卷级注入兜底）

    返回：拼好的"本章剧情"文本块（含所属卷/节点标题/摘要/戏剧位置），空串表示未命中。
    """
    if not target_chapter_num:
        return ''

    # ---- 优先级1：outline_hierarchy 精确到单章 ----
    if outline_hierarchy_raw and outline_hierarchy_raw.strip():
        try:
            from outline_hierarchy_builder import get_dramatic_context, build_dramatic_position_prompt
            hierarchy = json.loads(outline_hierarchy_raw)
            ctx = get_dramatic_context(hierarchy, target_chapter_num)
            if ctx:
                lines = []
                ch = ctx.get('chapter') or {}
                sec = ctx.get('section') or {}
                arc = ctx.get('arc') or {}
                if arc.get('arc_name'):
                    lines.append(f'所属卷：{arc["arc_name"]}')
                if arc.get('arc_theme'):
                    lines.append(f'卷主题：{str(arc["arc_theme"])[:60]}')
                if sec.get('purpose') or sec.get('title'):
                    lines.append(f'所属情节节点：{sec.get("purpose") or sec.get("title")}')
                if sec.get('summary') or sec.get('section_emotional_arc'):
                    lines.append(f'节点概要：{sec.get("summary") or sec.get("section_emotional_arc")}')
                if ch.get('dramatic_position'):
                    lines.append(f'本章戏剧位置：{ch["dramatic_position"]}')
                if ch.get('content_focus'):
                    lines.append(f'本章重点：{ch["content_focus"]}')
                if lines:
                    return '\n'.join(lines)
        except Exception:
            pass  # 降级到 timeline 匹配

    # ---- 优先级2：timeline JSON 按章号范围匹配单节点 ----
    if not timeline_raw or not timeline_raw.strip():
        return ''
    try:
        vols = json.loads(timeline_raw.strip())
        if not isinstance(vols, list):
            return ''
        for v in vols:
            if not isinstance(v, dict):
                continue
            nodes = v.get('nodes') or []
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                ch_range = str(n.get('chapters', ''))
                nums = re.findall(r'\d+', ch_range)
                if len(nums) >= 2 and int(nums[0]) <= target_chapter_num <= int(nums[-1]):
                    # 命中该节点
                    lines = []
                    vol_name = v.get('volume', f'第{v.get("volume_index","?")}卷')
                    lines.append(f'所属卷：{vol_name}')
                    if v.get('main_plot'):
                        lines.append(f'卷主线：{str(v["main_plot"])[:80]}')
                    node_title = n.get('title', '未命名节点')
                    lines.append(f'所属情节节点：{node_title}（{ch_range}章）')
                    # ===== A方案：优先用章粒度 chapter_beats 切出"本章只推进的这一段" =====
                    beats = n.get('chapter_beats') if isinstance(n.get('chapter_beats'), list) else []
                    beat_for_this_chapter = ''
                    if beats:
                        for b in beats:
                            if not isinstance(b, dict):
                                continue
                            try:
                                if int(b.get('chapter')) == target_chapter_num:
                                    beat_for_this_chapter = str(b.get('beat') or '')
                                    break
                            except (TypeError, ValueError):
                                continue
                    if beat_for_this_chapter:
                        ch_lo, ch_hi = int(nums[0]), int(nums[-1])
                        lines.append(f'本章剧情点：{beat_for_this_chapter}')
                        if ch_lo < ch_hi:
                            remaining = ch_hi - target_chapter_num
                            if remaining > 0:
                                lines.append(f'【边界约束】本节点横跨 {ch_lo}-{ch_hi} 章，本章只许推进上述剧情点；剩余 {remaining} 章的剧情（含高潮/反转/收尾/钩子）留到后续章，禁止在本章一次性写完。')
                    else:
                        # 无章粒度数据（旧数据/未填）：回退整段 summary，但补边界约束
                        summary = n.get('summary') or n.get('plot') or ''
                        if summary:
                            lines.append(f'节点概要：{summary}')
                            ch_lo, ch_hi = int(nums[0]), int(nums[-1])
                            if ch_hi > ch_lo:
                                lines.append(f'【边界约束】本节点横跨 {ch_lo}-{ch_hi} 章，本章只写其中与第 {target_chapter_num} 章对应的那一段；不得把整段起因→高潮→收尾→钩子一章写完，后续章内容须保留。')
                    if n.get('cool_type') and not beat_for_this_chapter:
                        lines.append(f'爽点类型：{n["cool_type"]}')
                    if v.get('ending_hook') and int(nums[-1]) == target_chapter_num:
                        lines.append(f'卷尾钩子：{v["ending_hook"]}')
                    return '\n'.join(lines)
        return ''  # 没命中任何节点
    except (json.JSONDecodeError, ValueError):
        return ''


def _filter_foreshadowing_for_chapter(foreshadow_raw, foreshadowing_graph_json, target_chapter_num, bb=None):
    """M3: 伏笔维度过滤：用 ContextBus 算完整任务清单（应埋/应收/禁揭示），fallback 到文本+强约束。"""
    if not target_chapter_num:
        return foreshadow_raw.strip() if foreshadow_raw else ''

    # 优先从结构化 DAG 计算完整任务清单
    if foreshadowing_graph_json and bb:
        try:
            from context_ranker import ContextBus
            mission = ContextBus.get_hook_mission(bb, target_chapter_num)
            if mission:
                return mission + '\n\n【伏笔防剧透铁律】严禁提前揭示未到回收时机的伏笔谜底；POV 未察觉的伏笔只能以客观现象/旁枝线索出现，不能给读者上帝视角。'
        except Exception:
            pass

    # fallback：文本全量注入 + 强约束
    if not foreshadow_raw or not foreshadow_raw.strip():
        return ''
    return (
        foreshadow_raw.strip()
        + '\n\n【伏笔防剧透铁律】以上伏笔中，未到回收时机的严禁揭示谜底；'
        '只允许呼应 POV 已察觉的客观线索。'
    )


def _log_validation_issues(bb, dim_key: str, issues):
    """M4: 将 error 级自检问题写入 FailureDB"""
    if not bb or not issues:
        return
    try:
        from meta_optimizer import log_failure
        for issue in issues:
            if issue.severity != 'error':
                continue
            cat_map = {
                'JSON_INVALID': 'format',
                'VOL_COUNT_MISMATCH': 'structure',
                'VOL_INDEX_GAP': 'structure',
                'CH_NUM_OVERFLOW': 'structure',
                'CH_NUM_UNDERFLOW': 'structure',
                'FIELD_MISSING': 'structure',
                'ACT_COUNT_MISMATCH': 'structure',
                'CHAR_JSON_LEAK': 'format',
            }
            category = cat_map.get(issue.code, 'content')
            log_failure(bb, category, dim_key=dim_key, summary=f'[{issue.code}] {issue.message}',
                        snippet=issue.auto_fix or issue.message, fix_hint=issue.auto_fix)
    except Exception:
        pass


def _get_event_log_ctx(bb, target_chapter_num, limit=5):
    """M2: 从 EventLog 拉取上一章/最近事件，作为本章写作前的"最新动态"。"""
    if not bb or not bb.event_log_json or not target_chapter_num:
        return ''
    try:
        from event_log_manager import EventLogManager
        events = EventLogManager.load(bb)
        if not events:
            return ''
        # 取上一章的事件 + 最近几条更早的事件
        prev_events = [e for e in events if e.chapter_num == target_chapter_num - 1]
        recent = [e for e in events if e.chapter_num < target_chapter_num and e not in prev_events]
        recent.sort(key=lambda x: x.chapter_num, reverse=True)
        selected = (prev_events + recent)[:limit]
        if not selected:
            return ''
        lines = ['【前情提要·事件序列】']
        for e in sorted(selected, key=lambda x: x.chapter_num):
            actors = '、'.join(e.actors) if e.actors else '（无）'
            loc = f'｜地点：{e.location}' if e.location else ''
            lines.append(f'· 第{e.chapter_num}章｜{e.type}｜{actors}{loc}｜{e.summary}')
        return '\n'.join(lines)
    except Exception:
        return ''


def _filter_dynamic_reports_for_chapter(book_id, target_chapter_num, limit=5):
    """动态报告过滤：只注入 chapter_end < target_chapter_num 的报告（已发生事件摘要）。
    防止未来事件泄露给当前章创作。
    """
    from app import DynamicReport
    try:
        reports = DynamicReport.query.filter_by(book_id=book_id) \
            .filter(DynamicReport.chapter_end < target_chapter_num) \
            .order_by(DynamicReport.chapter_end.desc()).limit(limit).all()
        if not reports:
            return ''
        lines = []
        for r in reports:
            title = r.title or f'动态({r.chapter_start}-{r.chapter_end}章)'
            content = (r.content or '').strip()
            lines.append(f'· {title}：\n{content}')
        return '\n\n'.join(lines)
    except Exception:
        return ''




# ========================================================================
# B2：风格对齐 SkillPack（style_packs/ 目录下的 txt 范本自动注入）
# 开关优先级：① style_skill_ids 含禁用词（none/off/禁用等）→ 彻底关
#   ② 含具体 pack id（如 fantasy_xuanhuan_v1）→ 强制启用  ③ 否则按 book.genre 自动匹配
# ========================================================================

_STYLE_PACK_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'style_packs')
_style_manifest_cache: Optional[Dict] = None


def _load_style_manifest() -> Dict:
    global _style_manifest_cache
    if _style_manifest_cache is not None:
        return _style_manifest_cache
    try:
        with open(os.path.join(_STYLE_PACK_ROOT, '_manifest.json'), 'r', encoding='utf-8') as f:
            _style_manifest_cache = json.load(f)
    except Exception:
        _style_manifest_cache = {'packs': [], 'disable_keywords': []}
    return _style_manifest_cache


def _parse_style_ids(book) -> List[str]:
    """从 Book.style_skill_ids（JSON 数组字符串）解析用户显式指定/禁用的文风包 id 列表。"""
    raw = getattr(book, 'style_skill_ids', None) or '[]'
    try:
        arr = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [str(x).strip() for x in arr if str(x).strip()]
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def _get_enabled_style_pack(book) -> str:
    """返回当前书应该启用的风格 pack 注入正文（空字符串表示不启用任何 pack）。"""
    manifest = _load_style_manifest()
    packs = manifest.get('packs') or []
    if not packs:
        return ''
    # 1) 显式禁用
    explicit = _parse_style_ids(book)
    disabled = {k.lower() for k in (manifest.get('disable_keywords') or [])}
    if any(e.lower() in disabled for e in explicit):
        return ''
    # 2) 显式指定 pack id（只取第一个命中的）
    if explicit:
        for p in packs:
            if p.get('id') in explicit:
                try:
                    with open(os.path.join(_STYLE_PACK_ROOT, p['file']), 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception:
                    return ''
    # 3) 按 genre 自动匹配
    genre = (getattr(book, 'genre', None) or 'other').strip()
    if not genre:
        return ''
    genre_low = genre.lower()
    matched = None
    for p in packs:
        if not p.get('enabled_by_default', True):
            continue
        for g in (p.get('genre_match') or []):
            if not g:
                continue
            gl = str(g).lower()
            if gl in genre_low or genre_low in gl:
                matched = p
                break
        if matched:
            break
    if not matched:
        return ''
    try:
        with open(os.path.join(_STYLE_PACK_ROOT, matched['file']), 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''




def _action_chapter(book, session, instruction, gw, sse, target_chapter_num, prev_chapter_content, mode,
                    base_url=None, api_key=None, model=None, skill_pack_ids=None):
    """续写/润色本章正文：产 SAVE_CHAPTER 卡。
    视点感知注入（第三人称有限视角）：只给AI看POV角色能知道的信息，减少token + 防剧透。
    - 人物：只注入POV + POV关系网 + 主角
    - 剧情：只注入当前卷及之前卷（当前卷内只注入已发生节点）
    - 动态报告：只注入target章之前的报告
    - 伏笔：注入但加强约束（严禁揭示未到回收时机的谜底）
    - 世界观/规则/文风/地点/构思：全量注入（写作基础，不剧透）
    """
    # === SSE 双兜底：正文阶段上下文组装耗时较久（DB查询+规则拼接=200-500ms），先yield一帧占住连接 ===
    mode_label = '续写' if mode == 'continue' else '润色'
    yield sse({'type': 'delta', 'content': f'\n正在{mode_label}第 {target_chapter_num or "?"} 章…\n\n'})
    from app import db, BookBible, Chapter, _get_total_volumes, _get_chapters_per_volume, parse_chapter_number
    book_id = book.id
    bb = BookBible.query.filter_by(book_id=book_id).first()

    # ====== 核心创作参数铁律 + 越界硬拦截（正文写作第一道门） ======
    tv = _get_total_volumes(bb, book)
    cpv = _get_chapters_per_volume(bb, book)
    max_chapters = tv * cpv  # 总章数上限 = 总卷数 × 每卷章数
    core_iron = _core_params_iron_block(bb, book)

    # 统一口径：从章节表提取最新章节号（写作/修改/去AI共用）
    ch_info = _get_latest_chapter_info(book_id)
    # 确定当前章号 + 上一章内容
    if not target_chapter_num:
        # 续写用“最新章节号+1”，润色用“最新章节号”
        target_chapter_num = ch_info['next_num'] if mode == 'continue' else ch_info['latest_num']

    # 越界硬拦截：章号超过总章数上限立即停止，不发 LLM 请求
    if target_chapter_num and target_chapter_num > max_chapters:
        yield sse({'type': 'error',
                   'error': (f'【核心参数越界拦截】全书设定总卷数 {tv} 卷 × 每卷 {cpv} 章 = 总章数上限 {max_chapters} 章，'
                             f'当前请求第 {target_chapter_num} 章已超出上限。若需要继续写作，请先到作品基本信息中调大总卷数。')})
        return

    if not prev_chapter_content:
        # 统一口径：target_chapter_num 是 1-based 章号；
        # 找"上一章" = 找 display 章号为 target_chapter_num - 1 的章节，
        # order_index 最大不超过 (target_chapter_num - 1) - 1 = target_chapter_num - 2
        # （因为纯 order_index 兜底时 displayNum = order_index + 1）
        prev = Chapter.query.filter_by(book_id=book_id, is_volume=False) \
            .filter(Chapter.order_index < target_chapter_num - 1) \
            .order_by(Chapter.order_index.desc()).first()
        # 但若上面没找到（比如中间有删章/标题章号与顺序错位），回退到最接近 target_chapter_num 之前的那一章
        if not prev:
            # 按标题解析 displayNum < target_chapter_num 的最大那章，兜底用 order_index
            all_prev_candidates = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
            best_prev, best_display = None, 0
            for cp in all_prev_candidates:
                p_num = parse_chapter_number(cp.title or '') or (cp.order_index + 1 if cp.order_index is not None else 0)
                if p_num < target_chapter_num and p_num > best_display:
                    best_prev, best_display = cp, p_num
            prev = best_prev
        # 再回退：若按上面取不到，用统一口径的最新章节
        if not prev and ch_info['latest_chapter'] and mode == 'continue':
            prev = ch_info['latest_chapter']
        prev_chapter_content = (prev.content or '')[:2000] if prev else ''

    # 推断POV视点人物
    pov_name = _infer_pov_character(book_id, target_chapter_num, prev_chapter_content)

    # === 视点感知上下文构建 ===
    # 1. 人物维度：只注入POV + 关系网 + 主角（大幅省token，防无关人物干扰）
    char_ctx = _filter_characters_by_pov(book_id, pov_name)
    # 若Character表为空，回退到bible.character_profiles
    if not char_ctx and bb and bb.character_profiles:
        cp = bb.character_profiles.strip()
        if cp.startswith('['):
            char_ctx = _character_profiles_to_text(cp)
        else:
            char_ctx = cp

    # 2. 剧情维度：
    #    2a. 精确命中本章情节节点（让AI知道"本章该写什么"）
    #    2b. 当前卷及之前卷的剧情脉络（宏观补充，防后续卷剧透）
    chapter_plot_ctx = ''
    if bb:
        chapter_plot_ctx = _get_chapter_plot_node(
            bb.timeline, bb.outline_hierarchy, target_chapter_num)

    timeline_ctx, timeline_truncated = '', False
    if bb and bb.timeline:
        timeline_ctx, timeline_truncated = _filter_timeline_for_chapter(bb.timeline, target_chapter_num)

    # 3. 动态报告：只注入target章之前的（防未来事件泄露）
    dynamic_reports_ctx = _filter_dynamic_reports_for_chapter(book_id, target_chapter_num)

    # 4. 伏笔：注入但后续prompt加强约束
    foreshadow_ctx = _filter_foreshadowing_for_chapter(
        bb.foreshadowing if bb else '',
        bb.foreshadowing_graph if bb else '',
        target_chapter_num,
        bb)

    # M2: 事件序列上下文（前情提要）
    event_log_ctx = _get_event_log_ctx(bb, target_chapter_num)

    # 5. 世界观/规则/文风/地点/构思/大纲：全量注入（写作基础，不剧透）
    #    大纲(plot_design)是总纲，注入但加约束"仅作宏观方向，不可剧透未发生转折"
    static_dims = []
    for d in SMART_DIMENSIONS:
        if d['key'] in ('character_profiles', 'timeline', 'foreshadowing'):
            continue  # 这三个已单独处理
        v = (getattr(bb, d['field'], '') or '').strip() if bb else ''
        if v:
            static_dims.append(f'【{d["label"]}】\n{v}')
    static_ctx = '\n\n'.join(static_dims)

    # === ONE 主钩子 + 本章剧情数字硬约束（与core_iron并列，优先级高于所有设定/规则/字数）===
    # 从 chapter_plot_ctx 里推断 ONE 主钩子（取第一个节点里的核心对象/核心任务）
    chapter_plot_iron = ''
    if chapter_plot_ctx:
        chapter_plot_iron = (
            '【本章剧情·最高指令】书接上文，读取剧情维度里的「本章剧情节点」，禁止超出本章剧情节点创作，保证ONE主钩子贯穿本章、禁止无目标流水账。语句自然顺畅，写事为主，景一笔带过，非必要不用比喻/拟人等修辞。'
            f'\n\n本章必须写完且只写以下 {len([x for x in chapter_plot_ctx.splitlines() if x.strip()])} 个节点（顺序不得调换、不得跳过、不得新增）：'
            f'\n{chapter_plot_ctx}'
            '\n\n【本章边界铁律·防超纲透支】本节点横跨多章时，本章只推进「本章剧情点」里指定的那一段：\n'
            '  · 只写完本章对应推进内容，未到位的后续剧情（高潮/反转/收尾/钩子）一律留到节点跨度的后续章，禁止一章把所有节点剧情全程写完；\n'
            '  · 若「本章剧情点」已给出，就严格按它写，不自行把 summary 整段拍进去；\n'
            '  · 若只有「节点概要」（旧数据无按章细分），只写其中属于第 X 章的一段，因果链可在本章起始一笔交代前情，但后续关键推进必须保留到后续章节。'
            '\n\n【本章字数铁律】纯正文（不含标题）2300-2500字，全角中文标点；不足时扩事件对白/停顿情绪/推进动作，超了删枝节。（⚠️ 遵守【禁令0】不得凑字/超标。写事为主，景一笔带过，非必要不用比喻/拟人等修辞，宁可字数微欠也不靠剩料描写充数）'
        )

    # === B2 风格对齐 SkillPack：自动匹配玄幻/都市范本，注入在 chapter_plot_iron 之后（第 3 高位）===
    style_pack_prompt = _get_enabled_style_pack(book)

    # === 事前生成禁令·6条（反例矿道文暴露出的核心硬约束，第3高位，优先级最高档）===
    #   注入位置：core_iron → ONE主钩子 → 【事前禁令7条】 → style_pack → writing_rules
    #   ⚠️ 禁令5（段均句数≤1.8，不卡总段数）已移到 GENERAL_CORE_RULES 总则里全阶段通用，这里不再重复写 2 遍。
    PRE_GENERATE_BAN_RULES = '''【事前生成禁令·6条（比任何规则都高，违反直接扣分/重写）】
禁令0·禁AI修正式否定句密集症 → 【详见通用核心铁律·禁令0】；禁令5·漫画分镜一句话一段话 → 【详见通用核心铁律·禁令5+铁律A+行文规范段落合并节】（以上两条下文均有完整细则，此处不复述）
禁令1·禁背景板一次性名字段≥8段：群像围观（点卯台/集会/街道/战场围观群众）只允许1段集中描写；严禁把 8 个以上路人/背景板角色各自拆成独立一段，必须合成≤2段【详见行文规范群像并列节】。
禁令2·禁空转流水账重复动作：任何重复性劳作/推进行为 → 必须遵守【行文规范黄金4型·动作链】"小目标→决策→验证"每 200-300 字一个闭环，严禁只写"继续/还在/接着做"无验证句。
禁令3·禁对白提示语句首+对白独立段：对白提示语句首占比 ≤40%；连续 3 句对白至少 1 句与动作/反应同段，禁止每句对白单独成段像剧本台词【详见行文规范对白节】。
禁令4·禁群像平级并列段：点卯台/围观/集会/站队类多角色场景，必须有≥1条「递进比较链」；不许 3 段以上平级并列堆段【详见行文规范黄金4型·递进比较链】。
禁令6·禁结尾真相直白点破：结尾为真相揭露时绝对禁止直接陈述句点破，必须改写成动态动作句收尾【详见行文规范结尾钩子·真相直白点破禁】。'''


    # 组装上下文块（本章剧情已提上去写在core_iron之后，这里bible_ctx里就不再重复chapter_plot_ctx）
    ctx_blocks = []
    if static_ctx:
        ctx_blocks.append(static_ctx)
    if char_ctx:
        pov_note = f'（本章视点人物：{pov_name}，第三人称有限视角，只写{pov_name}能感知到的事物）' if pov_name else '（第三人称有限视角）'
        ctx_blocks.append(f'【人物档案·视点感知】{pov_note}\n{char_ctx}')
    if timeline_ctx:
        trunc_note = '\n（注：后续卷剧情已省略，防剧透）' if timeline_truncated else ''
        ctx_blocks.append(f'【本卷及过往剧情脉络】{trunc_note}\n{timeline_ctx}')
    if foreshadow_ctx:
        ctx_blocks.append(f'【伏笔线索】\n{foreshadow_ctx}')
    if event_log_ctx:
        ctx_blocks.append(event_log_ctx)
    if dynamic_reports_ctx:
        ctx_blocks.append(f'【近期动态文件（已发生事件摘要）】\n{dynamic_reports_ctx}')
    bible_ctx = '\n\n'.join(ctx_blocks) or '（暂无设定）'

    # 【P2改进】长篇上下文相关性加权裁剪：避免低相关内容膨胀占满 token
    # 仅在 bible_ctx 较长时触发（短篇直接全量注入，无裁剪开销）
    try:
        from context_ranker import ContextRanker, ContextChunk
        _ctx_total_chars = len(bible_ctx)
        # 粗估 token：中文约 2 字/token，超过 4000 token（约 8000 字）才触发裁剪
        if _ctx_total_chars > 8000:
            # 把 ctx_blocks 拆成带标签的 chunks 用于加权排序
            raw_chunks = []
            for d in SMART_DIMENSIONS:
                if d['key'] in ('character_profiles', 'timeline', 'foreshadowing'):
                    continue
                v = (getattr(bb, d['field'], '') or '').strip() if bb else ''
                if v:
                    raw_chunks.append(ContextChunk(
                        dim_key=d['key'], label=d['label'], content=v,
                        priority=ContextRanker.BASE_PRIORITY.get(d['key'], 3)
                    ))
            if char_ctx:
                raw_chunks.append(ContextChunk('character_profiles', '人物档案', char_ctx, priority=1))
            if chapter_plot_ctx:
                raw_chunks.append(ContextChunk('timeline', '本章剧情', chapter_plot_ctx, priority=1))
            if timeline_ctx:
                raw_chunks.append(ContextChunk('timeline', '本卷剧情脉络', timeline_ctx, priority=1))
            if foreshadow_ctx:
                raw_chunks.append(ContextChunk('foreshadowing', '伏笔线索', foreshadow_ctx, priority=3))
            if event_log_ctx:
                raw_chunks.append(ContextChunk('event_log', '前情提要', event_log_ctx, priority=2))
            if dynamic_reports_ctx:
                raw_chunks.append(ContextChunk('dynamic', '近期动态', dynamic_reports_ctx, priority=2))
            if raw_chunks:
                ranker = ContextRanker(max_tokens=4000)
                pov_name_for_rank = pov_name or None
                ranked = ranker.rank_for_chapter(raw_chunks, target_chapter_num, pov_name_for_rank, book_id)
                # 重新组装带 POV 注解的 bible_ctx
                ranked_parts = []
                for c in ranked:
                    if c.dim_key == 'character_profiles':
                        pov_note = f'（本章视点人物：{pov_name}，第三人称有限视角，只写{pov_name}能感知到的事物）' if pov_name else '（第三人称有限视角）'
                        ranked_parts.append(f'【人物档案·视点感知】{pov_note}\n{c.content}')
                    elif c.dim_key == 'timeline' and c.label == '本章剧情':
                        # 本章剧情已写在core_iron之后 chapter_plot_iron 段（位置最高），此处避免重复
                        continue
                    elif c.dim_key == 'timeline':
                        ranked_parts.append(f'【本卷及过往剧情脉络】\n{c.content}')
                    elif c.dim_key == 'foreshadowing':
                        ranked_parts.append(f'【伏笔线索】\n{c.content}')
                    elif c.dim_key == 'event_log':
                        ranked_parts.append(c.content)
                    elif c.dim_key == 'dynamic':
                        ranked_parts.append(f'【近期动态文件（已发生事件摘要）】\n{c.content}')
                    else:
                        ranked_parts.append(f'【{c.label}】\n{c.content}')
                bible_ctx = '\n\n'.join(ranked_parts) or '（暂无设定）'
    except Exception:
        pass  # 裁剪失败时回退到全量注入

    # 防剧透硬约束（伏笔+大纲）
    anti_spoiler_rule = (
        '\n\n【防剧透铁律·第三人称有限视角】'
        '\n1. 严禁在正文中揭示伏笔的谜底/真相，只能呼应POV已察觉的表象线索'
        '\n2. 严禁写出POV不在场时发生的事件（POV不知道的事不能写）'
        '\n3. 大纲/总纲中的未来转折不可在当前章节提前泄露'
        '\n4. 严禁出现POV不认识的人物内心活动（只能通过POV观察推测他人）'
    )

    # 开头提示帧已在函数顶部发出（防连接超时占位），此处不再重复发「正在写第X章」，避免前端出现两条相同提示
    if mode == 'polish':
        # 润色：按章节号定位原文（与前端 displayChapterNum 口径一致：优先标题解析，回退 order_index+1）
        cur = None
        candidates = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        # ① 优先：标题能解析出 target_chapter_num（比如"第3章 觉醒"→3）
        for c in candidates:
            if parse_chapter_number(c.title or '') == target_chapter_num:
                cur = c
                break
        # ② 回退：按 displayChapterNum 兜底（order_index + 1 = target_chapter_num），即 order_index = target_chapter_num - 1
        if not cur:
            cur = next((c for c in candidates if (
                (parse_chapter_number(c.title or '') is None or parse_chapter_number(c.title or '') == 0)
                and c.order_index is not None and c.order_index + 1 == target_chapter_num
            )), None)
        # ③ 最终兜底：直接查 order_index = target_chapter_num - 1
        if not cur:
            cur = Chapter.query.filter_by(book_id=book_id, is_volume=False,
                                           order_index=target_chapter_num - 1).first()
        if not cur or not (cur.content or '').strip():
            yield sse({'type': 'error', 'error': f'第 {target_chapter_num} 章无正文，无法润色'})
            return
        cur_len = len((cur.content or '').strip())
        # 正文阶段·专属规则（只含通用核心+行文规范+文风技能包，屏蔽构思专属规则）
        writing_rules = build_writing_rules(book, skill_pack_ids, extra_style_pack=style_pack_prompt)
        sys_prompt = (
            f'你是资深网文润色编辑。请润色《{book.title}》第 {target_chapter_num} 章正文。'
            f'\n\n{core_iron}'
            f'\n\n{chapter_plot_iron if chapter_plot_iron else ""}'
            f'\n\n{PRE_GENERATE_BAN_RULES}'
            f'\n\n{writing_rules}'
            f'\n要求：保持剧情和人物不变，优化文笔节奏，提升画面感。（⚠️ 【禁令0】任何情形不得写「不是X是Y/更X更Y更Z」修正句式/排比三连）'
            f'\n用户要求：{instruction or "无"}'
            f'\n\n【输出格式】第一行章节标题（如"第{target_chapter_num}章 标题"），第二行空行，第三行起纯正文。'
            f'\n【字数铁律】纯正文（不含标题）2300-2500字，全角中文标点；原文{cur_len}字：不足扩，超了删，区间内保持篇幅。'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【原文】\n{cur.content}'
            f'{anti_spoiler_rule}'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
            f'\n\n直接输出，不要解释，不要附字数统计。'
        )
        user_msg = f'请润色第 {target_chapter_num} 章'
    else:
        # 正文阶段·专属规则（只含通用核心+行文规范+文风技能包，屏蔽构思专属规则）
        writing_rules = build_writing_rules(book, skill_pack_ids, extra_style_pack=style_pack_prompt)
        sys_prompt = (
            f'你是资深网文创作副驾。请为《{book.title}》续写第 {target_chapter_num} 章正文。'
            f'\n\n{core_iron}'
            f'\n\n{chapter_plot_iron if chapter_plot_iron else ""}'
            f'\n\n{PRE_GENERATE_BAN_RULES}'
            f'\n\n{writing_rules}'
            f'\n\n【全文设定参考】\n{bible_ctx}'
            f'\n\n【上一章结尾】\n{prev_chapter_content or "（第一章）"}'
            f'\n用户要求：{instruction or "自然推进剧情"}'
            f'\n\n【输出格式】第一行章节标题（如"第{target_chapter_num}章 标题"），第二行空行，第三行起纯正文。'
            f'\n【字数铁律】纯正文（不含标题）2300-2500字，用全角中文标点；不足扩场景细节/对话停顿/POV感官（温度/气味/触感/视线压力），超了删枝节。（⚠️ 遵守【禁令0】不得凑字/超标）'
            f'{anti_spoiler_rule}'
            f'\n\n{PLAIN_TEXT_LAYOUT_RULES}'
            f'\n\n直接输出，不要解释，不要附字数统计。'
        )
        user_msg = f'请续写第 {target_chapter_num} 章'

    messages = [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': user_msg}]
    content = ''
    try:
        # 写正文 max_tokens 按模型能力"不限"：给足 _DIM_MAX_TOKENS（旧硬编码 4096 ≈ 2000
        # 中文字即截断，正是字数铁律反复触发修正的元凶），网关按已知/自学习上限钳制
        for chunk in gw_stream_with_hb(gw, messages, temperature=0.85, max_tokens=_DIM_MAX_TOKENS):
            if chunk is HEARTBEAT:
                yield SSE_HEARTBEAT_COMMENT
                continue
            if _is_stream_retry(chunk):
                yield sse({'type': 'meta', 'kind': 'stream_retry', 'info': chunk.info})
                continue
            content += chunk
            yield sse({'type': 'delta', 'content': chunk})
    except GeneratorExit:
        # 客户端断开（锁屏/切后台/网络切换）：同步抢救已生成的半章正文再退出
        _save_partial_on_disconnect(session, f'{mode_label}第 {target_chapter_num or "?"} 章',
                                    instruction or '', content)
        raise
    except Exception as e:
        yield sse({'type': 'error', 'error': f'{mode_label}失败：{e}'})
        return

    # 剥离标题行：card.content 只存纯正文，card.title 用 AI 生成的章节名（去 # 标记）
    # 先执行一次平台级纯文本清理（去 * 和 #），再剥离标题行
    content = _clean_text_to_plain(content)
    extracted_title, body_content = _strip_chapter_title(
        content, fallback_title=f'第{target_chapter_num}章')

    # 【字数铁律】用 count_words 校验纯正文字数（与章节保存/列表显示口径一致）
    # AI 自数字数往往偏高（把半角标点/空白也算进去），实际 count_words 常偏低约 200 字
    # 不在 2300-2500 区间则调 _ensure_word_count 重写补正
    from app import count_words, _ensure_word_count
    draft_wc = count_words(body_content)
    if (draft_wc < 2300 or draft_wc > 2500) and api_key and base_url and model:
        yield sse({'type': 'delta', 'content': f'\n\n[字数校验] 初稿 {draft_wc} 字，正在修正至 2400±100…'})
        # _ensure_word_count 内部是 requests.post 同步阻塞（非流式），可能 TTFT>30s 触发 Render idle timeout
        # → 用 _run_blocking_with_heartbeat 包一下，后台线程跑阻塞函数，主 generator 每 10s yield 1 帧心跳
        yield SSE_HEARTBEAT_COMMENT  # 先打 1 帧，占住连接

        def _wc_blocking_call():
            return _ensure_word_count(
                body_content, api_key=api_key, base_url=base_url,
                model=model, max_tokens=_DIM_MAX_TOKENS, chapter_num=target_chapter_num,
                count_fn=count_words)

        _wc_result = yield from _run_blocking_with_heartbeat(
            _wc_blocking_call, sse,
            extra_frames=[sse({'type': 'delta', 'content': '…'})])
        corrected, wc_note = _wc_result if _wc_result is not None else (None, None)
        if corrected and corrected.strip() and count_words(corrected) != draft_wc:
            # 修正后字数更接近目标，采用修正版（再剥一次标题防御 + 纯文本清理）
            corrected = _clean_text_to_plain(corrected)
            _, body_content = _strip_chapter_title(
                corrected, fallback_title=extracted_title)
            final_wc = count_words(body_content)
            yield sse({'type': 'delta', 'content': f'\n[字数校验] 已修正至 {final_wc} 字。'})
        elif wc_note:
            yield sse({'type': 'delta', 'content': f'\n[字数校验] {wc_note}'})

    card = {
        'id': str(uuid.uuid4())[:8],
        'type': 'SAVE_CHAPTER',
        'title': extracted_title,
        'content': body_content,
        'target': '章节正文',
    }
    yield sse({'type': 'card', 'card': card, 'session_id': session.id})

    # 持久化会话（用已发出的卡片对象，保留 id 与前端一致，采纳状态可回写）
    yield from _persist_action_session(session, f'{mode_label}第{target_chapter_num}章：{instruction or ""}',
                                       {f'chapter_{target_chapter_num}': body_content},
                                       [f'chapter_{target_chapter_num}'],
                                       cards_out=[card])


def _persist_action_session(session, title, generated, dims, cards_out=None):
    """动作执行完后持久化会话消息。

    cards_out: 动作函数已生成并发给前端的卡片对象列表（含 id/title/type/content/target）。
               若提供，直接用这些卡片持久化（保留 id，与前端一致，确保采纳状态可回写）；
               若不提供，回退到旧逻辑（按 dims 从 generated 取内容，重新生成 id）。
    """
    from app import db
    history = load_session_messages(session)
    history.append({'role': 'user', 'content': title})
    # 优先用动作函数已发出的卡片对象（id 与前端一致，采纳/编辑/忽略状态可回写）
    if cards_out:
        cards = [{**c, 'status': 'pending'} for c in cards_out if c]
    else:
        # 兼容旧调用：按 dims 从 generated 取内容，重新生成 id（不推荐，id 会与前端不一致）
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
    # session.title 先改好，_safe_save_session_messages 的断连重试分支会同步它
    if not session.title or session.title == 'AI动作':
        session.title = title[:30]
    _safe_save_session_messages(session, history)
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
# 纯文字排版统一约束：平台所有生成内容（正文/大纲/设定/人物/卡片内容）
# 一律去除 Markdown 符号 * 和 #，保留中文数字+顿号/句号/空格/空行排版
# ============================================================================

# -------------------------------------------------------------------------
# 剧情时间线（timeline）维度专用：输出是 JSON 数组，但 main_plot / core_conflict /
# ending_hook / nodes[].title / nodes[].summary / nodes[].cool_type 等字段都是
# 面向读者的自然语言文本，同样必须遵守叙事工艺铁律。格式与排版约束做了 JSON 兼容
# 改写，避免影响数组语法合法。
# -------------------------------------------------------------------------
TIMELINE_NARRATIVE_RULES = ("""
【叙事工艺铁律·剧情维度专用·JSON 字段文本必须遵守】
你输出的是按卷 JSON 数组，但以下所有自然语言文本字段同样要遵守叙事工艺铁律：
  - volume 卷名
  - summary 每卷总体剧情概要（覆盖整卷，150-250字）
  - main_plot 本卷主线剧情（卷内主线推进路径，100-160字）
  - core_conflict 核心冲突
  - ending_hook 卷尾钩子
  - main_events[].title 主要剧情事件标题
  - main_events[].summary 主要剧情事件概要
  - main_events[].bury / main_events[].payoff（伏笔埋收）
  - nodes[].title 节点标题
  - nodes[].summary 节点概要
  - nodes[].cool_type 爽感类型

以上字段文本内容必须遵守：

【0. 总则】
· 写事为主，景一笔带过；多用对话推进，动作与神态紧跟对话；叙述句以逗号长句为主（20-35字串1-2个动作），短句只做重拍。
· 爽点直白清晰，深层动机让读者脑补；转折来自已埋伏笔或人物动机，禁天降巧合。
· 核心口诀：行动往上浮，动机往下潜；先让读者爽，再让他细思极恐。

【1. 冰山与结构】
· 水上1/8（情节/爽点/打脸/升级/赚钱）清晰直白，直接喂给读者；
· 水下7/8（动机/伏笔/创伤/执念/世界观深层）让读者能脑补，不写成设定说明书。
· 每卷 ending_hook 必须是动态悬念/冲突/转折，禁抒情总结升华。
· main_events[] 中的主要剧情事件必须能被后续章节回收（契诃夫之枪原则）。
· 节点设计（nodes[]，由用户点「节点设计」生成）要承接对应 main_event，不能脱离主线。
· nodes[].summary 是"事件推进梗概"不是正文草稿：只写 起因→关键动作→直接后果→收尾→钩子，各一句、动词+名词为主；禁止环境/物象描写、比喻/拟人/排比、动作细节链、形容词混砌、心理/情绪铺陈。每个节点是剧情调度卡（时间/地点/事件/冲突/出场人物/钩子/伏笔），不承担文字润色。

【2. 人物】
· summary / main_plot / main_events[].summary / nodes[].summary 中禁止贴"冷酷/温柔/腹黑"这类标签，须用反常行为刻画；
· 水面行为下埋可脑补动机；背景只露一角；人物有瑕疵/纠结/口是心非；禁完美人设。

【3. 去 AI 味】
· 禁信号词：这说明/这意味着/由此可见/换句话说/事实上/显然/本质上/归根结底/不得不说/毋庸置疑/因此/然而/与此同时/总而言之/综上所述；
· 禁工整句式：多段排比三连/段尾总总结升华/路标词密集/观点句+解释+段尾总结模板；
· 禁典型 AI 短语：一股杀气/一抹笑意/不由得/不禁/随即/与此同时/缓缓/淡淡/微微/眼中闪过一丝/心中暗想/心念电转/恍然大悟/面无表情/淡漠/眸子/嘴角微微上扬/如同/宛如/犹如/周身/威压/那道身影/话音未落/当即/顿时；
· 推荐口语化表达：合着/整半天/好家伙/说白了/得了吧/啥情况/搁这/没跑了/差不离/差不多得了/说实话；
· summary / main_plot / ending_hook / 事件概要 / 节点概要结尾停在动态动作或悬念，禁总结升华句；
· 爽感类型（cool_type）用精确分类名（实力碾压/智商碾压/扮猪吃虎/打脸装逼/信息差爽感/情感爆发/悬念反转…），不说空话。

【4. 伏笔埋收标注铁律（绝对要填）】
· 主要剧情事件（main_events）和节点（nodes）都要明确标注：哪里埋了什么伏笔、后面哪一卷/哪一章回收。
· 埋伏笔字段：bury = "第XX章（或第X卷前期/中期/后期）埋下：XXX；预计回收：第YY章（第Z卷）"
· 回收伏笔字段：payoff = "第XX章回收：前文第YY章埋下的XXX；效果：XXX"
· 若无埋/收，字段留空字符串，但绝不乱填。
· 卷与卷的连贯：第N卷最后一个 main_event 的 payoff 允许关联第N+1卷的 bury 或第N-1卷伏笔回收。

【5. JSON 兼容排版约束】
· 为保持 JSON 语法合法，所有字符串值内：
  1）绝对禁止出现未转义的反斜杠 \\ ；
  2）绝对禁止出现未转义的双引号 " ；
  3）绝对禁止 Markdown 符号 * 、 # 、 行首 - 、 > 引用、 ``` 代码块；
  4）列表/条目用"一、二、三、"或"1）2）3）"或"其一其二"，不要 1. 2. 3. 编号；
  5）强调用书名号《》或中文引号，不要 **加粗** 不要 *斜体* 。
· 直接写干净中文短句，段落感用中文标点自然体现。

【6. 自检清单】
· 设定一致：人物行为/性格/语言与大纲一致；势力数量/分布/关系一致；战力不超设定；物品/技能不超前；关系转变有铺垫。
· 卷间连贯：第N卷 ending_hook 与第N+1卷开头严格衔接；各卷 main_events 连续编号不重叠；卷间伏笔埋收跨卷对应。
· AI 味特征：总结升华/排比抒情/比喻/拟人每千字超过3处或命中 8 大 AI 套话词（宛如/犹如/恍若/宛若 + 大海/巨龙/深渊/星河）/评价旁白/对称结构/三连排/解释性叙述/**解释性排比「不是A是B」三连**（⚠️ 自我修正型"不是X，准确说是Y"保留） → 即砍。
· 主要剧情事件：每个 main_event 的 title+summary 必须是一个明确的、可用约5章展开的事件推进（10个≈支撑50章12万字），不是空话。
""").strip()

# 构思阶段专用：输出格式约束（剧情维度的 JSON 兼容、伏笔标注铁律等）
CONCEPTION_EXTRA_RULES = TIMELINE_NARRATIVE_RULES

PLAIN_TEXT_LAYOUT_RULES = """
【纯文字排版铁律·平台级约束·所有输出必须遵守】
（本条对正文、大纲、设定、人物、世界观、伏笔等所有内容生效；Action Card 内的卡片标题和卡片内容也必须遵守）

一、平台级排版约束（只负责排版格式，不包含任何写作规则/行文规范——那些由各阶段专属 build_*_rules() 精准注入：构思=通用核心+构思格式，正文=通用核心+行文规范，去AI=通用核心+去AI手册，绝不一股脑全加载）

1. 绝对禁止任何 Markdown 标记符号，包括：
   一）禁止 # 开头的标题（不要写 # 标题、## 二级标题这类形式）
   二）禁止 * 作为强调/列表/斜体/粗体（不要写 *xxx*、**xxx**、行首 * 列表）
   三）禁止行首 - 短横线列表（" - xxx" / "- xxx" 都不允许）
   四）禁止行首 > 引用块
   五）禁止 ``` 代码块
   六）禁止用 1. / 2. / (1) 这类编号列表符号
   七）绝对禁止输出 JSON 数组或 JSON 对象——不要出现 [ ] { } " " : , 等英文符号，
       不要出现 name identity summary volume main_events 之类的英文键名（本条对大纲、设定、人物、
       世界观、伏笔、地点、文风等全部维度生效；唯一例外：剧情线维度被明确要求按卷 JSON 输出）
2. 正确的纯文字排版形式：
   一）分节标题：直接写成“第一幕：XXX”“本卷目标”“第3卷·XX卷”“主角名”等，前后各空一行即可（不要加#、不要加*）
   二）条目列表：用“一、二、三、…”“1）2）3）…”“甲、乙、丙…”或中文顿号直接并列，缩进用空格，禁止用 - 或 * 或 1. 开头
   三）强调/专有名词：直接用书名号《》、引号“”或不加符号即可，不要用 **粗体** 或 *斜体*
3. 章节正文专属：只输出自然叙述，段落直接用换行/空行分隔。除对话中的正常标点外，正文内容里也不能出现 * 和 # 符号本身。
4. 检查自检：你输出的完整文本中如果出现了独立的 * 字符（除正常数学乘号含义外，极少用到）或行首 #，一律删掉或改成等价中文形式再输出。
""".strip()


# 拒答/客套模板关键词：如果内容主要由这些构成（即使有几百字），也算"有效为空"，应当作 EMPTY_OUTPUT 触发重试
_REFUSAL_OR_FLUFF_PATTERNS = [
    re.compile(r'(?:作(?:为|成).{0,6}AI|我.{0,6}(?:无法|不能|抱歉|抱歉.{0,4}无法|做不到))', re.S),
    re.compile(r'(?:好的|没问题|收到|明白|了解|好哒|好嘞|OK)[，。！,.!\s]*$', re.S),
    re.compile(r'(?:我来(?:帮你|为你|给你)|我会(?:帮你|为你|给你)|下面我(?:将|会|来))[^。！\n]{0,30}$', re.S),
]
# 合理客套最大长度：如果内容 <= 这个长度 且 主要是客套/道歉，则视为空
_FLUFF_MAX_LEN = 60


def _strip_think_tags(text: str) -> str:
    """统一剥离推理模型的 <think>...</think> 标签。
    很多深度推理模型（R1 系列）会先吐出一大段 <think> 内省文字，占满 max_tokens 后正文还没开始，
    结果就是 raw_joined 很长但实际全是 think，清理后内容为空 → 触发 EMPTY_OUTPUT。
    这里在所有内容清理前先把 think 整块剥离，让正文真正进入后续流程。
    """
    if not text:
        return ''
    s = text
    # 标准配对 <think>...</think>（允许跨行、贪婪匹配到最后一个闭合）
    s = re.sub(r'<think[^>]*>[\s\S]*?</think>', '', s, flags=re.IGNORECASE)
    # 兼容未闭合的 <think ...>（开到文尾）
    s = re.sub(r'<think[^>]*>[\s\S]*$', '', s, flags=re.IGNORECASE)
    # 兼容 </think> 残留闭合标签（有闭合没开始）
    s = re.sub(r'</think[^>]*>', '', s, flags=re.IGNORECASE)
    return s


def _is_refusal_or_fluff(text: str) -> bool:
    """判断内容是否是"拒答/客套道歉/承诺开头但没实质内容"。
    这是模型实际"吐字了但等于没吐"的一大类，是 EMPTY_OUTPUT 的隐性来源。
    """
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    for p in _REFUSAL_OR_FLUFF_PATTERNS:
        if p.search(s) and len(s) <= _FLUFF_MAX_LEN:
            return True
    return False


def _clean_patches(raw):
    """清洗 LLM 输出的 patches —— 只保留含非空 original/rewritten 的对，防注入无效项。

    patches: [{ original, rewritten }] —— 落地时对现有维度字段做精准局部替换，
    未命中原文的项被忽略（不会退化成整字段覆盖），从而保住未改动部分，避免"采纳后内容不全"。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        original = (p.get('original') or '').strip()
        rewritten = (p.get('rewritten') or '').strip()
        if not original or not rewritten or original == rewritten:
            continue
        out.append({'original': original, 'rewritten': rewritten})
    return out


def _apply_patches_to_text(text: str, patches: list) -> tuple:
    """对一个维度的当前文本做精准局部替换。

    返回 (new_text, applied_count)。逐条对当前有效文本 replace(original→rewritten, 1)；
    任意 original 未命中 → 跳过该项，绝不整字段覆盖。保证未改动内容 100% 保留。
    """
    if not patches:
        return text, 0
    cur = text
    applied = 0
    for p in patches:
        original = p.get('original')
        rewritten = p.get('rewritten')
        if original and original in cur:
            cur = cur.replace(original, rewritten, 1)
            applied += 1
    return cur, applied


def _clean_text_to_plain(text: str) -> str:
    """统一后处理：移除 Markdown (#、##、###, **xxx**, *xxx*, 行首 *, 行首 -, 代码块 ```，数字. 列表前缀)
    同时保留中文顿号数字+顿号排版（一、二、三、1）2））。输出排版好看的纯文字。
    注意：只做无损清理（如 # 标题 改成 标题；行首 - xxx 改成 　　xxx；**加粗** 去掉加粗符；``` 代码块去掉包裹）。
    """
    if not text:
        return ''
    s = _strip_think_tags(text)  # 先去 think 标签（推理模型常见前置垃圾）
    # 1) 三重反代码块围栏（整行 ``` 或 ```lang）
    s = re.sub(r'^```[a-zA-Z0-9_\-]*\s*$', '', s, flags=re.M)
    # 2) 行首 #/##/### + 空格 → 改成原标题文本（前置空一行 + 标题）
    s = re.sub(r'^#{1,6}\s+', '', s, flags=re.M)
    # 3) **粗体** / *斜体* —— 去掉星号保留原文（贪婪匹配，多行安全）
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s, flags=re.S)
    s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', s, flags=re.S)
    # 3.1) 移除孤立的 * 字符（Markdown 残留）：所有未被上文成对匹配消耗掉的 *
    #       —— 只保留极少量确实有语义的 "*123" 这种模式（中文场景极少），其余全部直接去掉
    #       中文创作环境下基本不会出现合法 *，用户明确要求 * 和 # 都不要，因此整体安全移除
    s = s.replace('*', '')
    # 3.2) 移除孤立 # 字符：所有行内未在单词中的 #，以及行首 # 全部去掉
    s = s.replace('#', '')
    # 4) 行首 - / * / + 无序列表：替换成两个中文全角空格（缩进保留"列表感"，不丢内容）
    #    —— 也匹配"先有若干全角/半角空格 + 短横"的情形（case5 带缩进的子列表）
    s = re.sub(r'^([ \t\u3000]*)[-*+][ \t]+', lambda m: (m.group(1) or '') + '　　', s, flags=re.M)
    # 5) 行首 > 引用前缀去掉
    s = re.sub(r'^[ \t]*>[ \t]?', '', s, flags=re.M)
    # 6) 行首 "1. " / "2) " / "(1) " 这类编号列表转成全角空格 + 同内容（保留序号但避免半角点列表符号）
    #    - 允许一、二、… 这种中文数字+顿号原样保留（不动）
    s = re.sub(r'^[ \t]*(\d+)\.[ \t]+', lambda m: '　　' + m.group(1) + '、', s, flags=re.M)
    s = re.sub(r'^[ \t]*\((\d+)\)[ \t]+', lambda m: '　　(' + m.group(1) + ') ', s, flags=re.M)
    s = re.sub(r'^[ \t]*(\d+)\)[ \t]+', lambda m: '　　' + m.group(1) + '）', s, flags=re.M)
    # 7) 连续空行最多保留两行
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _downgrade_prompt_for_retry(messages, keep_dim=None):
    """【空内容自动重试】用户反馈经常 EMPTY_OUTPUT，很常见一个根因是：
    首调 messages 过长（铁律+上下文+N条历史）→ 模型 tokens 超限或因 system prompt 过长拒答 →
    直接吐空内容 / 只有 think tokens / 前置空白 / 道歉客套话。
    这里做一个"激进降级精简"：
    - 保留首条 system 但砍到 <= 1800 字（更狠，首条都精简）
    - **末尾追加强制指令**：不要道歉、不要客套、不要 think 标签、直接输出正文
    - 保留最后 6 条对话（retry 场景 last 2 必须带），中间历史全部扔掉
    """
    if not messages:
        return messages
    try:
        msgs = list(messages)
        max_system_chars = 1800
        processed = []
        # 尾部强制指令（重试时才加，首调不加 —— 否则相当于告诉模型你会吐空）
        _force_block = """
————————————————
【重试·强制输出铁律·违反即失败】
1. 绝对禁止道歉/客套/解释：不要说"好的/抱歉/我来帮你/作为AI"这类废话
2. 绝对禁止输出 <think> 标签或任何推理过程
3. 绝对禁止空答：哪怕内容不够完美，也必须输出实质性的创作内容
4. 直接开始输出正文，不要任何前置语，不要总结
————————————————""".strip()
        for i, m in enumerate(msgs):
            if i == 0 and isinstance(m, dict) and m.get('role') == 'system':
                s = m.get('content') or ''
                if len(s) > max_system_chars:
                    s = s[:max_system_chars] + '\n……（中间过长铁律已精简，直接输出有效内容即可）'
                if keep_dim:
                    s += f'\n当前维度：{keep_dim}。'
                s += '\n\n' + _force_block
                processed.append({'role': 'system', 'content': s})
                continue
            processed.append(m)
        # 保留最后 6 条（如果是重试场景，最后两条是 assistant_old + user_retry_hint，必须带）
        if len(processed) > 8:
            processed = [processed[0]] + processed[-6:]
        return processed
    except Exception:
        return messages


# ============================================================================
# AI 智驾：四Tab（设定/正文/去AI/校审）统一接口
# 整合原 AI副驾 + AI总创作 + 章节AI创作 能力，统一入口
# ============================================================================

# 维度定义：用户可见的9个维度子按钮（设定Tab下）
SMART_DIMENSIONS = [
    # mode 说明（维度生成模式）：
    #   suggest = 方向性选择维度：smart_suggest 生成 3-5 个差异化方案卡供作者多选一
    #             （仅构思=方向源头 / 文风=口味偏好 出多方案；大纲原为 suggest，
    #              但走到大纲时构思/设定已锁方向，多方案与已定上游不搭边=胡乱创作，
    #              用户明确要求大纲不出多方案 → 已改 direct）
    #   direct  = 执行性展开维度：上游必填依赖已完善时不再出多方案（伪选择：上游已锁方向，
    #             多方案只会诱导 LLM 各换一套体系，选定后与已定构思打架=拼凑感根源），
    #             直接基于锁定上游生成 + 不满意整体重生成（reroll）；
    #             依赖未完善时保留多方案作为探索模式兜底
    {'key': 'concept',            'label': '构思',       'field': 'concept',            'card': 'SAVE_CONCEPT',      'icon': '💡', 'hint': '一句话讲清故事核：主角是谁、要什么、最大的阻碍', 'mode': 'suggest'},
    {'key': 'key_rules',          'label': '设定',       'field': 'key_rules',          'card': 'SAVE_RULE',         'icon': '⚙️', 'hint': '能力体系/修炼体系/科技树，硬规则（构思已定金手指方向时直接生成）', 'mode': 'direct'},
    {'key': 'worldbuilding',      'label': '世界观',     'field': 'worldbuilding',      'card': 'SAVE_WORLDSETTING', 'icon': '🌍', 'hint': '故事发生的世界，独特规则或设定（生成中会提取世界地图架构到“地图”维度）', 'mode': 'direct'},
    {'key': 'plot_design',        'label': '大纲',       'field': 'plot_design',        'card': 'SAVE_OUTLINE_NODE', 'icon': '📋', 'hint': '主线走向，五幕式总纲（构思已定故事核、卷数已锁定时直接生成）', 'mode': 'direct'},
    {'key': 'character_profiles', 'label': '人物',       'field': 'character_profiles', 'card': 'SAVE_CHARACTER',    'icon': '👤', 'hint': '主角和核心配角的动机、性格、关系网（大纲已定各卷人物方向时直接生成）', 'mode': 'direct'},
    {'key': 'timeline',           'label': '剧情',       'field': 'timeline',           'card': 'SAVE_PLOT',         'icon': '📖', 'hint': '关键剧情节点的时间顺序（大纲已定每卷目标时直接生成）', 'mode': 'direct'},
    {'key': 'foreshadowing',      'label': '伏笔',       'field': 'foreshadowing',      'card': 'SAVE_FORESHADOW',   'icon': '🔮', 'hint': '长线伏笔的埋设与回收计划（基于大纲/剧情派生，直接生成）', 'mode': 'direct'},
    {'key': 'locations',          'label': '地图',       'field': 'locations',          'card': 'SAVE_LOCATION',     'icon': '🗺️', 'hint': '故事中的地点、势力分布、世界地图架构（基于世界观派生，直接生成）', 'mode': 'direct'},
    {'key': 'style_guide',        'label': '文风',       'field': 'style_guide',        'card': 'APPLY_STYLE',       'icon': '🎨', 'hint': '叙事风格、语言调性、节奏把控', 'mode': 'suggest'},
]

# 通用聊天：不属于任何维度，自由讨论小说/剧情分析，通过触发关键词填入各维度
SMART_GENERAL_KEY = 'general'

_DIM_KEY_TO_SPEC = {d['key']: d for d in SMART_DIMENSIONS}

# ============================================================================
# 维度依赖图（P1 改进）：某维度生成前，建议/要求先完善哪些前置维度
# required:   未完善时阻断生成（强依赖，违反会导致下游维度质量崩溃）
# recommended: 未完善时提示但允许生成（软依赖，影响一致性）
# 设计原则：
#   - concept 是所有维度的源头（一句话讲清故事核）
#   - worldbuilding/key_rules 是剧情/人物的设定基础
#   - plot_design（五幕总纲）是 character_profiles（人物）的前置：
#     大纲先锁定各卷功能位与人物方向，人物按大纲锚定设计（角色弧线挂靠五幕节点），
#     反过来"先人物后大纲"会为人设硬造剧情=拼凑感根源之一
#   - plot_design（五幕总纲）是 timeline（分卷剧情）的前置
#   - timeline（分卷剧情）是 foreshadowing（伏笔回收计划）的前置
#   管道式信息流：构思→设定→世界观→大纲→人物→剧情→伏笔
# ============================================================================
DIMENSION_DEPENDENCIES = {
    'concept':            {'required': [], 'recommended': []},
    'key_rules':          {'required': ['concept'], 'recommended': []},
    'worldbuilding':      {'required': ['concept'], 'recommended': ['key_rules']},
    'plot_design':        {'required': ['concept'], 'recommended': ['worldbuilding', 'key_rules']},
    'timeline':           {'required': ['plot_design'], 'recommended': ['character_profiles', 'worldbuilding']},
    'character_profiles': {'required': ['concept', 'plot_design'], 'recommended': ['worldbuilding']},
    'foreshadowing':      {'required': ['plot_design'], 'recommended': ['timeline']},
    'locations':          {'required': ['worldbuilding'], 'recommended': []},
    'style_guide':        {'required': [], 'recommended': ['concept']},
}


def _is_dim_filled(bb, dim_key: str) -> bool:
    """判断指定维度是否已完善（达到可用阈值）"""
    spec = _DIM_KEY_TO_SPEC.get(dim_key)
    if not spec or not bb:
        return False
    val = (getattr(bb, spec['field'], '') or '').strip()
    if not val:
        return False
    # 各维度的"可用阈值"字数下限（上游分节铁律有明确要求，这里做粗筛）
    # 构思/设定/世界观/地图/伏笔/文风/大纲 这些维度不能再 50 字就放行
    dim_floor = {
        'concept': 1200,
        'key_rules': 1500,
        'worldbuilding': 2000,
        'locations': 1200,
        'foreshadowing': 1000,
        'style_guide': 1000,
        'plot_design': 1500,
    }
    floor = dim_floor.get(dim_key)
    if floor:
        return len(val) >= floor
    # timeline 是 JSON 数组：至少 1 卷且每卷有 main_plot（不做字数下限直接用结构校验）
    if dim_key == 'timeline':
        try:
            vols = json.loads(val)
            return isinstance(vols, list) and len(vols) > 0 and \
                   all(isinstance(v, dict) and (v.get('main_plot') or '').strip() for v in vols)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    # character_profiles 可能是 JSON 数组或纯文本：至少有 1 个人物（JSON 需 1 个对象；纯文本需 >= 200 字，因为现在要求每人字段写满30字×至少4字段）
    if dim_key == 'character_profiles':
        if val.startswith('['):
            try:
                chars = json.loads(val)
                return isinstance(chars, list) and len(chars) > 0 and \
                       any(len(str(c.get('background') or c.get('description') or c.get('identity') or '')) >= 50 for c in chars if isinstance(c, dict))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return len(val) >= 200
    # 其他维度：至少 50 字（兜底）
    return len(val) >= 50


def check_dim_readiness(bb, dim_key: str) -> dict:
    """检查指定维度的前置依赖完善度

    返回:
    {
        'ready': bool,
        'missing_required': ['character_profiles'],
        'missing_recommended': ['worldbuilding'],
        'warning': '生成剧情前建议先完善：人物设定'
    }
    """
    deps = DIMENSION_DEPENDENCIES.get(dim_key, {'required': [], 'recommended': []})
    missing_req = [k for k in deps['required'] if not _is_dim_filled(bb, k)]
    missing_rec = [k for k in deps['recommended'] if not _is_dim_filled(bb, k)]

    warning = ''
    if missing_req:
        labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in missing_req)
        warning = f'生成“{_DIM_KEY_TO_SPEC[dim_key]["label"]}”前必须先完善：{labels}（未完善会导致内容质量严重下降）'
    elif missing_rec:
        labels = '、'.join(_DIM_KEY_TO_SPEC[k]['label'] for k in missing_rec)
        warning = f'建议先完善：{labels}（未完善可能导致内容不一致）'

    return {
        'ready': len(missing_req) == 0,
        'missing_required': missing_req,
        'missing_recommended': missing_rec,
        'warning': warning,
    }



def _build_dim_context(book, bb, dim_key, with_self=True):
    """构建指定维度的上下文：仅注入上游依赖维度（管道式信息流）+ 当前维度已有内容。

    依据 DIMENSION_DEPENDENCIES（与创作流程一致：构思→设定→世界观→大纲→剧情→情节节点）：
    - 只注入当前维度 required + recommended 的上游维度内容，未列入依赖的维度不再全量注入；
    - 下游/无关维度注入会与上游转述互相污染且浪费 token；
    - 依赖维度内容完整注入不截断（避免信息缺失导致设定错乱）。
    """
    target = _DIM_KEY_TO_SPEC.get(dim_key)
    if not target:
        return '', ''
    deps = DIMENSION_DEPENDENCIES.get(dim_key, {'required': [], 'recommended': []})
    relevant = set(deps['required'] + deps['recommended'])
    parts = []
    for d in SMART_DIMENSIONS:
        if d['key'] == dim_key or d['key'] not in relevant:
            continue
        if bb:
            v = (getattr(bb, d['field'], '') or '').strip()
            if v:
                # 【纯文字铁律】JSON 存储维度注入前一律转自然语言（人物/剧情线），防下游模仿 JSON
                if d['key'] == 'character_profiles' and v.startswith('['):
                    v = _character_profiles_to_text(v)
                elif d['key'] == 'timeline' and (v.startswith('[') or v.startswith('{')):
                    v = _json_to_plain_text(v)
                parts.append(f'【{d["label"]}】\n{v}')
    ctx = '\n\n'.join(parts)
    self_content = ''
    if bb and with_self:
        self_content = (getattr(bb, target['field'], '') or '').strip()
        if self_content:
            # 人物维度自身已有内容也转自然语言（timeline 自身保持 JSON，按卷落地需要）
            if dim_key == 'character_profiles' and self_content.startswith('['):
                self_content = _character_profiles_to_text(self_content)
    return ctx, self_content


# 通用聊天：自动根据用户输入定位相关章节原文/维度内容并注入上下文
# 让 AI 不再需要回复"请把资料发我"
# ============================================================================

# 关键词映射 -> 维度 key（含同义词，便于用户自由表达时命中）
_DIM_KEYWORD_MAP = [
    ('concept',            ['构思', '核心构思', '故事核', '核心冲突', '卖点', '一句话', '故事梗概']),
    ('key_rules',          ['设定', '核心规则', '规则', '能力体系', '修炼体系', '科技树', '等级', '境界', '功法', '体系']),
    ('worldbuilding',      ['世界观', '世界设定', '地理', '大陆', '国家', '城邦', '势力', '历史', '世界背景']),
    ('plot_design',        ['大纲', '剧情大纲', '主线', '支线', '五幕', '三幕', '起承转合', '剧情线', '整体大纲']),
    ('timeline',           ['剧情', '时间线', '时间', '年代', '剧情时间', '顺序', '先后', '事件顺序']),
    ('character_profiles', ['人物', '角色', '主角', '配角', '反派', '性格', '外貌', '背景故事', '人物档案', '人物设定', '角色设定']),
    ('foreshadowing',      ['伏笔', '铺垫', '预示', '伏笔回收', '回收伏笔', '埋设', '暗线']),
    ('locations',          ['地图', '地点', '场景', '势力分布', '世界地图', '地理位置', '地名']),
    ('style_guide',        ['文风', '叙事风格', '风格', '调性', '语言风格', '写作风格', '文笔']),
]


def _detect_mentions(user_text, book_id, bb):
    """从用户输入中识别提及的章节（号/标题关键词）和维度。
    返回：{'chapters': [Chapter 对象列表，按命中顺序去重], 'dims': [dim_key 列表，按命中顺序去重]}
    """
    from app import Chapter, parse_chapter_number
    user_text = user_text or ''
    # 1) 章节识别：章号
    hit_chapters = []
    hit_ids = set()
    try:
        all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
        def _ch_num(c):
            n = parse_chapter_number(c.title or '')
            return (0, n) if n is not None else (1, c.order_index)
        all_chs_sorted = sorted(all_chs, key=_ch_num)
        # 章号正则（支持第N章/第N回/Chapter N）
        nums_found = []
        suffix = '章节回卷部篇话集幕折更段讲课夜日年季场'
        for m in re.finditer(r'第\s*([0-9零一二三四五六七八九十百千万亿两〇]+)\s*([' + suffix + r'])', user_text):
            n = _cn_to_int(m.group(1))
            if n is not None and n not in nums_found:
                nums_found.append(n)
        for m in re.finditer(r'(?:chapter|ch|episode|ep)\.?\s*(\d+)', user_text, re.IGNORECASE):
            n = int(m.group(1))
            if n not in nums_found:
                nums_found.append(n)
        for n in nums_found:
            for c in all_chs_sorted:
                cn = parse_chapter_number(c.title or '')
                if cn == n:
                    if c.id not in hit_ids:
                        hit_chapters.append(c)
                        hit_ids.add(c.id)
                    break
        # 章节标题关键词（除章号外的其余片段，若匹配某章标题中包含则命中，最多1章）
        # 先去掉已命中章号的子串，避免重复匹配
        remaining = re.sub(r'第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*[' + suffix + r']', '', user_text)
        remaining = re.sub(r'(?:chapter|ch|episode|ep)\.?\s*\d+', '', remaining, flags=re.IGNORECASE)
        # 提取"XXX章"中章号后面的章节名关键词，长度>=2
        name_match = re.search(r'章\s*([\u4e00-\u9fffA-Za-z0-9]{2,})', user_text)
        if name_match:
            kw = name_match.group(1)
            for c in all_chs_sorted:
                if c.id in hit_ids:
                    continue
                t = (c.title or '').strip()
                # 仅命中不是章号部分的文字
                t_without_num = re.sub(r'^第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*章\s*', '', t)
                if kw and (kw in t or kw in t_without_num):
                    hit_chapters.append(c)
                    hit_ids.add(c.id)
                    break
    except Exception:
        pass

    # 2) 维度识别：关键词命中
    dims = []
    dim_keys_seen = set()
    for dim_key, kws in _DIM_KEYWORD_MAP:
        for kw in kws:
            if kw and kw in user_text:
                if dim_key not in dim_keys_seen:
                    dims.append(dim_key)
                    dim_keys_seen.add(dim_key)
                break
    # 如果提到"设定"但没提"核心规则"单独词，可能用户泛指，保留一次
    # （已在关键词中直接映射为 key_rules，保持一致即可）

    return {'chapters': hit_chapters[:5], 'dims': dims[:5]}


def _cn_to_int(s):
    """中文数字转 int（轻量版，覆盖聊天场景常见范围）。"""
    if not s:
        return None
    if re.fullmatch(r'\d+', s):
        return int(s)
    digit_map = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
    total, section, num = 0, 0, 0
    for ch in s:
        v = digit_map.get(ch)
        if v is None:
            return None
        if v >= 10:
            section = (num or 1) * v
            total += section
            num = 0
            if v >= 10000:
                total = section
                section = 0
        else:
            num = v
    return total + num


def _build_auto_context_block(user_text, book_id, bb):
    """根据用户提及自动构建引用块（章节原文 + 维度内容摘要）。
    返回：(block_str, info_dict) — info_dict 供前端回显命中的章节/维度名。
    """
    mentions = _detect_mentions(user_text, book_id, bb)
    if not mentions['chapters'] and not mentions['dims']:
        return '', {'chapters': [], 'dims': []}

    lines = []
    info_chapters = []
    info_dims = []

    for ch in mentions['chapters']:
        title = ch.title or f'第{ch.order_index}章'
        raw = (ch.content or '').strip()
        # 正文过长（>1500字）截断中段保留首尾，关键信息不丢
        if len(raw) > 1500:
            head = raw[:800]
            tail = raw[-700:]
            snippet = head + '\n…（中间已省略，约' + str(max(0, len(raw) - 1500)) + '字）…\n' + tail
        else:
            snippet = raw or '（章节尚无正文）'
        wc = ch.word_count or len(raw)
        lines.append(f'【引用·章节原文】{title}（{wc}字，已自动从章节表载入，无需作者再发）')
        lines.append(snippet)
        info_chapters.append({'id': ch.id, 'title': title})

    for dim_key in mentions['dims']:
        spec = _DIM_KEY_TO_SPEC.get(dim_key)
        if not spec:
            continue
        label = spec['label']
        raw = ''
        if bb:
            raw = (getattr(bb, spec['field'], '') or '').strip()
            # 【纯文字铁律】JSON 存储维度引用注入前转自然语言（人物/剧情线），防模仿 JSON
            if dim_key == 'character_profiles' and raw.startswith('['):
                raw = _character_profiles_to_text(raw)
            elif dim_key == 'timeline' and (raw.startswith('[') or raw.startswith('{')):
                raw = _json_to_plain_text(raw)
        if raw:
            snippet = raw if len(raw) <= 2500 else (raw[:1800] + '\n…（中间省略）…\n' + raw[-700:])
            lines.append(f'【引用·维度内容】{label}（已从设定库载入，无需作者再发）')
            lines.append(snippet)
        else:
            lines.append(f'【引用·维度内容】{label}（此维度目前还是空白）')
        info_dims.append({'key': dim_key, 'label': label})

    block = '\n\n'.join(lines)
    return block, {'chapters': info_chapters, 'dims': info_dims}


def _character_profiles_to_text(json_str):
    """把 character_profiles JSON 数组转为自然语言文本，避免 AI 模仿 JSON 格式输出。
    输入：[{"name":"主角名","identity":"...","personality":"..."}, ...]
    输出：
      姓名：主角名
      身份：...
      性格：...
      （空行分隔下一个角色）
    """
    try:
        arr = json.loads(json_str)
        if not isinstance(arr, list):
            return json_str
        blocks = []
        for c in arr:
            if not isinstance(c, dict):
                continue
            lines = []
            field_labels = [
                ('name', '姓名'), ('role', '角色'), ('identity', '身份'),
                ('personality', '性格'), ('motivation', '动机'),
                ('background', '背景'), ('relationships', '关系'),
                ('abilities', '能力'), ('cultivation_talent', '修炼天赋'),
                ('realm', '境界'), ('items', '物品'),
            ]
            for f, label in field_labels:
                val = (c.get(f) or '').strip()
                if val:
                    lines.append(f'{label}：{val}')
            if lines:
                blocks.append('\n'.join(lines))
        return '\n\n'.join(blocks) if blocks else json_str
    except Exception:
        return json_str


# 通用 JSON→纯文本转换：已知英文键名映射中文标签（覆盖剧情线卷结构/人物/常见结构）
_JSON_FIELD_LABELS = {
    'volume': '卷名', 'volume_index': '卷序', 'volume_id': '卷标识', 'act': '幕',
    'summary': '概要', 'main_plot': '主线', 'core_conflict': '核心冲突',
    'ending_hook': '卷尾钩子', 'main_events': '主要事件', 'nodes': '情节节点',
    'title': '标题', 'chapters': '对应章节', 'type': '类型',
    'bury': '伏笔埋设', 'payoff': '伏笔回收', 'arc_points': '弧线要点',
    'name': '姓名', 'role': '角色', 'identity': '身份', 'gender': '性别',
    'age': '年龄', 'appearance': '外貌', 'personality': '性格',
    'motivation': '动机', 'background': '背景', 'relationships': '关系',
    'abilities': '能力', 'items': '物品', 'realm': '境界',
    'description': '描述', 'content': '内容', 'goal': '目标', 'key_events': '关键事件',
}

_CN_CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'


def _json_value_to_text(val):
    """递归把 JSON 值转纯中文文本：dict→键值行，list→①②编号块，标量→原样。"""
    if isinstance(val, dict):
        lines = []
        for k, v in val.items():
            label = _JSON_FIELD_LABELS.get(k, k)
            if isinstance(v, (dict, list)):
                lines.append(f'{label}：')
                sub = _json_value_to_text(v)
                if sub.strip():
                    lines.append(sub)
            else:
                sv = str(v).strip()
                if sv:
                    lines.append(f'{label}：{sv}')
        return '\n'.join(lines)
    if isinstance(val, list):
        parts = []
        for i, item in enumerate(val):
            mark = _CN_CIRCLED[i] if i < len(_CN_CIRCLED) else f'第{i + 1}项、'
            body = _json_value_to_text(item)
            if body.strip():
                # 编号拼到首行行首（dict 块也有 ①②…，多事件不混淆）
                first_nl = body.find('\n')
                body = (f'{mark}{body[:first_nl]}\n{body[first_nl + 1:]}'
                        if first_nl > 0 else f'{mark}{body}')
                parts.append(body)
        return '\n\n'.join(parts)
    return str(val).strip()


def _json_to_plain_text(text):
    """把 JSON 数组/对象文本转成纯中文分节文本（timeline 卷结构等通用兜底）。

    用途：LLM 被上游 JSON 污染输出 JSON 结构时（典型：大纲模仿剧情线 JSON），
    转成"卷名：xxx／概要：xxx／主要事件：①…"的纯文字形式；解析失败原样返回。
    """
    if not text:
        return text
    s = text.strip()
    m = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', s)
    if m:
        s = m.group(1).strip()
    if not (s.startswith('[') or s.startswith('{')):
        return text
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError, TypeError):
        return text
    if isinstance(parsed, dict):
        for k in ('volumes', 'data', 'result', 'items', 'list'):
            if isinstance(parsed.get(k), list):
                parsed = parsed[k]
                break
    out = _json_value_to_text(parsed)
    return out if out and out.strip() else text


def _plain_json_fallback(dim_key, content):
    """【纯文字铁律兜底】非 timeline 维度：内容以 [ 或 { 开头时一律转纯文本。

    所有智驾维度输出铁律是纯中文自然语言（timeline 例外，按卷 JSON 落地）。
    人物维度优先用专用转换器（键名映射更全），其余维度走通用转换器。
    """
    if dim_key in ('timeline', 'SAVE_PLOT') or not content:
        return content
    s = content.lstrip()
    m = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', s)
    if m:
        s = m.group(1).strip()
    if not (s.startswith('[') or s.startswith('{')):
        return content
    if dim_key in ('character_profiles', 'SAVE_CHARACTER'):
        out = _character_profiles_to_text(s)  # s 已剥 fence，专用转换器不再自剥
    else:
        out = _json_to_plain_text(content)
    return out if out and out.strip() else content


# ----------------------------------------------------------------------------
# 设定Tab：通用聊天（自由讨论，关键词触发填入维度）
# ----------------------------------------------------------------------------

# 关键词到维度的映射：用户消息中含关键词时，AI 回复可产对应维度的卡片
_GENERAL_KEYWORD_MAP = {
    'concept': ['构思', '故事核', '主线思路', '核心冲突'],
    'key_rules': ['设定', '体系', '规则', '修炼', '能力', '科技树'],
    'worldbuilding': ['世界观', '世界设定', '世界规则'],
    'plot_design': ['大纲', '主线', '剧情走向', '起承转合'],
    'timeline': ['剧情', '时间线', '事件顺序', '剧情节点'],
    'character_profiles': ['人物', '角色', '主角', '配角', '关系'],
    'foreshadowing': ['伏笔', '埋线', '回收'],
    'locations': ['地图', '地点', '势力分布', '地理位置'],
    'style_guide': ['文风', '风格', '语言调性', '叙事'],
}


def _detect_dim_from_text(text):
    """从用户文本中检测涉及的维度关键词，返回 [(dim_key, matched_words)]。"""
    if not text:
        return []
    hits = []
    for dim_key, kws in _GENERAL_KEYWORD_MAP.items():
        matched = [kw for kw in kws if kw in text]
        if matched:
            hits.append((dim_key, matched))
    return hits


# ============================================================================
# 【域模块拆分】（架构门禁 P2a：本文件行数只减不增）
#   blueprints/chat_smart_fix_bp.py     防遗忘报告→修正闭环（4 路由）
#   blueprints/chat_roundtable_bp.py    圆桌会议 + 联网搜索配置（3 路由）
#   blueprints/chat_smart_opt_bp.py     优化建议报告（6 路由）
#   blueprints/chat_smart_gen_bp.py     智能创作生成域核心（3 路由）
#   blueprints/chat_smart_edit_bp.py    维度编辑·批量·去AI·校审域（10 路由）
# 约定：域模块顶部不反向 import 本模块（避免循环导入）；共享符号由下方
# init() 注入其模块全局；路由经 register() 挂到 chat_collab_bp，endpoint
# 与原先装饰器注册完全一致（前端 / 测试零感知）。
# ============================================================================
def _register_split_domains():
    from blueprints import chat_smart_fix_bp as _fix
    _fix.init(
        _apply_patches_to_text=_apply_patches_to_text,
        _character_profiles_to_text=_character_profiles_to_text,
        _clean_patches=_clean_patches,
        build_review_rules=build_review_rules,
        chat_collab_bp=chat_collab_bp,
    )
    _fix.register(chat_collab_bp)

    from blueprints import chat_roundtable_bp as _rt
    _rt.init(
        CARD_REGISTRY=CARD_REGISTRY,
        _DIM_KEY_CARD=_DIM_KEY_CARD,
        _DIM_MAX_TOKENS=_DIM_MAX_TOKENS,
        _RT_CREATE_ALL=_RT_CREATE_ALL,
        _RT_CREATE_DIMS=_RT_CREATE_DIMS,
        _RT_CREATE_FIELD=_RT_CREATE_FIELD,
        _auto_rank_scan_from_nl=_auto_rank_scan_from_nl,
        _auto_sync_params_from_user_message=_auto_sync_params_from_user_message,
        _build_toc_block=_build_toc_block,
        _character_profiles_to_text=_character_profiles_to_text,
        _clean_text_to_plain=_clean_text_to_plain,
        _json_to_plain_text=_json_to_plain_text,
        _plain_json_fallback=_plain_json_fallback,
        _core_params_iron_block=_core_params_iron_block,
        _detect_dim_from_text=_detect_dim_from_text,
        _dim_max_tokens=_dim_max_tokens,
        _enrich_card_rank_meta=_enrich_card_rank_meta,
        _format_rank_context=_format_rank_context,
        _get_latest_chapter_info=_get_latest_chapter_info,
        _get_or_create_session_for_book=_get_or_create_session_for_book,
        _is_rt_continue=_is_rt_continue,
        _rt_create_dimension_system=_rt_create_dimension_system,
        _rt_general_dim_request=_rt_general_dim_request,
        _rt_load_state=_rt_load_state,
        _rt_load_state_by_sid_independent=_rt_load_state_by_sid_independent,
        _rt_parse_create_dims=_rt_parse_create_dims,
        _rt_persist_messages=_rt_persist_messages,
        _rt_save_state=_rt_save_state,
        _rt_stream_turn=_rt_stream_turn,
        build_chat_system_prompt=build_chat_system_prompt,
        chat_collab_bp=chat_collab_bp,
        parse_cards=parse_cards,
        strip_cards=strip_cards,
    )
    _rt.register(chat_collab_bp)

    from blueprints import chat_smart_opt_bp as _opt
    _opt.init(SMART_DIMENSIONS=SMART_DIMENSIONS, chat_collab_bp=chat_collab_bp)
    _opt.register(chat_collab_bp)

    from blueprints import chat_smart_gen_bp as _gen
    _gen.init(
        DIMENSION_DEPENDENCIES=DIMENSION_DEPENDENCIES,
        PLAIN_TEXT_LAYOUT_RULES=PLAIN_TEXT_LAYOUT_RULES,
        TIMELINE_NARRATIVE_RULES=TIMELINE_NARRATIVE_RULES,
        _CARD_TARGET=_CARD_TARGET,
        _DIM_KEY_TO_SPEC=_DIM_KEY_TO_SPEC,
        _DIM_MAX_TOKENS=_DIM_MAX_TOKENS,
        _RE_TV=_RE_TV,
        _auto_sync_params_from_user_message=_auto_sync_params_from_user_message,
        _build_auto_context_block=_build_auto_context_block,
        _build_dim_context=_build_dim_context,
        _build_toc_block=_build_toc_block,
        _character_profiles_to_text=_character_profiles_to_text,
        _clean_text_to_plain=_clean_text_to_plain,
        _core_params_iron_block=_core_params_iron_block,
        _detect_dim_from_text=_detect_dim_from_text,
        _dim_max_tokens=_dim_max_tokens,
        _downgrade_prompt_for_retry=_downgrade_prompt_for_retry,
        _enrich_card_rank_meta=_enrich_card_rank_meta,
        _format_rank_context=_format_rank_context,
        _get_latest_chapter_info=_get_latest_chapter_info,
        _get_or_create_session_for_book=_get_or_create_session_for_book,
        _is_dim_filled=_is_dim_filled,
        _is_refusal_or_fluff=_is_refusal_or_fluff,
        _is_write_chapter_intent=_is_write_chapter_intent,
        _log_validation_issues=_log_validation_issues,
        _plain_json_fallback=_plain_json_fallback,
        _strip_think_tags=_strip_think_tags,
        build_chat_chapter_rules=build_chat_chapter_rules,
        build_chat_system_prompt=build_chat_system_prompt,
        build_conception_rules=build_conception_rules,
        build_context_messages=build_context_messages,
        check_dim_readiness=check_dim_readiness,
        chat_collab_bp=chat_collab_bp,
    )
    _gen.register(chat_collab_bp)

    from blueprints import chat_smart_edit_bp as _edit
    _edit.init(
        PLAIN_TEXT_LAYOUT_RULES=PLAIN_TEXT_LAYOUT_RULES,
        SMART_DIMENSIONS=SMART_DIMENSIONS,
        TIMELINE_NARRATIVE_RULES=TIMELINE_NARRATIVE_RULES,
        _CARD_TARGET=_CARD_TARGET,
        _DIM_KEY_TO_SPEC=_DIM_KEY_TO_SPEC,
        _DIM_MAX_TOKENS=_DIM_MAX_TOKENS,
        _STYLE_PACK_ROOT=_STYLE_PACK_ROOT,
        _build_dim_context=_build_dim_context,
        _character_profiles_to_text=_character_profiles_to_text,
        _clean_text_to_plain=_clean_text_to_plain,
        _core_params_iron_block=_core_params_iron_block,
        _dim_max_tokens=_dim_max_tokens,
        _downgrade_prompt_for_retry=_downgrade_prompt_for_retry,
        _enrich_card_rank_meta=_enrich_card_rank_meta,
        _get_enabled_style_pack=_get_enabled_style_pack,
        _get_latest_chapter_info=_get_latest_chapter_info,
        _get_or_create_session_for_book=_get_or_create_session_for_book,
        _is_refusal_or_fluff=_is_refusal_or_fluff,
        _json_to_plain_text=_json_to_plain_text,
        _load_style_manifest=_load_style_manifest,
        _log_validation_issues=_log_validation_issues,
        _persist_card_status=_persist_card_status,
        _plain_json_fallback=_plain_json_fallback,
        _strip_chapter_title=_strip_chapter_title,
        _strip_think_tags=_strip_think_tags,
        build_conception_rules=build_conception_rules,
        build_review_rules=build_review_rules,
        check_dim_readiness=check_dim_readiness,
        chat_collab_bp=chat_collab_bp,
    )
    _edit.register(chat_collab_bp)


_register_split_domains()
