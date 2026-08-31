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

    total_volumes = _get_total_volumes(bb, book)
    cpv = _get_chapters_per_volume(bb, book)
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
  "characters": "本节点核心出场人物，按权重",
  "events": "本节点事件：谁在什么场景做了什么→关键后果（20-40字，动词+名词）",
  "conflict": "本节点冲突：主角vs谁/争什么/卡点（20字内）",
  "time": "本节点时间锚",
  "location": "本节点精确场景地点",
  "realm_change": "本节点结束时境界/根基/金手指变化",
  "age_change": "本节点结束时主角年龄/时程变化",
  "summary": "本节点事件推进梗概（起因→关键动作→直接后果→收尾→钩子，纯动词+名词，禁止环境/比喻/形容词/心理铺陈）",
  "chapter_beats": [{"chapter": 章号, "beat": "本章只推进的这一段剧情（20-35字）"}],
  "cool_type": "爽点类型（八选一）",
  "cool_structure": "爽点结构",
  "cool_contrast": "衬托方式",
  "cool_level": "爽点层级",
  "bury": "第XX章埋下：XXX；预计回收：第YY章（第Z卷）/空串",
  "payoff": "第XX章回收：第YY章埋下的XXX/空串",
  "hook": "本节点章尾钩子"
}"""


def _build_system_prompt(st):
    """分段节点生成系统提示词（加固输出范围的卷隔离铁律）。"""
    per_count = 3 if st['me_count'] >= 7 else 4
    per_max = 5 if st['me_count'] >= 7 else 6
    return f"""你是番茄小说金番作者级别的情节节点设计师。
