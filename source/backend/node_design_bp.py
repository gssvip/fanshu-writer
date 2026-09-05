"""节点设计师：为指定卷按 main_event 分段生成情节节点。

背景：节点设计整卷一次性生成（5-15 分钟的长 LLM 推理集合）过去依赖同步/长 SS
会撞 Cloudflare+Render 的双层网关超时（502 / network error）。本模块采用
「分段异步任务 + 渐进轮询」架构（与 app.py 的 ai_outline_volume 同源思路）：

  - POST /api/books/<book_id>/node-design/submit  → 秒回 job_id
      后端守护线程按 main_event 一段一段生成（每段只调 1 次 LLM，服务器内部完成，
      不下行到浏览器 → 永不触发网关超时）。
  - GET  /api/books/<book_id>/node-design/status   → 渐进返回已生成节点
      {state, done, total, nodes}，前端每 ~1.5-2s 轮询，逐段看到节点实时冒出来。
  - POST /api/books/<book_id>/node-design/revise  → 对单个节点提交修改意见，
      异步重生成该节点并回填，结果同样从 status 取。
  - POST /api/books/<book_id>/node-design/apply   → 采纳：把节点按 index 合并进
      book_bible.timeline 对应卷的 nodes（保留卷级 summary/main_plot/main_events 等）。
  - POST /api/books/<book_id>/node-design/cancel  → 取消进行中的任务。

未覆盖 profile 纵深，仅服务“节点设计师”单点职责；与 chat_smart / apply-card 解耦。
"""
from __future__ import annotations
import threading
import time as _time
import uuid as _uuid
import json
import re as _re
import re  # helper 函数中直接写 re.xxx
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

node_design_bp = Blueprint('node_design', __name__)

# ----------------------------------------------------------------------------
# 模块级任务存储（内存）。与 app.py 的 _Jobs 同构：job 创建后由守护线程写状态，
# 主线程（登录校验 + HTTP 响应）读状态。Receiver 冷启动丢态可接受（前端会提示重试）。
# ----------------------------------------------------------------------------
_JOBS: dict = {}
_ACTIVE: dict = {}          # session_id -> job（同一会话同时只跑一个，避免多段抢跑）
_LOCK = threading.Lock()
_GEN: dict = {}             # session_id -> 卷上下文（供 revise 复用；job 完成后保留短期）

_JOB_TTL_SEC = 30 * 60
_LAST_SWEEP = 0.0


def _sweep():
    global _LAST_SWEEP
    now = _time.time()
    if now - _LAST_SWEEP < 120:
        return
    _LAST_SWEEP = now
    dead = [k for k, v in _JOBS.items() if v.get('created', 0) + _JOB_TTL_SEC < now]
    with _LOCK:
        for k in dead:
            _JOBS.pop(k, None)
            _ACTIVE.pop(k, None)


def _safe_err(e):
    """把 Python 异常转成面向用户的可读错误文本。

    重点：KeyError('foo') 的 str 形式是 "'foo'"（含单引号），直接展示用户会摸不着头脑。
    这里把它格式化为「缺少字段: foo」。其他异常按原文本截断输出。
    """
    if isinstance(e, KeyError):
        args = e.args
        key = args[0] if args else ''
        return f'缺少字段: {key}'
    return str(e)[:300]


def _job_factory(**kw):
    return {
        'job_id': _uuid.uuid4().hex,
        'owner': None,
        'state': 'running',
        'created': _time.time(),
        'done': 0,
        'total': 0,
        'current_segment': None,
        'nodes': [],
        'message': '任务已创建，排队等待生成…',
        'error': None,
        'kind': 'start',          # start | revise
        'revise_node_index': None,
        'revise_feedback': '',
        'result': None,           # revise 完成后单节点回填
        # runner 内部键（job['_*']）：在 factory 里预置并初始化为默认值，
        # 彻底避免后台线程 job['_cancel']/job['_state'] 这类 KeyError 裸抛。
        '_cancel': False,
        '_state': None,
        '_revise_node': None,
        **kw,
    }


# ----------------------------------------------------------------------------
# 卷上下文加载（复用 app 的口径）
# ----------------------------------------------------------------------------
def _vol_index(v):
    """从 timeline 卷对象里稳健提取 volume_index。"""
    if not isinstance(v, dict):
        return None
    vi = v.get('volume_index')
    if vi is None:
        for k in ('volume_id', 'volume', 'vol', 'id'):
            val = v.get(k)
            if val is None:
                continue
            s = str(val)
            m = _re.search(r'(\d+)', s)
            if m:
                vi = int(m.group(1))
                break
    try:
        return int(float(vi)) if vi is not None else None
    except (TypeError, ValueError):
        return None