任务：为第 {st['volume_index']} 卷“{st['volume_title']}”的【一个】主要剧情事件（main_event）生成 {per_count}-{per_max} 个情节子节点（nodes）。
【输出范围铁律】只允许输出本卷（第{st['volume_index']}卷）内容。禁止在输出中复述/罗列/带入任何其他卷的大纲/节点/剧情概要——仅供你推理衔接参考，绝不写进输出。nodes 的 chapters 必须严格落在本卷 {st['start_chapter']}-{st['evt_end']} 章区间内。
【模式说明】本卷已有卷剧情，你的任务是为 user prompt 指定的这一个 main_event 生成子节点。
- 不修改本卷 summary/main_plot/main_events/core_conflict/ending_hook 等卷级字段，只输出 nodes。
- 各子节点之间剧情连贯：上一节点末尾自然衔接到下一节点开头。
- 本卷第一个子节点承接上一卷卷尾钩子；本卷最后一个子节点须埋下卷尾钩子承接本卷 ending_hook。
- 子节点必须严格归属到本 main_event，不得脱离自创剧情。
- 子节点 chapters 要连续不重叠，恰好覆盖本 main_event 被分派的章节区间。
{st['cohesion']}
【五幕模型对齐】本卷对应五幕中的“{st['current_act']}”幕：{st['act_desc']}
节点设计必须服务于该幕的核心目标。
{_COOL_SYSTEM}
【节点要素铁律】每个节点只许包含以下结构要素（剧情调度卡，不是正文/抒情）：
time / location / events / conflict / characters / hook；埋收 bury/payoff 精确到章。
【负清单】禁止环境物象描写、比喻拟人排比工整对仗、动作细节链与形容词堆砌、心理情绪铺陈；summary 只用动词+名词推进梗概。
【章粒度】节点 chapters 是多章区间时，必须按每章一条拆出 chapter_beats（数组长度=区间章数，第i条对应该区间第i章），每条 beat 只能推进本章内容，不透支下一章。
【伏笔埋收】每个子节点标注 bury（真埋才写）/payoff（真收才写），与所属 main_event 对齐，跨卷 payoff 指明第X卷。
【输出格式】严格输出以下 JSON（不要 markdown 代码块，不要注释），nodes 为数组：
{_NODE_SCHEMA}
【章型配额】M主线50% / C角色10% / W世界观10% / D日常20% / F伏笔10%
【小故事闭环】新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子
【节点容量】每个子节点 summary 须足以支撑其 chapters 区间（按每章2400字估算），不得简略。"""


def _build_user_prompt(st, alloc, is_first_alloc, is_last_alloc):
    """单个 main_event 的生成用户提示词（一个分段）。"""
    _me = alloc['raw']
    main_block = (
        f"  · 事件{alloc['me_index']}《{alloc['title']}》（应分配章节：{alloc['s']}-{alloc['e']}，共 {alloc['e']-alloc['s']+1} 章，支撑 ec={alloc['ec']} 章）\n"
        f"    概要：{str(_me.get('summary',''))[:300]}\n"
        f"    ·人物：{_me.get('characters','')}\n"
        f"    ·事件：{_me.get('events','')}\n"
        f"    ·时间：{_me.get('time','')}\n"
        f"    ·地点：{_me.get('location','')}\n"
        f"    ·境界：{_me.get('realm_change','')}\n"
        f"    ·年龄：{_me.get('age_change','')}"
        + (f"\n    埋：{_me.get('bury','')}" if _me.get('bury') else '')
        + (f"\n    收：{_me.get('payoff','')}" if _me.get('payoff') else '')
    )
    first_rule = (f"\n本卷第一个子节点的起始章号必须为 {st['start_chapter']}。"
                  + (f"\n承接上一卷卷尾钩子：{st['prev_vol_hook']}" if st['prev_vol_hook'] else '')) if is_first_alloc else ''
    last_rule = (f"\n本卷最后一个子节点必须埋下卷尾钩子，承接本卷 ending_hook：{st['target_ending'][:120]}") if is_last_alloc else ''
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
- 卷尾钩子：{st['target_ending'] or '（无）'}
【世界观设定】{st['worldbuilding_ctx'] or '（暂无）'}
【核心规则】{st['key_rules_ctx'] or '（暂无）'}
【人物档案】{st['characters_ctx'] or '（暂无）'}

【本次生成的 main_event 详情】（请严格按此事件展开为 3-6 个子节点，子节点 chapters 必须严格卡在 {alloc['s']}-{alloc['e']} 区间内）：
{main_block}{first_rule}{last_rule}

请为第 {st['volume_index']} 卷 main_event.{alloc['me_index']}《{alloc['title']}》设计子节点，所有子节点 chapters 严格卡在 {alloc['s']}-{alloc['e']} 区间。"""


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
    """start 任务：按 main_event 逐段生成，段与段之间空出 0.35s，逐段写入 job.nodes。"""
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
        for i, alloc in enumerate(allocs):
            if job.get('_cancel'):
                break
            user_prompt = _build_user_prompt(st, alloc, i == 0, i == n - 1)
            alloc_title = alloc.get('title') or ''
            me_idx = alloc.get('me_index') or (i + 1)
            job['current_segment'] = alloc_title or f'事件{me_idx}'
            job['message'] = f'正在生成子节点：{alloc_title or "事件" + str(me_idx)}…'
            content, err = _call_llm(
                [{'role': 'system', 'content': sys_prompt}, {'role': 'user', 'content': user_prompt}],
                max_tokens=0, temperature=0.65, retry_count=2, timeout=180,
            )
            if err:
                job['error'] = f'事件“{alloc_title or me_idx}”生成失败：{err}'
                break
            parsed, jerr = _extract_json_from_llm(content, expect='object')
            nodes = []
            if not jerr and isinstance(parsed, dict):
                nodes = parsed.get('nodes') or []
                if not isinstance(nodes, list):
                    nodes = []
            elif jerr:
                job['error'] = f'事件“{alloc_title}”JSON解析失败：{jerr}'
                break
            add_count = 0
            for nd in nodes:
                if not isinstance(nd, dict):
                    continue
                nd['main_event_index'] = me_idx
                index_seq += 1
                nd['index'] = index_seq if not nd.get('index') else nd['index']
                new_nodes.append(nd)
                add_count += 1
            job['nodes'] = list(job.get('nodes') or []) + list(new_nodes[-add_count:] if add_count else [])
            job['done'] = i + 1
            _time.sleep(0.35)
        if job.get('error') is None:
            job['nodes'] = sorted(job.get('nodes') or [], key=lambda x: _node_sort_key(x))
            job['state'] = 'done'
            job['message'] = f'已完成，共生成 {len(job.get("nodes") or [])} 个情节节点。'
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
    from app import AuthToken
    from datetime import datetime, timezone
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not token:
        token = request.args.get('token', '')
    if not token:
        return None, '请先登录'
    at = AuthToken.query.filter_by(token=token).first()
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