def _load_volume_context(book_id, volume_index, req_data, rv, act_descriptions):
    """从 BookBible.timeline 取出目标卷 + 相关上下文，构建节点生成骨架 state。

    返回 dict；若无法拿到可生成节点的结构化事件则返回 None（并写入 err）。
    """
    from app import db, Book, BookBible, _get_total_volumes, _get_chapters_per_volume
    book = Book.query.get(book_id)
    if not book:
        rv['error'] = '书籍不存在'
        return None
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        rv['error'] = '该作品还没有剧情线（timeline），请先在剧情维度生成卷剧情后再节点设计。'
        return None

    existing_vols = []
    try:
        parsed = json.loads(bb.timeline or '[]')
        if isinstance(parsed, list):
            existing_vols = parsed
    except (json.JSONDecodeError, ValueError, TypeError):
        existing_vols = []

    target = None
    for v in existing_vols:
        if _vol_index(v) == volume_index:
            target = v
            break

    total_volumes = _get_total_volumes(bb, book) or 1
    cpv_raw = _get_chapters_per_volume(bb, book)
    # 12万字/卷口径默认值：单卷目标 50 章 × 2400±100字/章 = 12万字
    cpv = int(cpv_raw or 0)
    if cpv < 1:
        cpv = 50
    if not target:
        volume_title = (req_data.get('volume_title') or '').strip() or f'第{volume_index}卷'
    else:
        volume_title = target.get('volume', target.get('volume_title', f'第{volume_index}卷'))

    # 五幕 + 起始章号（对齐 app.ai_outline_volume 口径）
    act_mapping = {1: '立身', 2: '立足', 3: '立势', 4: '立威', 5: '立命'}
    if total_volumes <= 5:
        current_act = act_mapping.get(int(volume_index), '立身')
    else:
        act_idx = min(5, max(1, int((int(volume_index) / max(1, total_volumes)) * 5) + 1))
        current_act = act_mapping.get(act_idx, '立身')
    start_chapter = max(1, (int(volume_index) - 1) * max(1, cpv) + 1)
    evt_end = start_chapter + max(1, cpv) - 1

    # main_events：优先新结构，退化旧 key_events/turning_points
    me = []
    if target and isinstance(target.get('main_events'), list):
        me = target['main_events']
    if not me:
        for k in ('key_events', 'turning_points'):
            ke = (target or {}).get(k)
            if isinstance(ke, list) and ke:
                me = [{'index': i + 1, 'title': str(x) if not isinstance(x, dict) else (x.get('title') or str(x)),
                       'summary': str(x) if not isinstance(x, dict) else (x.get('summary') or str(x)),
                       'estimated_chapters': max(1, max(1, cpv) // max(1, len(ke))),
                       'characters': '', 'events': '', 'time': '', 'location': '',
                       'realm_change': '', 'age_change': '', 'bury': '', 'payoff': ''}
                      for i, x in enumerate(ke)]
                break

    if not me:
        rv['error'] = ('该卷还没有 main_events / key_events 结构化事件数组，无法按事件分段拆子节点。'
                       '请先点本卷【🔍识别】生成结构化剧情事件后再点「节点设计」。')
        return None

    # 事件章区间分配（对齐 app 的 _evt_alloc）
    evt_alloc = []
    cur_ch = start_chapter
    for _me in me:
        if not isinstance(_me, dict):
            continue
        try:
            ec = int(_me.get('estimated_chapters') or 0)
            if ec <= 0:
                ec = max(1, cpv // max(1, len(me)))
        except (TypeError, ValueError):
            ec = max(1, cpv // max(1, len(me)))
        s = cur_ch
        e = min(evt_end, cur_ch + ec - 1)
        if e < s:
            e = s
        evt_alloc.append({
            'me_index': int(_me.get('index') or 0) or (len(evt_alloc) + 1),
            'title': str(_me.get('title') or ''),
            's': s, 'e': e, 'ec': ec, 'raw': _me,
        })
        cur_ch = e + 1
    if evt_alloc and evt_alloc[-1]['e'] != evt_end:
        evt_alloc[-1]['e'] = evt_end
        evt_alloc[-1]['ec'] = evt_end - evt_alloc[-1]['s'] + 1

    # === 关键修复：将 evt_alloc 中"跨度 > MAX_CH_PER_SUBALLOC（默认 8）"的大事件段自动拆成
    # 多个连续子段，每段子段 <=8 章。LLM 一次稳定输出 8 个节点远比重稳输出 20~30 个容易得多，
    # 从源头消灭"LLM 漏章→后置修复变占位节点"问题。
    MAX_CH_PER_SUBALLOC = 8
    if evt_alloc and MAX_CH_PER_SUBALLOC > 0:
        fine_allocs = []
        for a in evt_alloc:
            span = a['e'] - a['s'] + 1
            if span <= MAX_CH_PER_SUBALLOC:
                fine_allocs.append(dict(a))
                continue
            me_idx = a.get('me_index') or (len(fine_allocs) + 1)
            title = a.get('title') or ''
            raw = a.get('raw') or {}
            cur_s = a['s']
            e_end = a['e']
            seg_i = 0
            while cur_s <= e_end:
                # 子段每段 MAX_CH_PER_SUBALLOC 章，最后一段可能略短
                cur_e = min(e_end, cur_s + MAX_CH_PER_SUBALLOC - 1)
                seg_ec = cur_e - cur_s + 1
                seg_i += 1
                sub_title = title if seg_i == 1 else f'{title}（{seg_i}/{((e_end - a["s"]) // MAX_CH_PER_SUBALLOC) + (1 if (e_end - a["s"]) % MAX_CH_PER_SUBALLOC else 0)}）'
                fine_allocs.append({
                    'me_index': me_idx,
                    'title': sub_title,
                    's': cur_s, 'e': cur_e, 'ec': seg_ec, 'raw': raw,
                    '_is_sub_split': True, '_sub_seg': seg_i,
                })
                cur_s = cur_e + 1
        # 再做一次末段兜底：防止拆分误差导致最终 e != evt_end
        if fine_allocs and fine_allocs[-1]['e'] != evt_end:
            fine_allocs[-1]['e'] = evt_end
            fine_allocs[-1]['ec'] = evt_end - fine_allocs[-1]['s'] + 1
        # 再做一次前置兜底：首段 s 必须 == start_chapter
        if fine_allocs and fine_allocs[0]['s'] != start_chapter:
            fine_allocs[0]['s'] = start_chapter
            fine_allocs[0]['ec'] = fine_allocs[0]['e'] - start_chapter + 1
        evt_alloc = fine_allocs

    # 相邻卷衔接上下文
    prev_vol, next_vol = None, None
    ordered = sorted(existing_vols, key=lambda v: _vol_index(v) or 0)
    for i, v in enumerate(ordered):
        if _vol_index(v) == volume_index:
            prev_vol = ordered[i - 1] if i > 0 else None
            next_vol = ordered[i + 1] if i + 1 < len(ordered) else None
            break
    prev_vol_hook = ((prev_vol or {}).get('ending_hook') or '')
    prev_vol_end_chapter = start_chapter - 1
    prev_vol_summary = ((prev_vol or {}).get('summary') or (prev_vol or {}).get('main_plot') or '')
    target_ending = ((target or {}).get('ending_hook') or (target or {}).get('ending') or (target or {}).get('climax') or '')

    # 已有 nodes（供续生成索引续排 / 修改意见的原文上下文）
    existing_nodes = []
    if target and isinstance(target.get('nodes'), list):
        existing_nodes = target['nodes']
    index_offset = (max([int(n.get('index') or 0) for n in existing_nodes if isinstance(n, dict)] or [0]))

    state = {
        'book_id': book_id,
        'volume_index': int(volume_index),
        'volume_title': volume_title,
        'cpv': max(1, cpv),
        'start_chapter': start_chapter,
        'evt_end': evt_end,
        'current_act': current_act,
        'act_desc': act_descriptions.get(current_act, ''),
        'evt_alloc': evt_alloc,
        'total_segments': len(evt_alloc),
        'me_count': len(evt_alloc),
        'target': target,
        'existing_nodes': existing_nodes,
        'node_index_offset': index_offset,
        'bb': bb,
        # 提示词上下文
        'prev_vol_hook': prev_vol_hook,
        'prev_vol_end_chapter': prev_vol_end_chapter,
        'prev_vol_summary': prev_vol_summary,
        'target_ending': target_ending,
        'existing_timeline': _timeline_brief(existing_vols, volume_index, 4500),
        'master_outline': (bb.plot_design or '')[:3000],
        'worldbuilding_ctx': (bb.worldbuilding or '')[:1500],
        'characters_ctx': (bb.character_profiles or '')[:1500],
        'key_rules_ctx': (bb.key_rules or '')[:1000],
        'title': book.title or '',
        'core_params': _build_core_params_str(bb, book),
    }
    return state


def _timeline_brief(vols, self_index, limit):
    """生成全书已有剧情/节点的一行摘要（供 LLM 衔接用），对齐 app 的 existing_timeline。"""
    lines = []
    for v in vols:
        if not isinstance(v, dict):
            continue
        vi = _vol_index(v)
        if vi is None:
            continue
        name = v.get('volume', v.get('volume_title', f'第{vi}卷'))
        nodes = v.get('nodes') if isinstance(v.get('nodes'), list) else []
        if isinstance(nodes, list) and nodes:
            brief = ' | '.join(f'{n.get("chapters","")}{n.get("title","")}' for n in nodes[:8] if isinstance(n, dict))
            hook = str(v.get('ending_hook') or v.get('ending') or v.get('climax') or '')[:60]
            line = f'· 第{vi}卷“{name}”：节点：{(brief or "（无）")[:220]}；卷尾钩子：{hook or "（无）"}'
            lines.append(line)
        else:
            main = (v.get('main_plot') or v.get('core_goal') or '')[:80]
            hook = str(v.get('ending_hook') or v.get('ending') or v.get('climax') or '')[:60]
            lines.append(f'· 第{vi}卷“{name}”：主线：{main or "（无）"}；卷尾钩子：{hook or "（无）"}')
    return '\n'.join(lines)[:limit]


def _build_core_params_str(bb, book):
    try:
        from app import _build_core_params_block
        return _build_core_params_block(bb, book) or ''
    except Exception:
        return ''


# ----------------------------------------------------------------------------
# 节点 JSON 模板与分段提示词
# ----------------------------------------------------------------------------
_ACT_DESCRIPTIONS = {
    '立身': '主角登场、金手指获得、确立生存基础（1-5%）',
    '立足': '主角站稳脚跟、初露锋芒、建立基本人际网（5-25%）',
    '立势': '主角势力扩张、主要冲突激化、BOSS浮出（25-50%）',
    '立威': '主角与BOSS正面对抗、实力跃升、打脸高潮（50-75%）',
    '立命': '终局决战、伏笔回收、世界观全貌揭示（75-100%）',
}

_COOL_SYSTEM = """【爽点设计系统】小说不能只走主线/打打杀杀，每个情节节点必须内置爽点。
■ 八种爽点类型（cool_type 必须为其中之一）：
  ① 实力碾压爽 ② 信息差爽 ③ 扮猪吃虎爽 ④ 荒诞反差爽 ⑤ 打脸装逼爽 ⑥ 社会认同爽 ⑦ 升级蜕变爽 ⑧ 守护爆发爽
■ 爽点结构（cool_structure）：先抑后扬 / 直接碾压 / 默默装完逼
■ 衬托方式（cool_contrast）：旁人震惊 / 不敢置信 / 事后佩服
■ 爽点层级（cool_level）：微爽 / 小爽 / 中爽 / 大爽
■ 钩子系统（每个节点至少带1个钩子）：身份揭露 / 新危机 / 荒诞反转 / 悬念 / 角色危机 / 能力突破 / 世界异常"""

_NODE_SCHEMA = """{
  "main_event_index": <对应main_event的index>,
  "index": <子节点序号>,
  "title": "节点标题（动宾结构）",
  "chapters": "<起始章号>-<结束章号>",
  "type": "M/C/W/D/F",
  "characters": [{"name": "人物名", "relation": "关系类型(主角/家人/亲友/爱人/盟友/同僚/下属/上司/对手/敌对/路人/中立/陌生人·支持组合如'家人·兄')"}],
  "events": "本节点事件：谁在什么场景做了什么→关键后果（20-40字，动词+名词）",
  "conflict": "本节点冲突：主角vs谁/争什么/卡点（20字内）",
  "time": "本节点时间锚",
  "location": "本节点精确场景地点",
  "realm_change": "本节点结束时境界/根基/金手指变化",
  "age_change": "本节点结束时主角年龄/时程变化",
  "summary": "本节点事件推进梗概（起因→关键动作→直接后果→收尾→钩子，纯动词+名词，禁止环境/比喻/形容词/心理铺陈）",
  "chapter_beats": [{"chapter": 章号, "beat": "本章只推进的这一段剧情（20-35字）"}],
  "resources_gained": ["【钱财】...×N", "【物品】...×N", "【武器法宝】...×N", "【功法能力】...×N", "【其它】..."],
  "resources_used":   ["【钱财】...×N", "【物品】...×N", "【武器法宝】...×N", "【功法能力】...×N", "【其它】..."],
  "total_resources_owned": { "钱财": ["...×N"], "物品": ["...×N"], "武器法宝": ["...×N"], "功法能力": ["...×N"], "其它": ["...×N"] },
  "cool_type": "爽点类型（八选一）",
  "cool_structure": "爽点结构",
  "cool_contrast": "衬托方式",
  "cool_level": "爽点层级",
  "bury": "第XX章埋下：XXX；预计回收：第YY章（第Z卷）/空串",
  "payoff": "第XX章回收：第YY章埋下的XXX/空串",
  "hook": "本节点章尾钩子"
}"""


def _build_system_prompt(st):
    """分段节点生成系统提示词（方案A 章粒度 + 方案C 边界硬约束）。

    - A 章粒度：每个情节子节点 chapters 必须是【单章】形式 X（或等价 X-X），
      必须严格对应且只覆盖那一章，绝不跨多章合并成一个节点。
    - C 边界硬约束：所有节点的 chapters 并集必须 = [alloc.s, alloc.e]，
      无重叠、无越界、无跳章，且本卷首节点为 start_chapter、末节点为 evt_end，
      合计节点数 = cpv = 本卷设定总章数（默认 50 章/卷），单章对应单节点
      才能支撑"每章正文 2400±100 字 × 50 章 = 12 万字/卷"容量需求。
    """
    cpv = int(st.get('cpv') or 50)
    sc = int(st.get('start_chapter') or 1)
    ec = int(st.get('evt_end') or (sc + cpv - 1))
    return f"""你是番茄小说金番作者级别的情节节点设计师。
【容量规模铁律】：本卷目标 {cpv} 章、按每章正文 2400±100 字 = 约 12 万字/卷。
  → 每个"情节子节点（node）"必须且只能对应【1 章】，chapters 必须写成单章号（如 "第{sc}章"的 chapters: {sc} 或 {sc}-{sc}）。
  → 绝不允许把 2 章及以上内容压进 1 个 node 合并写，哪怕内容看起来再短；也不得一章多节点重复覆盖。
  → 最终交付时本卷所有节点 chapters 的并集必须精确等于 [{sc}, {sc+1}, …, {ec}]，合计节点数严格等于 cpv（{cpv} 个），差一个都算失败。
【方案C · 边界硬约束】（本生成任务的门禁级条件）
  1) 章节归属锁死本卷：任何节点 chapters 必须落在 {sc}–{ec} 区间内，不得出现 < {sc}（越到上一卷）或 > {ec}（越到下一卷）。
  2) 无重叠：任意两个节点的 chapters 交集必须为空。
  3) 无跳章：[alloc.s, alloc.e] 内每一章都必须有且只有一个节点覆盖。
  4) 逐 main_event 分段生成时：本 main_event 分配章节 [alloc.s-alloc.e] 内每一章都必须在本轮被覆盖，不得把本事件的章"扔给下个事件"由下个事件生成，也不得抢下个事件的章（尤其最后一章 ec）。
  5) 首末门禁：本卷第一个子节点 chapters 必须从 {sc} 起；本卷最后一个子节点 chapters 必须以 {ec} 终；末节点必须埋下与 ending_hook 对齐的卷尾钩子。
【方案A · 章粒度铁律】：单章 = 单节点。
  节点章节表达：chapters 字段统一写单章正整数或 X-X 等价形式；chapter_beats 只允许长度为 1 数组（就是本章的正文推进节拍），不得写成多章数组。
【输出范围铁律】只允许输出本卷（第{st['volume_index']}卷）内容。禁止在输出中复述/罗列/带入任何其他卷的大纲/节点/剧情概要——仅供你推理衔接参考，绝不写进输出。
【模式说明】本卷已有卷剧情，你的任务是为 user prompt 指定的这一个 main_event 生成子节点。
- 不修改本卷 summary/main_plot/main_events/core_conflict/ending_hook 等卷级字段，只输出 nodes。
- 各子节点之间剧情连贯：上一节点末尾自然衔接到下一节点开头。
- 子节点必须严格归属到本 main_event，不得脱离自创剧情。
{st['cohesion']}
【五幕模型对齐】本卷对应五幕中的“{st['current_act']}”幕：{st['act_desc']}
节点设计必须服务于该幕的核心目标。
{_COOL_SYSTEM}
【节点要素铁律】每个节点只许包含以下结构要素（剧情调度卡，不是正文/抒情）：
time / location / events / conflict / characters(必须带relation关系标签) / resources_gained / resources_used / total_resources_owned(滚动核算：上一章总资源 - 本章消耗 + 本章获得 = 本章总资源) / hook；埋收 bury/payoff 精确到章。
【资源分类口径（5类，必须写清【类别前缀】）】
  · 【钱财】银两/灵石/元石/金币/铜币（例：下品灵石×300）
  · 【物品】丹药/符箓/令牌/钥匙/材料/天材地宝/杂物（例：洗髓丹×5、赤焰铁×12斤）
  · 【武器法宝】兵器/防具/法宝/法器/灵器/命器（例：青锋剑·上品法器×1）
  · 【功法能力】修炼功法/武技/神通/秘法/血脉能力/被动天赋（例：《焚天诀》·天阶下品·初窥门径）
  · 【其它】地位/称号/势力归属/人脉/信息/契约（例：护城军·百夫长、城主密道入口地图）
【资源滚动铁律】resources_gained 列"本章新增获取"的资源；resources_used 列"本章使用/消耗/被抢走/破碎/过期/送人"的资源（已经使用/消耗掉的，必须在本章 total_resources_owned 中消除扣减，禁止既写在 resources_used 又在总资源中原封不动）；total_resources_owned 是截至本章结束主角实际仍拥有的全部资源明细，分 5 类列出，必须严格与前一章滚动（上一章total - 本章used + 本章gained = 本章total），不能凭空跳变。
【人物关系口径】characters 字段强制写 relation 标签，枚举：主角/家人(父/母/兄/弟/姐/妹/子女/配偶)/亲友(朋/友/师/徒)/爱人/盟友/同僚/下属/上司/对手/敌对/路人/中立/陌生人；多关系可用"家人·兄""盟友·同僚"组合。
【负清单】禁止环境物象描写、比喻拟人排比工整对仗、动作细节链与形容词堆砌、心理情绪铺陈；summary 只用动词+名词推进梗概。
【章节匹配检查项】生成后请自行逐条核对：
  ✅ 本事件 [alloc.s-alloc.e] 区间每一章都对应恰好一个 node；总数 = (e - s + 1)
  ✅ 每个 node.chapters 是单章（或 X-X）；未出现跨多章合并
  ✅ 无章号 < alloc.s 或 > alloc.e
  ✅ 无任何章重复覆盖
【伏笔埋收】每个子节点标注 bury（真埋才写）/payoff（真收才写），与所属 main_event 对齐，跨卷 payoff 指明第X卷。
【输出格式】严格输出以下 JSON（不要 markdown 代码块，不要注释），nodes 为数组：
{_NODE_SCHEMA}
【章型配额】M主线50% / C角色10% / W世界观10% / D日常20% / F伏笔10%
【小故事闭环】新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子
【节点容量】因为 1 个 node = 1 章（约 2400±100 字正文），每个 node 的 summary 必须足以撑起整章：至少包含开场场景→核心事件→冲突转折→收尾/钩子 4 段节拍，不能过于简略。"""


def _build_user_prompt(st, alloc, is_first_alloc, is_last_alloc):
    """单个 main_event 的生成用户提示词（一个分段，严格按 1章=1节点生成。）"""
    _me = alloc['raw']
    ec = alloc['e'] - alloc['s'] + 1   # 本事件应交付的章节数 = 节点数
    main_block = (
        f"  · 事件{alloc['me_index']}《{alloc['title']}》\n"
        f"    📚 章区间：第{alloc['s']}章 – 第{alloc['e']}章（共 {ec} 章 → 必须产出【恰好 {ec} 个节点】，1 个节点对应且只对应 1 章）\n"
        f"    📝 概要：{str(_me.get('summary',''))[:300]}\n"
        f"    ·人物：{_me.get('characters','')}\n"
        f"    ·事件：{_me.get('events','')}\n"
        f"    ·时间：{_me.get('time','')}\n"
        f"    ·地点：{_me.get('location','')}\n"
        f"    ·境界：{_me.get('realm_change','')}\n"
        f"    ·年龄：{_me.get('age_change','')}"
        + (f"\n    埋：{_me.get('bury','')}" if _me.get('bury') else '')
        + (f"\n    收：{_me.get('payoff','')}" if _me.get('payoff') else '')
    )
    sc = st.get('start_chapter') or 1
    first_rule = ''
    if is_first_alloc:
        first_rule += f"\n【首段门禁】：本次第一个节点 chapters 必须精确为 {sc}（绝不能是 {sc+1}，否则整卷首章漏节点）。"
        if st.get('prev_vol_hook'):
            first_rule += f"\n【卷间衔接】：第{sc}章必须承接上一卷卷尾钩子：{st['prev_vol_hook']}。"
    last_rule = ''
    if is_last_alloc:
        last_rule += f"\n【末段门禁】：本次最后一个节点 chapters 必须精确为 {st.get('evt_end')}（绝不能提前收尾），且 hook 字段必须与本卷 ending_hook 对齐：{str(st.get('target_ending') or '')[:160]}"
    # 列清单：本事件每一章的期待章号 + 索引位，降低模型"数错章"概率
    ch_list = '、'.join(str(c) for c in range(alloc['s'], alloc['e'] + 1))
    return f"""书名：{st['title']}
{st['core_params']}
【五幕式总纲】（仅供衔接参考）
{st['master_outline'] or '（暂无）'}
【全书已有剧情】（仅供衔接参考，严禁写进输出）
{st['existing_timeline'] or '（暂无）'}
【本卷卷级 6 要素锚】
- 核心人物：{(st['target'] or {}).get('characters','') or '（无）'}
- 时间锚：{(st['target'] or {}).get('timeline_anchor','') or '（无）'}
- 地点路线：{(st['target'] or {}).get('location','') or '（无）'}
- 境界变化：{(st['target'] or {}).get('realm_change','') or '（无）'}
- 年龄变化：{(st['target'] or {}).get('age_change','') or '（无）'}
- 核心冲突：{(st['target'] or {}).get('core_conflict','') or '（无）'}
- 卷尾钩子：{st.get('target_ending') or '（无）'}
【世界观设定】{st['worldbuilding_ctx'] or '（暂无）'}
【核心规则】{st['key_rules_ctx'] or '（暂无）'}
【人物档案】{st['characters_ctx'] or '（暂无）'}

【本次生成的 main_event · A+C 约束清单】{main_block}{first_rule}{last_rule}

✅ 本事件章号清单（必须逐章对应 1 个 node，nodes 数组顺序必须与下面章号顺序一致）：
  章号：{ch_list}
  节点数：必须恰好 {ec} 个（不得多、不得少），第 k 个 node 的 chapters = 第 k 个章号。

【门禁级输出要求】：
  1) nodes.length == {ec}；
  2) nodes[i].chapters 解析后单章号 == 章号清单第 i 项（i 从 0 开始）；
  3) 无任何单章跨多章（chapters 为单数字或 X-X，不得 X-Y 且 X≠Y），无多章合并，无重复章；
  4) 每个 node.summary 至少 80 字，能支撑该章 2400±100 字正文创作。

请严格围绕以上条件输出 JSON。"""


def _build_fillgap_prompt(st, alloc, missing_chs, existing_nodes_in_alloc):
    """大事件主段生成后，若还有 missing_chs（本事件区间内没被 LLM 覆盖的章号），
    用这个 prompt 专向 LLM"点名补漏"：明确按缺失章号清单一个不漏地只输出这些章的节点。
    missing_chs: List[int]，必须是 alloc.s..alloc.e 子集且按升序。
    existing_nodes_in_alloc: 本子段已生成节点（供衔接参考，不重写，避免覆盖已生成内容）。"""
    missing_chs = sorted(set(int(c) for c in missing_chs if alloc['s'] <= int(c) <= alloc['e']))
    if not missing_chs:
        return None, ''
    me_idx = alloc.get('me_index') or 0
    me_title = alloc.get('title') or f'事件{me_idx}'
    ch_list = '、'.join(str(c) for c in missing_chs)
    ec = len(missing_chs)
    ref_nodes_txt = ''
    if existing_nodes_in_alloc:
        try:
            refs = []
            for n in existing_nodes_in_alloc[:4]:
                rng = _parse_chapters_field(n.get('chapters'))
                if not rng: continue
                refs.append(f"  ch{rng[0]} title={str(n.get('title',''))[:30]} 摘要前60字={str(n.get('summary',''))[:60]}")
            if refs:
                ref_nodes_txt = "\n【已完成同事件节点（仅供你衔接，严禁写进输出，严禁覆盖它们）】\n" + "\n".join(refs)
        except Exception:
            ref_nodes_txt = ''
    prompt = f"""书名：{st['title']}
{st.get('core_params','')}
【任务：专属补漏】本 main_event（事件{me_idx}《{me_title}》，章区间第{alloc['s']}-{alloc['e']}章）刚刚已经完成一轮生成，但以下章节仍"一个节点都没有"，必须由你本次只输出这些缺失章的节点，不要再输出其他章。
【缺失章号清单（按正文顺序）】：{ch_list}
  → 共 {ec} 章，nodes.length 必须严格等于 {ec}，第 k 个 node.chapters = 第 k 个缺失章号。
【本卷卷级 6 要素锚】
- 核心人物：{(st.get('target') or {}).get('characters','') or '（无）'}
- 地点路线：{(st.get('target') or {}).get('location','') or '（无）'}
- 境界变化：{(st.get('target') or {}).get('realm_change','') or '（无）'}
- 核心冲突：{(st.get('target') or {}).get('core_conflict','') or '（无）'}
- 卷尾钩子：{st.get('target_ending') or '（无）'}
【五幕对齐】本卷属于"{st.get('current_act','立身')}"幕：{st.get('act_desc','')}
【爽点系统】每个节点要有爽点类型/结构/衬托/层级。{ref_nodes_txt}
【方案A · 章粒度铁律】：缺失章号清单里的每一章 = 1 个独立 node，chapters 写单章号或 X-X，禁止多章合并。
【方案C · 边界硬约束】：任何 node.chapters 必须 ∈ 缺失章号清单，且不得出现 alloc 区间外（<{alloc['s']} 或 >{alloc['e']}）的章。
【容量】每个 node.summary ≥ 80 字，结构要包含：开场→核心事件→冲突→转折→收尾/钩子；这样才能撑起该章 2400±100 字正文。
【输出】严格 JSON：{{"nodes": [ /* 仅列 missing_chs 对应节点，顺序 = missing_chs 升序 */ ]}}，不要 markdown 代码块，不要注释。
"""
    return prompt, ch_list


def _build_revise_prompt(st, node, feedback):
    """修改意见：基于原始节点 + 用户反馈，重生成单个节点。"""
    orig = json.dumps(node, ensure_ascii=False)
    return f"""书名：{st['title']}
{st['core_params']}
【本卷】第{st['volume_index']}卷“{st['volume_title']}”。请只针对下面这一个情节节点做修改，输出仍为单个节点 JSON（结构不变）。
【原节点】
{orig}
【作者的修改意见】
{feedback}

请严格保持原节点的 main_event_index / index / chapters 不变（除非意见明确要求调整章号），按修改意见重写该节点的
title / type / characters / events / conflict / time / location / realm_change / age_change / summary / chapter_beats / cool_type / cool_structure / cool_contrast / cool_level / bury / payoff / hook。
【输出格式】严格输出单个 JSON 对象（不要 markdown 代码块，不要注释），结构与上面 schema 一致：
{_NODE_SCHEMA}"""


# ----------------------------------------------------------------------------
# 分段生成执行（守护线程跑，不占请求连接）
# ----------------------------------------------------------------------------
def _run_node_job(job):
    """start 任务：按 main_event 逐段生成，段与段之间空出 0.25s，逐段写入 job.nodes。
    全局采用"整卷预算管控"，确保 < 前端 MAX_MS 阈值（默认 12/15 分钟），不会因串行 3×重试 × 3 轮放大到数小时。"""
    try:
        from app import db, _call_llm, _extract_json_from_llm
        st = job.get('_state')
        if st is None:
            job['state'] = 'error'
            job['error'] = '内部错误：任务缺少 state，请重新提交'
            return
        st['cohesion'] = _build_cohesion(st)
        sys_prompt = _build_system_prompt(st)
        allocs = st.get('evt_alloc') or []
        n = len(allocs)
        job['total'] = n
        new_nodes = []
        index_seq = st.get('node_index_offset') or 0

        # ================ 整卷超时预算管控 ================
        # 目标：整卷总墙钟时长严格 < 前端 ChatPanel.MAX_MS(=15min)，永远由后端主动先收束
        # 成高质量占位，绝不把控制权交给前端报错。
        TOTAL_BUDGET_S = 10 * 60           # 整卷墙钟预算 = 10 分钟（留 5 分钟余量给冷启动 + 网络波动）
        MARGIN_SAFE_S = 90                 # 最后 90s 视为"安全边际区"：只 1 轮 + timeout≤60s + retry=0
        started_at = _time.time()

        def _budget_left() -> float:
            """剩余预算（秒），永远 >=0。"""
            return max(0.0, TOTAL_BUDGET_S - (_time.time() - started_at))

        for i, alloc in enumerate(allocs):
            if job.get('_cancel'):
                break
            user_prompt = _build_user_prompt(st, alloc, i == 0, i == n - 1)
            alloc_title = alloc.get('title') or ''
            me_idx = alloc.get('me_index') or (i + 1)
            s_alloc, e_alloc = int(alloc['s']), int(alloc['e'])
            job['current_segment'] = alloc_title or f'事件{me_idx}'

            # 按剩余预算 × 剩余段数，均分得到本段可用预算
            segs_left = max(1, n - i)
            seg_budget = _budget_left() / segs_left
            # --- 按预算自适应 timeout / retry / 补漏轮数 ---
            # LLM 对 8 章子段（输出 JSON ≈ 3k tokens）：
            #   - 冷启动 + 推理好天气 ~ 30~60 s
            #   - 偶发速率/拥塞 ~ 60~120 s
            #   - 180 s 以上基本是 LLM 队列卡住/掉包，再等无意义
            # 因此 timeout 不要低于 60 s（否则首段 37s 直接判死），上限 120 s。
            if seg_budget <= 0:
                # 已超总预算 → 本段直接 0 次 LLM，用高质量占位（后置修复器 100% 兜底章不漏 + summary≥80字）
                job['message'] = f'[已收束] 总预算用尽，本段用占位补齐（第{s_alloc}-{e_alloc}章）…'
                _time.sleep(0.05)
                fixed_nodes, index_seq = _repair_nodes_to_one_ch_per_node(
                    [], s_alloc, e_alloc, me_idx, index_seq
                )
                new_nodes.extend(fixed_nodes)
                job['nodes'] = list(job.get('nodes') or []) + list(fixed_nodes)
                job['done'] = i + 1
                continue

            # 【timeout】：基于 seg_budget 直接给（不再除 2 惩罚），夹 [60, 120]
            per_call_timeout = int(max(60, min(120, seg_budget)))
            # 【retry_count】：预算宽裕才 2 次重试，中段 1 次；紧张段 0 次（但仍用补漏轮当"二次机会"）
            if seg_budget < 70:
                retry_count = 0
            elif seg_budget < 140:
                retry_count = 1
            else:
                retry_count = 2
            # 【补漏轮数 max_rounds】：紧张段只 1+1=2 轮（1主+1补漏，相当于 2 次"主生成机会"）；
            # 为什么 seg_budget<60 仍给 2 轮？因为第 1 轮 LLM 超时就判死太亏（你刚遇到），
            # 补漏轮会把"本段全部 missing_chs"再跑一次 —— 本质就是低成本重试。
            if seg_budget < 60:
                max_rounds = 2
            elif seg_budget < 180:
                max_rounds = 3
            else:
                max_rounds = 3  # 上限 3 轮（1 主 + 2 补）
            # 安全边际区（最后 90s）：强制 2 轮，timeout 最多 60s，retry=0
            if _budget_left() < MARGIN_SAFE_S:
                max_rounds = min(max_rounds, 2)
                retry_count = 0
                per_call_timeout = min(per_call_timeout, 60)
            job['budget'] = {
                'total_s': TOTAL_BUDGET_S,
                'left_s': round(_budget_left(), 1),
                'seg_budget_s': round(seg_budget, 1),
                'per_call_timeout_s': per_call_timeout,
                'retry_count': retry_count,
                'max_rounds': max_rounds,
            }

            # --- 本轮子段的累计节点：主生成 + 最多 max_rounds-1 轮补漏 ---
            round_nodes_accum: list = []
            all_missing_chs_for_retry: list = list(range(s_alloc, e_alloc + 1))  # 第2轮若主生成全空，missing=全集 → 等于"重试"

            def _missing_in_alloc(have_nodes):
                covered = set()
                for nd in have_nodes:
                    rng = _parse_chapters_field(nd.get('chapters'))
                    if not rng: continue
                    a, b = rng
                    for c in range(max(a, s_alloc), min(b, e_alloc) + 1):
                        covered.add(c)
                full = set(range(s_alloc, e_alloc + 1))
                return sorted(full - covered)

            do_rounds = 0
            segment_error_first = None  # 只记录首次错误（用于日志，不直接中断）
            segment_had_any_ok = False
            while do_rounds < max_rounds and not job.get('_cancel') and _budget_left() > 5:
                do_rounds += 1
                if do_rounds == 1:
                    job['message'] = (f'[1/{max_rounds}] 生成子节点：{alloc_title or "事件" + str(me_idx)}（第{s_alloc}-{e_alloc}章）'
                                      f' ⏱剩{round(_budget_left(),0)}s t/o={per_call_timeout}s retry={retry_count}')
                    prompt = user_prompt
                    temperature = 0.65
                else:
                    missing = _missing_in_alloc(round_nodes_accum)
                    if not missing:
                        break  # 本段全齐，提前结束
                    all_missing_chs_for_retry = missing
                    job['message'] = (f'[{do_rounds}/{max_rounds}] 为 {alloc_title or "事件"+str(me_idx)} 补 {len(missing)} 章 '
                                      f'（{",".join(str(c) for c in missing[:8])}{"…" if len(missing)>8 else ""}）'
                                      f' ⏱剩{round(_budget_left(),0)}s t/o={per_call_timeout}s retry={retry_count}')
                    prompt, _ch_list = _build_fillgap_prompt(st, alloc, missing, round_nodes_accum)
                    if not prompt:
                        break
                    temperature = 0.75 + (do_rounds - 1) * 0.05
                content, err = _call_llm(
                    [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': prompt}],
                    max_tokens=0, temperature=temperature,
                    retry_count=retry_count, timeout=per_call_timeout,
                )
                nodes_this_round: list = []
                if err:
                    if segment_error_first is None:
                        segment_error_first = f'第{do_rounds}轮：{err}'
                else:
                    parsed, jerr = _extract_json_from_llm(content, expect='object')
                    if jerr:
                        if segment_error_first is None:
                            segment_error_first = f'第{do_rounds}轮JSON解析失败：{jerr}'
                    elif isinstance(parsed, dict):
                        nr = parsed.get('nodes') or []
                        if isinstance(nr, list):
                            nodes_this_round = nr
                            if len(nodes_this_round) > 0:
                                segment_had_any_ok = True
                # 合并入累计：相同 chapters 以新的覆盖
                def _key_ch(nd):
                    r = _parse_chapters_field(nd.get('chapters'))
                    return r[0] if r else None
                existing_by_ch = {_key_ch(nd): nd for nd in round_nodes_accum if _key_ch(nd) is not None}
                for nd in nodes_this_round:
                    k = _key_ch(nd)
                    if k is None or k < s_alloc or k > e_alloc:
                        continue
                    existing_by_ch[k] = nd
                round_nodes_accum = list(existing_by_ch.values())
                _time.sleep(0.1)
            # 全部轮次结束：只要"有过一次 LLM 非空输出"或"预算已到边际区"，就不抛 error；
            # 用后置修复器把缺章用高质量占位补齐，保证用户拿到完整结果（可单节点人工修订）。
            # 仅当：① 有报错 ② 累计全空 ③ 预算仍充足（>安全边际） 才判整卷失败。
            if segment_error_first is not None and len(round_nodes_accum) == 0 and _budget_left() > MARGIN_SAFE_S:
                job['error'] = f'事件“{alloc_title or me_idx}”生成失败：{segment_error_first}'
                break
            fixed_nodes, index_seq = _repair_nodes_to_one_ch_per_node(
                round_nodes_accum, s_alloc, e_alloc, me_idx, index_seq
            )
            new_nodes.extend(fixed_nodes)
            job['nodes'] = list(job.get('nodes') or []) + list(fixed_nodes)
            job['done'] = i + 1
            _time.sleep(0.1)
        if job.get('error') is None:
            final_nodes = sorted(job.get('nodes') or [], key=lambda x: _node_sort_key(x))
            # 整卷终态校验：最终节点必须覆盖 [start_chapter, evt_end] 全 cpv 章，缺就补
            sc = int(st.get('start_chapter') or 1)
            ec = int(st.get('evt_end') or (sc + int(st.get('cpv') or 50) - 1))
            cpv = int(st.get('cpv') or 50)
            if len(final_nodes) < cpv:
                final_nodes, _ = _repair_nodes_to_one_ch_per_node(
                    final_nodes, sc, ec, 0, 0
                )
            job['nodes'] = final_nodes
            job['state'] = 'done'
            left = round(_budget_left(), 0)
            placeholders = sum(1 for n in final_nodes if '补齐' in str(n.get('title','')) or '占位' in str(n.get('title','')))
            note = f'；其中高质量占位 {placeholders}/{cpv} 个（剩余预算{left}s，可人工编辑或单独修改意见重生成）' if placeholders > 0 else ''
            job['message'] = f'已完成，共生成 {len(final_nodes)} 个情节节点（对应第{sc}-{ec}章，共{cpv}章，耗时{round(TOTAL_BUDGET_S - left,0)}s）{note}。'
        else:
            job['state'] = 'error'
    except Exception as e:  # noqa: BLE001
        job['state'] = 'error'
        job['error'] = _safe_err(e)
    finally:
        try:
            from app import db
            db.session.remove()
        except Exception:
            pass


def _run_revise_job(job):
    """revise 任务：修改意见 → 重生成单个节点 → 存 job.result。"""
    try:
        from app import _call_llm, _extract_json_from_llm
        st = job.get('_state')
        orig = job.get('_revise_node')
        feedback = job.get('revise_feedback') or ''
        fidx = job.get('revise_node_index')
        if st is None or not isinstance(orig, dict):
            job['state'] = 'error'
            job['error'] = '内部错误：任务缺少 state 或原节点数据，请重新提交'
            return
        st['cohesion'] = ''
        user_prompt = _build_revise_prompt(st, orig, feedback)
        job['message'] = '正在按修改意见重写节点…'
        content, err = _call_llm(
            [{'role': 'system', 'content': _build_system_prompt(st)}, {'role': 'user', 'content': user_prompt}],
            max_tokens=0, temperature=0.6, retry_count=2, timeout=180,
        )
        if err:
            job['error'] = f'修改失败：{err}'
            job['state'] = 'error'
            return
        parsed, jerr = _extract_json_from_llm(content, expect='object')
        if jerr or not isinstance(parsed, dict):
            job['error'] = f'修改结果 JSON 解析失败：{jerr or "空结果"}'
            job['state'] = 'error'
            return
        new_node = parsed
        # 保持原 index / main_event_index / chapters（除非意见明确要求改）
        if new_node.get('index') in (None, orig.get('index')):
            new_node['index'] = orig.get('index', fidx)
        if not new_node.get('main_event_index'):
            new_node['main_event_index'] = orig.get('main_event_index')
        job['result'] = new_node
        job['state'] = 'done'
        job['message'] = '节点已按修改意见更新。'
    except Exception as e:  # noqa: BLE001
        job['state'] = 'error'
        job['error'] = _safe_err(e)
    finally:
        try:
            from app import db
            db.session.remove()
        except Exception:
            pass


def _parse_chapters_field(v):
    """把节点 chapters 字段解析成 (start_ch, end_ch) 整数对，始终 start≤end。"""
    if v is None:
        return None
    if isinstance(v, int):
        if v <= 0: return None
        return v, v
    if isinstance(v, list):
        if not v: return None
        flat = []
        for x in v:
            r = _parse_chapters_field(x)
            if r: flat.extend(range(r[0], r[1] + 1))
        if not flat: return None
        return min(flat), max(flat)
    s = str(v).strip()
    if not s: return None
    # "第3章", "Chapter 10" → 先剥前缀
    m = _re.search(r'(\d+)\s*[-–~～—到至]\s*(\d+)', s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b) if a <= b else (b, a)
    m = _re.search(r'(\d+)', s)
    if m:
        a = int(m.group(1))
        return a, a
    return None


def _normalize_node_resources_and_relations(nd, ch_int, prev_total_map):
    """统一给 A+C 修复后的节点补：人物关系（缺省标签/规范成字符串数组）+ 资源三字段。
    返回：(normalized_node, current_total_map)。current_total_map 用于下一章滚动核算。
    - prev_total_map: 上一章结束的 total_resources_owned（字典：5类→列表）
    """
    if not isinstance(nd, dict):
        nd = {}
    # ==== ① characters 统一成数组，每项 "姓名|关系:X" ====
    raw = nd.get('characters')
    normalized_chars: list[str] = []
    # 常用关系枚举：按关键词猜，猜不到就兜底"关系:主角/亲友/路人"
    def _guess_relation(name: str) -> str:
        n = name.strip()
        if not n:
            return "路人"
        low = n
        if any(k in low for k in ('主角', '男主', '女主', '我', '本人')):
            return "主角"
        if any(k in low for k in ('父', '母', '兄', '弟', '姐', '妹', '子', '女', '丈', '妻', '夫', '爷', '奶', '公', '婆')):
            return "家人"
        if any(k in low for k in ('师', '徒', '友', '朋', '兄', '姐')):
            return "亲友"
        if any(k in low for k in ('爱', '道侣', '伴侣', '女友', '男友', '妻', '夫')):
            return "爱人"
        if any(k in low for k in ('敌', '仇', '反派', '杀', '对手', 'BOSS', 'boss', '恶')):
            return "敌对"
        if any(k in low for k in ('同', '战友', '盟', '门内')):
            return "盟友·同僚"
        if any(k in low for k in ('下属', '手下', '随从', '仆', '兵')):
            return "下属"
        if any(k in low for k in ('长老', '掌门', '主', '上司', '管')):
            return "上司"
        if any(k in low for k in ('路人', '围观', '旁观', '群众', '杂兵')):
            return "路人"
        return "中立"

    def _split_to_people(raw_str):
        # 按常见分隔符拆分；避免括号内的内容被切
        out: list[str] = []
        cur = ''
        depth = 0
        for ch in raw_str:
            if ch in '（([':
                depth += 1; cur += ch
            elif ch in '）)]':
                depth = max(0, depth - 1); cur += ch
            elif depth == 0 and ch in '、,，；; \t\n/|':
                if cur.strip():
                    out.append(cur.strip())
                cur = ''
            else:
                cur += ch
        if cur.strip():
            out.append(cur.strip())
        return [x for x in out if x]

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                nm = str(item.get('name') or item.get('人物') or '').strip()
                rel = str(item.get('relation') or item.get('关系') or '').strip()
                if not rel:
                    rel = _guess_relation(nm)
                if nm:
                    normalized_chars.append(f"{nm}|关系:{rel}")
            elif isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                if '|关系:' in s or '（关系' in s or '(关系' in s:
                    # 已经写了关系 → 规范化
                    s2 = s.replace('（关系：', '|关系:').replace('（关系:', '|关系:').replace('(关系：', '|关系:').replace('(关系:', '|关系:')
                    if '）' in s2 and s2.endswith('）'):
                        s2 = s2[:-1]
                    if ')' in s2 and s2.endswith(')'):
                        s2 = s2[:-1]
                    normalized_chars.append(s2)
                else:
                    # 纯名字 → 切分出人，逐个补关系标签
                    for nm in _split_to_people(s):
                        if not nm:
                            continue
                        # 名字里带括号(关系:X) → 解析
                        rel_v = ''
                        par_m = re.search(r'[（(]\s*关?系?[:：]\s*([^()）]+)\s*[)）]', nm)
                        if par_m:
                            rel_v = par_m.group(1).strip()
                            nm_clean = nm[:par_m.start()].strip()
                        else:
                            nm_clean = nm
                            rel_v = _guess_relation(nm_clean)
                        if nm_clean:
                            normalized_chars.append(f"{nm_clean}|关系:{rel_v}")
    elif isinstance(raw, str):
        s = raw.strip()
        if s:
            if '|关系:' in s or '（关系' in s or '(关系' in s or '、' in s or '，' in s:
                for p in _split_to_people(s):
                    if not p:
                        continue
                    if '|关系:' in p:
                        normalized_chars.append(p)
                    else:
                        rel_v = ''
                        par_m = re.search(r'[（(]\s*关?系?[:：]\s*([^()）]+)\s*[)）]', p)
                        if par_m:
                            rel_v = par_m.group(1).strip()
                            nm_clean = p[:par_m.start()].strip()
                        else:
                            nm_clean = p
                            rel_v = _guess_relation(p)
                        if nm_clean:
                            normalized_chars.append(f"{nm_clean}|关系:{rel_v}")
            else:
                normalized_chars.append(f"{s}|关系:{_guess_relation(s)}")
    # 兜底：空人物 → 默认"主角|关系:主角"（保证写正文时至少有主角名占位）
    if not normalized_chars:
        normalized_chars = ["主角|关系:主角"]
    nd['characters'] = normalized_chars

    # ==== ② resources_gained / resources_used 规范成字符串数组 ====
    def _norm_resource_list(v) -> list[str]:
        res: list[str] = []
        if isinstance(v, list):
            for x in v:
                if x is None:
                    continue
                s = str(x).strip()
                if s:
                    res.append(s)
        elif isinstance(v, dict):
            for cat, items in v.items():
                c_cat = str(cat).strip() or '其它'
                if isinstance(items, list):
                    for it in items:
                        s = str(it).strip()
                        if s:
                            res.append(f"【{c_cat}】{s}")
                elif items:
                    s = str(items).strip()
                    if s:
                        res.append(f"【{c_cat}】{s}")
        elif isinstance(v, str) and v.strip():
            # 行分隔字符串 → 拆成多项
            for ln in re.split(r'[；;\n]+', v):
                s = ln.strip()
                if not s:
                    continue
                if s.startswith('【'):
                    res.append(s)
                else:
                    res.append(f"【物品】{s}")
        # 分类过滤：没有【类别】前缀的自动补【物品】或【功法能力】
        tidy: list[str] = []
        for s in res:
            if not s:
                continue
            if s.startswith('【'):
                tidy.append(s); continue
            cat = '物品'
            low = s
            if any(k in low for k in ('灵石', '银两', '黄金', '金币', '元石', '铜币', '钱', '财')):
                cat = '钱财'
            elif any(k in low for k in ('剑', '刀', '枪', '甲', '盾', '法宝', '法器', '灵器', '命器', '弓', '棍', '杖', '鞭', '锤')):
                cat = '武器法宝'
            elif any(k in low for k in ('功法', '武技', '神通', '秘法', '诀', '术', '天赋', '血脉', '觉醒', '《')):
                cat = '功法能力'
            elif any(k in low for k in ('称号', '职位', '身份', '归属', '弟子', '长老', '令牌', '情报', '地契', '契约', '人脉')):
                cat = '其它'
            tidy.append(f"【{cat}】{s}")
        return tidy

    gained = _norm_resource_list(nd.get('resources_gained'))
    used = _norm_resource_list(nd.get('resources_used'))
    nd['resources_gained'] = gained
    nd['resources_used'] = used

    # ==== ③ total_resources_owned 规范成 { 钱财:[], 物品:[], 武器法宝:[], 功法能力:[], 其它:[] }
    #      滚动核算：上一章total - 本章used + 本章gained = 本章total
    CAT_KEYS = ('钱财', '物品', '武器法宝', '功法能力', '其它')
    def _parse_cat_map(raw) -> dict:
        """把各种形态的 total_resources_owned 解析成 {cat: list[str]}"""
        out: dict[str, list[str]] = {c: [] for c in CAT_KEYS}
        if isinstance(raw, dict):
            for c in CAT_KEYS:
                val = raw.get(c) or raw.get(c.lower()) or raw.get(c.replace('武器法宝', '武器·法宝')) or raw.get(c + '类') or []
                if isinstance(val, list):
                    for x in val:
                        if x is None: continue
                        s = str(x).strip()
                        if s:
                            out[c].append(s)
                elif isinstance(val, str) and val.strip():
                    for ln in re.split(r'[；;\n,，]+', val):
                        s = ln.strip()
                        if s: out[c].append(s)
        elif isinstance(raw, list):
            # 列表形态 → 按前缀分类
            for x in raw:
                s = str(x or '').strip()
                if not s: continue
                m = re.match(r'【(.+?)】', s)
                if m:
                    c = m.group(1).strip()
                    if c not in out: c = '物品'
                    out[c].append(s)
                else:
                    out['物品'].append(s)
        elif isinstance(raw, str) and raw.strip():
            for ln in raw.splitlines():
                s = ln.strip()
                if not s: continue
                m = re.match(r'【(.+?)】', s)
                if m:
                    c = m.group(1).strip()
                    if c not in out: c = '物品'
                    out[c].append(s)
                else:
                    out['物品'].append(s)
        return out

    def _minus_category(cat_items: list[str], used_entries: list[str]) -> list[str]:
        """按条目名+数量的近似匹配，扣减 used_entries。
        思路：构造字典 {名前缀: [剩余数量+整条]}；used 里若有同名条目就扣。
        扣不动就原样（兜底不丢信息）。
        """
        # 先把 gained/used 里的前缀去掉取正文名+数量
        import re as _re
        def _strip_cat(s: str):
            return _re.sub(r'^【.+?】', '', s).strip() or s

        def _name_qty(s: str):
            core = _strip_cat(s)
            # 拆分 名×N / N个XX / XX*N
            m1 = _re.search(r'^(.+?)\s*[×xX*]\s*(\d+)\s*个?$', core)
            if m1:
                return m1.group(1).strip(), int(m1.group(2))
            m2 = _re.search(r'^(\d+)\s*(?:个|份|颗|粒|斤|两|匹|张|件|本|块|段|把|柄|套|对|枚|卷|种|重|层)\s*(.+)$', core)
            if m2:
                return m2.group(2).strip(), int(m2.group(1))
            return core.strip(), 1  # 没写数量 → 默认 1

        used_by_prefix: dict[str, int] = {}
        for u in used_entries:
            nm, q = _name_qty(u)
            if nm:
                used_by_prefix[nm] = used_by_prefix.get(nm, 0) + q

        result: list[str] = []
        # 先合并同前缀（输入可能有重复前缀的多条）
        bucket: dict[str, int] = {}
        originals: dict[str, str] = {}
        for it in cat_items:
            nm, q = _name_qty(it)
            if not nm:
                continue
            bucket[nm] = bucket.get(nm, 0) + q
            if nm not in originals:
                originals[nm] = _strip_cat(it)
        for nm, qty in bucket.items():
            used_q = used_by_prefix.pop(nm, 0)
            left = qty - used_q
            if left <= 0:
                continue
            core = originals.get(nm) or nm
            # 再拼回「【类别】名称×数量」
            if left == 1:
                result.append(core)
            else:
                # 如果原先是「5个丹」这种格式，保持风格，统一写成 core×left
                result.append(f"{core}×{left}")
        # used 里没扣掉的条目（名称不匹配）→ 兜底：不扣，避免错误删资源（宁可多，不能吞）
        return result

    # 先拿 prev_total（上一章总资源），没有就用本节点自带的 total_resources_owned
    prev_map = _parse_cat_map(prev_total_map if isinstance(prev_total_map, dict) else None)
    self_total = _parse_cat_map(nd.get('total_resources_owned'))
    # 如果上一章是空（首章 / 续会前段没信息），优先用本节点自带的 total；否则按滚动公式：prev - used + gained
    if not any(prev_map.values()):
        # 首章/无前置信息：优先信任本节点自带的 total_resources_owned，否则按 gained 建（gained就是第一章收获）
        if any(self_total.values()):
            merged_map = {c: list(self_total.get(c) or []) for c in CAT_KEYS}
        else:
            merged_map = {c: [] for c in CAT_KEYS}
            # gained 按前缀分类注入
            for g in gained:
                mm = re.match(r'【(.+?)】', g)
                if mm:
                    c = mm.group(1)
                    if c not in merged_map: c = '物品'
                    merged_map[c].append(g)
    else:
        # 滚动公式：prev - used + gained
        merged_map = {c: list(prev_map.get(c) or []) for c in CAT_KEYS}
        # Step1: 扣 used
        for c in CAT_KEYS:
            if not merged_map.get(c):
                continue
            # 从本类 used_entries 里挑前缀匹配的进行扣减
            used_in_cat: list[str] = []
            for u in used:
                mm = re.match(r'【(.+?)】', u)
                if mm and mm.group(1) == c:
                    used_in_cat.append(u)
            merged_map[c] = _minus_category(merged_map[c], used_in_cat)
        # Step2: 加 gained
        for g in gained:
            mm = re.match(r'【(.+?)】', g)
            if mm:
                c = mm.group(1)
                if c not in merged_map: c = '物品'
                merged_map[c].append(g)
    # 对每一类做：合并同前缀多条成「名称×总数量」（避免滚动后一堆重复的【物品】洗髓丹×1 | 洗髓丹×3 → 合并成洗髓丹×4）
    import re as _re2
    def _compress_cat(items: list[str]) -> list[str]:
        def _strip(s): return _re2.sub(r'^【.+?】', '', s).strip() or s
        def _nq(s):
            core = _strip(s)
            m1 = _re2.search(r'^(.+?)\s*[×xX*]\s*(\d+)\s*个?$', core)
            if m1:
                return m1.group(1).strip(), int(m1.group(2))
            m2 = _re2.search(r'^(\d+)\s*(?:个|份|颗|粒|斤|两|匹|张|件|本|块|段|把|柄|套|对|枚|卷|种|重|层)\s*(.+)$', core)
            if m2:
                return m2.group(2).strip(), int(m2.group(1))
            return core.strip(), 1
        bucket: dict[str, int] = {}
        for it in items:
            nm, q = _nq(it)
            if not nm:
                continue
            bucket[nm] = bucket.get(nm, 0) + q
        out: list[str] = []
        for nm, q in sorted(bucket.items()):
            if q <= 0: continue
            if q == 1:
                out.append(nm)
            else:
                out.append(f"{nm}×{q}")
        return out

    for c in CAT_KEYS:
        merged_map[c] = _compress_cat(merged_map.get(c) or [])
    # 空类也保留 key 存在，保证 UI / JSON schema 一致
    final_total = {c: merged_map.get(c) or [] for c in CAT_KEYS}
    nd['total_resources_owned'] = final_total
    return nd, final_total


def _repair_nodes_to_one_ch_per_node(nodes, alloc_s, alloc_e, me_index, index_offset_start):
    """方案 A+C 的后置"门禁修复器"：无论模型输出如何，保证：
       - 返回节点数 = ec = alloc_e - alloc_s + 1
       - 每个 chapters 精确为该区间内唯一单章，无重叠无跳章
       - 返回 (nodes_list, next_index_offset_after)
    """
    ec = alloc_e - alloc_s + 1
    if ec <= 0:
        return [], index_offset_start
    # 从每个原节点中抽"有效章号列表"（平铺展开），尽量保留原内容
    per_chapter = {}  # ch -> node dict 拷贝
    for nd in nodes or []:
        if not isinstance(nd, dict):
            continue
        rng = _parse_chapters_field(nd.get('chapters'))
        if not rng:
            continue
        a, b = rng
        # 只保留本事件区间内的章
        for ch in range(max(a, alloc_s), min(b, alloc_e) + 1):
            if ch in per_chapter:
                continue
            cp = dict(nd)
            # 该节点修复为单章
            cp['chapters'] = ch
            cp['chapter_beats'] = [cp.get('chapter_beats') or cp.get('summary') or '']
            if isinstance(cp['chapter_beats'], list) and len(cp['chapter_beats']) == 0:
                cp['chapter_beats'] = [cp.get('summary') or '']
            elif isinstance(cp['chapter_beats'], str):
                cp['chapter_beats'] = [cp['chapter_beats']]
            # 截短多章 beats 到第 1 条（本章）
            cp['chapter_beats'] = [str(cp['chapter_beats'][0])[:600]]
            # 若原 summary 太短，把 beat 塞进去
            if len(str(cp.get('summary') or '')) < 60:
                cp['summary'] = str(cp['chapter_beats'][0])
            per_chapter[ch] = cp
    # 按章号清单补齐缺失章（用占位节点）
    next_idx = int(index_offset_start)
    ordered = []
    # 资源滚动：首章之前为 None → 按首章 gained / 自带 total 建；后续每章按 prev_total + gained - used 滚动
    prev_total: dict | None = None
    for ch in range(alloc_s, alloc_e + 1):
        if ch not in per_chapter:
            # 占位节点：标注为自动补齐，后续可人工修改
            per_chapter[ch] = {
                'chapters': ch,
                'title': f'第{ch}章（自动补齐占位）',
                'type': 'M',
                'summary': f'第{ch}章：承接上一章推进主线，需在正式创作前人工补齐该章的冲突转折。',
                'chapter_beats': [f'第{ch}章占位：请人工编辑'],
                'events': '',
                'conflict': '',
                'characters': '',
                'location': '',
                'time': '',
                'hook': '',
                'bury': '',
                'payoff': '',
                'resources_gained': [],
                'resources_used': [],
            }
        nd = per_chapter[ch]
        nd.setdefault('main_event_index', me_index)
        nd.setdefault('title', f'第{ch}章节点')
        # summary 强化兜底：LLM 若 summary + chapter_beats 互指造成"双短"死循环，
        # 这里用本节点已有字段拼一段最少 80 字的梗概，保证该章能支撑 2400±100 字正文。
        if len(str(nd.get('summary') or '')) < 80:
            base_title = str(nd.get('title') or f'第{ch}章')
            parts = [
                f'第{ch}章（{base_title}）：开场先承接上一章节奏自然入戏，',
                str(nd.get('events') or '随后推进核心事件，让主角在关键场景做出关键选择。'),
                '→冲突：', str(nd.get('conflict') or '遇到对手压制或规则卡点，推进受阻陷入被动。'),
                '→转折：主角依靠自身积累或伏笔破局，',
                str(nd.get('cool_type') or '完成一次爽点呈现。'),
                '→收尾：结果尘埃落定后余波铺开，',
                '钩子：', str(nd.get('hook') or '抛出新悬念引向下一章。'),
            ]
            nd['summary'] = ''.join(parts)[:400]
            # summary 同时同步回 chapter_beats[0] 保持单节拍一致
            nd['chapter_beats'] = [str(nd['summary'])[:600]]
        # 保证 index 连续且唯一（即使原有 index 重复）
        next_idx += 1
        nd['index'] = next_idx
        if not isinstance(nd.get('chapter_beats'), list) or len(nd['chapter_beats']) < 1:
            nd['chapter_beats'] = [str(nd.get('summary') or '')]
        # 资源三字段 + 人物关系规范化 + 滚动核算总资源（prev_total → 本章total）
        nd, prev_total = _normalize_node_resources_and_relations(nd, ch, prev_total)
        ordered.append(nd)
    return ordered, next_idx


def _node_sort_key(n):
    try:
        return int(str(n.get('chapters', '')).split('-')[0])
    except (ValueError, TypeError):
        return 999999


def _build_cohesion(st):
    vi = st.get('volume_index') or 1
    hook = st.get('prev_vol_hook') or ''
    sc = st.get('start_chapter') or 1
    pe = st.get('prev_vol_end_chapter') or 0
    pv = (st.get('prev_vol_summary') or '')[:200]
    if vi > 1 and hook:
        return (f"""【卷间衔接铁律】（本卷为第{vi}卷，必须严格承接第{vi-1}卷）
- 上一卷卷尾钩子：{hook}
- 上一卷核心主线：{pv or '（无）'}
- 本卷开头必须承接上一卷卷尾钩子的悬念/危机，不得凭空开启新场景
- 本卷第一个情节节点的起始章号必须为 {sc}（上一卷结束于第{pe}章）""")
    return ""


def _auth_user_id():
    """自包含鉴权（与 app.login_required 相同口径）：返回 (user_id, error_msg)。

    之所以不直接用 @login_required：入节点设计模块在 app.py 的 register_blueprint 之后才定义
    login_required，模块导入期无法引用，故在路由内部自行解析 token。
    """
    from app import AuthToken, hash_token
    from datetime import datetime, timezone
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        return None, '请先登录'
    at = AuthToken.query.filter_by(token=hash_token(token)).first()
    now = datetime.now(timezone.utc)
    if not at:
        return None, '登录已过期，请重新登录'
    exp = at.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        return None, '登录已过期，请重新登录'
    return at.user_id, None


def _check_book_owner(book_id, uid):
    """校验书籍归属（true=通过；false=不通过并已 set 错误返回体）。"""
    from app import Book
    book = Book.query.get(book_id)
    if not book:
        return False, '书籍不存在'
    if uid is not None and book.user_id and str(book.user_id) != str(uid):
        return False, '无权访问该书籍'
    return True, ''


# ----------------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------------
@node_design_bp.route('/api/books/<book_id>/node-design/submit', methods=['POST'])
def node_design_submit(book_id):
    """提交分段节点生成任务（start）或重生成单个节点（revise），秒回 job_id。"""
    uid, err = _auth_user_id()
    if err:
        return jsonify({'error': err}), 401
    ok, oerr = _check_book_owner(book_id, uid)
    if not ok:
        return jsonify({'error': oerr}), 404 if oerr == '书籍不存在' else 403

    data = request.json or {}
    volume_index = data.get('volume_index')
    if volume_index is None:
        return jsonify({'error': '缺少 volume_index'}), 400
    try:
        volume_index = int(volume_index)
    except (TypeError, ValueError):
        return jsonify({'error': 'volume_index 必须为数字'}), 400

    action = data.get('action') or 'start'
    session_id = (data.get('session_id') or '').strip() or f'nd-{_uuid.uuid4().hex[:12]}'

    # 目标卷上下文
    rv = {}
    st = _load_volume_context(book_id, volume_index, data, rv, _ACT_DESCRIPTIONS)
    if st is None:
        return jsonify({'error': rv.get('error') or '无法加载卷上下文'}), 400

    job = None
    if action == 'revise':
        node_index = data.get('node_index')
        feedback = (data.get('feedback') or '').strip()
        if node_index is None:
            return jsonify({'error': '缺少 node_index'}), 400
        if not feedback:
            return jsonify({'error': '修改意见不能为空'}), 400
        orig = None
        for n in st['existing_nodes']:
            if isinstance(n, dict) and int(n.get('index') or -1) == int(node_index):
                orig = n
                break
        if orig is None and st['target']:
            for n in (st['target'].get('nodes') or []):
                if isinstance(n, dict) and int(n.get('index') or -1) == int(node_index):
                    orig = n
                    break
        if orig is None:
            return jsonify({'error': f'未找到 index={node_index} 的节点（可能尚未采纳入库），请先采纳再修改。'}), 404
        job = _job_factory(kind='revise', revise_node_index=int(node_index),
                          revise_feedback=feedback, owner=uid,
                          _state=st, _revise_node=orig)
    else:
        job = _job_factory(kind='start', owner=uid, _state=st)
        job['total'] = st['total_segments']

    job_id = job['job_id']
    with _LOCK:
        _JOBS[job_id] = job
        _ACTIVE[session_id] = job

    def _runner():
        with _app_ctx():
            if job['kind'] == 'revise':
                _run_revise_job(job)
            else:
                _run_node_job(job)
    threading.Thread(target=_runner, daemon=True).start()
    return jsonify({'ok': True, 'job_id': job_id, 'session_id': session_id,
                    'total': job['total'], 'kind': job['kind']}), 200


@node_design_bp.route('/api/books/<book_id>/node-design/status', methods=['GET'])
def node_design_status(book_id):
    """渐进轮询：返回 {state, done, total, nodes, message, error}。"""
    job_id = (request.args.get('job_id') or '').strip()
    if not job_id:
        return jsonify({'error': '缺少 job_id'}), 400
    _sweep()
    with _LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return jsonify({'error': '任务不存在或已过期，请重新提交'}), 404
    uid = getattr(request, 'current_user_id', None)
    if job.get('owner') is not None and uid is not None and str(job.get('owner')) != str(uid):
        return jsonify({'error': '无权查看该任务'}), 403
    return jsonify({
        'state': job.get('state', 'running'),
        'done': job.get('done', 0),
        'total': job.get('total', 0),
        'current_segment': job.get('current_segment'),
        'nodes': job.get('nodes') or [],
        'result': job.get('result'),
        'kind': job.get('kind'),
        'message': job.get('message') or '…',
        'error': job.get('error'),
    }), 200


@node_design_bp.route('/api/books/<book_id>/node-design/apply', methods=['POST'])
def node_design_apply(book_id):
    """采纳落地：把节点按 index 合并进 book_bible.timeline 对应卷的 nodes，保留卷级其他字段。"""
    from app import db, BookBible
    try:
        data = request.json or {}
        volume_index = data.get('volume_index')
        nodes = data.get('nodes')
        if volume_index is None or not isinstance(nodes, list) or not nodes:
            return jsonify({'error': '缺少 volume_index 或 nodes 列表'}), 400
        try:
            volume_index = int(volume_index)
        except (TypeError, ValueError):
            return jsonify({'error': 'volume_index 必须为数字'}), 400

        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            return jsonify({'error': '该作品还没有剧情线（timeline）'}), 404

        vols = []
        try:
            parsed = json.loads(bb.timeline or '[]')
            if isinstance(parsed, list):
                vols = parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            vols = []

        target = None
        for v in vols:
            if _vol_index(v) == volume_index:
                target = v
                break
        if target is None:
            target = {'volume': f'第{volume_index}卷', 'volume_index': volume_index}
            vols.append(target)

        old_nodes = target.get('nodes') if isinstance(target.get('nodes'), list) else []
        merged = {}
        for n in old_nodes:
            if isinstance(n, dict):
                idx = n.get('index')
                if isinstance(idx, int) or (idx and str(idx).isdigit()):
                    merged[int(idx)] = n  # type: ignore[index]
        for n in nodes:
            if not isinstance(n, dict):
                continue
            idx = n.get('index')
            try:
                idx = int(idx) if idx not in (None, '') else None
            except (TypeError, ValueError):
                idx = None
            if idx is None:
                idx = (max(merged.keys(), default=0)) + 1
                n['index'] = idx
            merged[idx] = n
        new_nodes = [merged[k] for k in sorted(merged.keys())]
        target['nodes'] = new_nodes
        bb.timeline = json.dumps(vols, ensure_ascii=False)
        db.session.add(bb)
        db.session.commit()
        return jsonify({'ok': True, 'volume_index': volume_index,
                        'node_count': len(new_nodes), 'timeline': bb.timeline}), 200
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({'error': f'落地失败：{_safe_err(e)}'}), 500


@node_design_bp.route('/api/books/<book_id>/node-design/cancel', methods=['POST'])
def node_design_cancel(book_id):
    data = request.json or {}
    job_id = (data.get('job_id') or '').strip()
    if job_id:
        with _LOCK:
            job = _JOBS.get(job_id)
            if job:
                job['_cancel'] = True
                job['state'] = 'cancelled'
                job['message'] = '已取消'
        return jsonify({'ok': True}), 200
    return jsonify({'error': '缺少 job_id'}), 400


def _app_ctx():
    """返回后台线程所需的 Flask app context（DB 操作依赖）。"""
    from app import app
    return app.app_context()