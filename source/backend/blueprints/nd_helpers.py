"""节点设计师 · 续会工具与卡片聚合门禁（从 chat_collab_bp.py 拆出）。

背景：节点设计师生成整卷 50 章节点是长流式输出（4-10 分钟），中途断连/超时/
模型忘吐卡片是 P0 高频事故。本模块承载全部"断点续会"硬能力：
  1. 续会指令识别：_is_nd_continue（纯"继续/接着"短句 + 前端合成的"继续节点设计…"）
  2. 进度解析：_parse_last_chapter_from_text / _parse_volume_index_from_text
     （双路：优先 SAVE_PLOT 卡片 JSON 里的 nodes.chapters，正则兜底）
  3. 状态持久化：_nd_save/_load/_clear_state（独立 node_designer_state 小表）
     ⚠️ ai_sessions ORM 没有 meta_json 列，写 session.meta_json 会被 SQLAlchemy
     静默忽略（和圆桌 roundtable_state 踩过的同一个坑），必须走独立小表。
  4. 终极兜底：_nd_collect_all_save_plot_volumes + _nd_build_full_volume_card
     （模型没给整卷合并卡时，从 state 累计卡片 + 当前输出聚合出全卷合并版）
  5. 续会注入：_nd_build_continue_user_injection（把断点进度转成续会 prompt）

对 parse_cards 的依赖用函数内延迟导入（chat_collab_bp 顶层 import 本模块，
反向顶层 import 会循环；parse_cards 是纯文本解析，运行时导入无副作用）。
使用方：blueprints/general_chat.py: chat_general 的 node_designer 分支。
"""
from __future__ import annotations

import json
import re

_ND_STATE_KEY = 'node_designer_state'
_ND_STATE_TABLE = 'node_designer_state'

_ND_CONTINUE_HINTS = ('继续', '接着', '续', '往下', '没写完', '接着生成', '继续生成', '继续写', '接着写')
_ND_FULL_RE = re.compile(r'^\s*(?:继续|接着|续会?|往下(?:生成|写)?|没写完|(?:继续|接着|继续吧|接着吧)(?:生成|节点|写|出)?|(?:节点|节点设计)\s*(?:继续|接着))\s*[。.!！，,？?]*\s*$')
# 前端 ChatPanel 合成的续会指令前缀（继续按钮/一键继续构造的 prompt，形如
# 「继续节点设计。当前卷：第X卷。已完成：…」）——必须识别为续会，否则会被当成
# 新请求重置 last_ch=0 → 整卷从头重生成（P0 事故）
_ND_FRONTEND_CONTINUE_RE = re.compile(r'^(?:继续|接着|往下)\s*节点设计')

# 明确指令"第N卷"启动（命中就视为新区任务 → 清旧续会状态）
_ND_NEW_RE = re.compile(r'第\s*(\d+)\s*卷')
# 章号提取（用于从AI输出解析 last_ch）
_ND_CHAPTER_RE = re.compile(r'第\s*(\d+)\s*章')
# 卷号提取（用于从AI输出解析 volume_index）
_ND_VOLUME_RE = re.compile(r'第\s*(\d+)\s*卷')


def _is_nd_continue(msg: str) -> bool:
    """节点设计师「继续」指令识别：
    ① 纯「继续/接着/往下…」短句（和圆桌同口径，避免误抢普通创作追问）
    ② 前端合成的续会指令：以「继续/接着/往下 节点设计」开头（ChatPanel 继续/一键继续按钮的 prompt）
    """
    m = (msg or '').strip().lstrip('，。,.！!？? ').strip()
    if not m:
        return False
    if _ND_FULL_RE.match(m):
        return True
    if _ND_FRONTEND_CONTINUE_RE.match(m):
        return True
    # 宽松版：开头带"继续/接着"且整句极短（≤12字），且不包含明确的新卷/改某章指令
    if any(m.startswith(h) for h in ('继续', '接着', '往下')) and len(m) <= 12:
        if not _ND_NEW_RE.search(m) and '第' not in m.replace('继续', '').replace('接着', ''):
            # 避免"继续写第3卷第25章"被误判成纯续会（这种是指定章修改，走普通追问）
            # 这里用更安全的：若句子含"卷"字且不是纯继续 → 不判定
            if '卷' not in m and '章' not in m:
                return True
    return False


def _is_nd_new_volume_request(msg: str) -> int | None:
    """若用户消息明确带"第N卷"+"节点设计/情节节点/设计情节"关键词 → 返回卷号 N（启动新区任务）。

    两类误判必须排除（否则会清掉续会进度 → 整卷重生成）：
      ① 前端合成的续会指令（「继续节点设计。当前卷：第X卷。…」）
      ② 含"第X章"的章节级修改（如"继续写第3卷第25章"）
    多个卷号取最后一个（"第1卷写完了，设计第2卷"→2）。
    """
    if not msg:
        return None
    if _ND_FRONTEND_CONTINUE_RE.match((msg or '').strip().lstrip('，。,.！!？? ').strip()):
        return None
    matches = _ND_NEW_RE.findall(msg)
    if not matches:
        return None
    if _ND_CHAPTER_RE.search(msg):
        return None
    vi = int(matches[-1])
    if vi < 1 or vi > 99:
        return None
    keywords = ('节点', '情节', '大纲', '设计', '生成', '剧情', '写')
    if any(k in msg for k in keywords):
        return vi
    return None


def _lazy_parse_cards(text: str) -> list:
    """延迟导入 chat_collab_bp.parse_cards（避免顶层循环导入，运行时单向可达）。"""
    try:
        from blueprints.chat_collab_bp import parse_cards
        return parse_cards(text) if callable(parse_cards) else []
    except Exception:
        return []


def _parse_last_chapter_from_text(text: str) -> int:
    """从 AI 已输出文本里解析出出现过的最大章号；找不到返回 0。"""
    if not text:
        return 0
    # 优先取 CARD:SAVE_PLOT JSON 里的节点 chapters 最大号（更准）
    try:
        cards = _lazy_parse_cards(text)
        for c in cards:
            if not isinstance(c, dict) or c.get('type') != 'SAVE_PLOT':
                continue
            content = c.get('content') or ''
            if content.startswith('['):
                try:
                    arr = json.loads(content)
                    if isinstance(arr, list):
                        max_c = 0
                        for v in arr:
                            if not isinstance(v, dict):
                                continue
                            nodes = v.get('nodes')
                            if not isinstance(nodes, list):
                                continue
                            for n in nodes:
                                chs = n.get('chapters')
                                if isinstance(chs, list) and chs:
                                    for x in chs:
                                        if isinstance(x, int) and 1 <= x <= 9999 and x > max_c:
                                            max_c = x
                                elif isinstance(chs, int) and 1 <= chs <= 9999 and chs > max_c:
                                    max_c = chs
                        if max_c > 0:
                            return max_c
                except Exception:
                    pass
    except Exception:
        pass
    # 兜底：正则扫「第X章」最大号（≥1才计数，避免"第一章…"的数字写法抓不到）
    mx = 0
    for mm in _ND_CHAPTER_RE.finditer(text):
        try:
            n = int(mm.group(1))
            if 1 <= n <= 9999 and n > mx:
                mx = n
        except Exception:
            pass
    return mx


def _parse_volume_index_from_text(text: str) -> int | None:
    if not text:
        return None
    for mm in _ND_VOLUME_RE.finditer(text):
        try:
            n = int(mm.group(1))
            if 1 <= n <= 99:
                return n
        except Exception:
            pass
    return None


def _nd_global_chapter_to_local(ch, vi, cpv) -> int:
    """把全书全局连续章号转成本卷卷内章号（1..cpv）；不在本卷区间返回 0。

    节点设计师的章号是全书全局连续的（第 vi 卷覆盖 [(vi-1)*cpv+1, vi*cpv]），
    而续会 state 里的 last_ch 必须存卷内号（0=未开始，cpv=整卷完成），否则
    第 2 卷起会把全局号混进 last_ch → 「继续」误判已完成整卷 / 跳下一卷。
    """
    try:
        ch = int(ch)
        vi = int(vi) if vi else 1
        cpv = int(cpv) if cpv else 50
    except (TypeError, ValueError):
        return 0
    if cpv < 1 or vi < 1:
        return 0
    local = ch - (vi - 1) * cpv
    return local if 1 <= local <= cpv else 0


def _nd_ind_connect():
    """复用圆桌的独立连接工厂（独立于请求 db.session，不受回滚/GeneratorExit 影响；运行时导入避免循环）。"""
    from blueprints.chat_collab_bp import _rt_ind_connect
    return _rt_ind_connect()


def _nd_ind_ensure_table(ind_sess):
    """幂等建 node_designer_state 小表（session_id PK + state_json 全文 + updated_at）。"""
    from sqlalchemy import text as _t
    ind_sess.execute(_t(f"""
        CREATE TABLE IF NOT EXISTS {_ND_STATE_TABLE} (
            session_id VARCHAR(36) PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT
        )
    """))
    ind_sess.commit()


def _nd_save_state(session, db, state: dict) -> None:
    """把节点设计师进度写入独立 node_designer_state 小表（真持久化）。

    血泪：ai_sessions ORM 没有 meta_json 列 → 旧实现写 session.meta_json 内存属性
    被 SQLAlchemy 静默忽略 → 续会状态从没落过盘 → 「继续」解析不到进度就整卷重生成。
    方案照抄圆桌 roundtable_state：独立连接 + 独立小表 upsert，不受请求回滚影响。
    """
    sid = str(getattr(session, 'id', '') or '').strip()
    if not sid or not isinstance(state, dict):
        return
    try:
        from datetime import datetime, timezone
        _eng, _SM = _nd_ind_connect()
        try:
            _s = _SM()
            try:
                _nd_ind_ensure_table(_s)
                from sqlalchemy import text as _t
                _json = json.dumps(state, ensure_ascii=False)
                _ts = datetime.now(timezone.utc).isoformat()
                # SQLite/PG 都支持的 upsert：先 UPDATE，受影响=0 再 INSERT
                _up = _s.execute(
                    _t(f"UPDATE {_ND_STATE_TABLE} SET state_json = :sj, updated_at = :ts WHERE session_id = :sid"),
                    {'sj': _json, 'ts': _ts, 'sid': sid})
                if getattr(_up, 'rowcount', 0) == 0:
                    _s.execute(
                        _t(f"INSERT INTO {_ND_STATE_TABLE} (session_id, state_json, updated_at) VALUES (:sid, :sj, :ts)"),
                        {'sid': sid, 'sj': _json, 'ts': _ts})
                _s.commit()
            finally:
                try:
                    _s.close()
                except Exception:
                    pass
        finally:
            try:
                _eng.dispose()
            except Exception:
                pass
        # 兼容：同步写内存属性（本请求生命周期内的旧读法仍好使；不是DB列，不落盘）
        try:
            meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
            if not isinstance(meta, dict):
                meta = {}
            meta[_ND_STATE_KEY] = state
            session.meta_json = json.dumps(meta, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        pass


def _nd_load_state(session):
    """读节点设计师进度：优先独立小表；兜底内存 meta_json 属性（同请求生命周期内）。"""
    sid = str(getattr(session, 'id', '') or '').strip()
    if sid:
        try:
            _eng, _SM = _nd_ind_connect()
            try:
                _s = _SM()
                try:
                    _nd_ind_ensure_table(_s)
                    from sqlalchemy import text as _t
                    _row = _s.execute(
                        _t(f"SELECT state_json FROM {_ND_STATE_TABLE} WHERE session_id = :sid LIMIT 1"),
                        {'sid': sid}).fetchone()
                    if _row is not None:
                        try:
                            st = json.loads(str(_row[0]) or 'null')
                        except Exception:
                            st = None
                        if isinstance(st, dict):
                            return st
                finally:
                    try:
                        _s.close()
                    except Exception:
                        pass
            finally:
                try:
                    _eng.dispose()
                except Exception:
                    pass
        except Exception:
            pass
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict):
            st = meta.get(_ND_STATE_KEY)
            return st if isinstance(st, dict) else None
    except Exception:
        pass
    return None


def _nd_clear_state(session, db) -> None:
    """清掉节点设计师续会进度（明确启动"第N卷"新任务时调用）。"""
    sid = str(getattr(session, 'id', '') or '').strip()
    if sid:
        try:
            _eng, _SM = _nd_ind_connect()
            try:
                _s = _SM()
                try:
                    _nd_ind_ensure_table(_s)
                    from sqlalchemy import text as _t
                    _s.execute(_t(f"DELETE FROM {_ND_STATE_TABLE} WHERE session_id = :sid"), {'sid': sid})
                    _s.commit()
                finally:
                    try:
                        _s.close()
                    except Exception:
                        pass
            finally:
                try:
                    _eng.dispose()
                except Exception:
                    pass
        except Exception:
            pass
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict) and _ND_STATE_KEY in meta:
            del meta[_ND_STATE_KEY]
            session.meta_json = json.dumps(meta, ensure_ascii=False)
    except Exception:
        pass


def _nd_extract_save_plot_vols(cards: list) -> list[dict]:
    """从已解析的卡片列表（parse_cards 的返回值）里抽取 SAVE_PLOT 的 volume dict 列表。"""
    vols: list[dict] = []
    if not isinstance(cards, list):
        return vols
    for c in cards:
        if not isinstance(c, dict) or c.get('type') != 'SAVE_PLOT':
            continue
        content = c.get('content') or ''
        if not str(content).startswith('['):
            continue
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                vols.extend(v for v in arr if isinstance(v, dict))
        except Exception:
            pass
    return vols


def _nd_merge_state_vols(prev_vols, new_vols) -> list[dict]:
    """把两批 volume dict 按 volume_index 合并（nodes 按章号 ch_map 合并、新覆盖旧），
    作为 state['vols'] 持久化——历史消息里的卡片 content 落盘时会被截断（PG 安全线），
    续会合并兜底必须靠这份累计数据，不能依赖会话历史里的卡片。"""
    try:
        from node_design_bp import _parse_chapters_field
    except Exception:
        return [v for v in (prev_vols or []) if isinstance(v, dict)]
    merged: dict[int, dict] = {}
    order: list[int] = []
    for v in list(prev_vols or []) + list(new_vols or []):
        if not isinstance(v, dict):
            continue
        vi = v.get('volume_index')
        if not isinstance(vi, int) or not (1 <= vi <= 99):
            # 容错：volume_id 是数字也认
            try:
                vi = int(str(v.get('volume_id') or '').strip() or '0')
            except Exception:
                vi = 0
            if not (1 <= vi <= 99):
                continue
            v = dict(v)
            v['volume_index'] = vi
        cur = merged.get(vi)
        if cur is None:
            cur = {'volume_index': vi}
            merged[vi] = cur
            order.append(vi)
        # 卷级字段：非空才覆盖（新值优先）
        for k in ('volume', 'volume_id', 'volume_title', 'summary', 'main_plot', 'core_conflict', 'ending_hook'):
            nv = v.get(k)
            if isinstance(nv, str) and nv.strip():
                cur[k] = nv
        if isinstance(v.get('key_events'), list) and v['key_events']:
            cur['key_events'] = list(v['key_events'])
        if isinstance(v.get('chapter_count'), int) and v.get('chapter_count'):
            cur['chapter_count'] = v['chapter_count']
        # nodes 按章号合并（新覆盖旧；区间展开成单章，去重粒度=章）
        nodes = v.get('nodes')
        if isinstance(nodes, list) and nodes:
            ch_map: dict[int, dict] = {}
            for src in (cur.get('nodes') or [], nodes):
                for n in src:
                    if not isinstance(n, dict):
                        continue
                    chs = _parse_chapters_field(n.get('chapters'))
                    if not chs:
                        continue
                    a, b = chs
                    if a > b:
                        a, b = b, a
                    for ch in range(a, b + 1):
                        cp = dict(n)
                        cp['chapters'] = ch
                        ch_map[ch] = cp
            if ch_map:
                cur['nodes'] = [ch_map[k] for k in sorted(ch_map.keys())]
    return [merged[vi] for vi in order]


def _nd_collect_all_save_plot_volumes(session_history: list, current_text: str, state_vols: list | None = None, vi: int | None = None, cpv: int | None = None) -> tuple[list[dict], int | None, int | None]:
    """从 state 累计卡片 + 当前 AI 输出 complete + 历史会话里，搜集所有出现过的 SAVE_PLOT 卡片的 volume 对象。
    （历史会话里落盘的卡片 content 会被截断到 120 字，JSON 基本解析不出——
    真正可靠的数据源是 state['vols'] 累计和当前 complete 里的卡片。）
    【优化】模型不再输出 SAVE_PLOT 卡片，新增：从 current_text 可读文本直接解析节点。
    返回 ([volume_dict,...], detected_vi, detected_cpv)。"""
    vols: list[dict] = []
    # 0) state 累计的 vols（优先：完整、未截断；排在最前，让后面的新卡片覆盖旧值）
    if isinstance(state_vols, list):
        vols.extend(v for v in state_vols if isinstance(v, dict))
    # 1) 当前 complete 里的 cards
    try:
        for c in _lazy_parse_cards(current_text):
            if not isinstance(c, dict) or c.get('type') != 'SAVE_PLOT':
                continue
            content = c.get('content') or ''
            if content.startswith('['):
                try:
                    arr = json.loads(content)
                    if isinstance(arr, list):
                        vols.extend(v for v in arr if isinstance(v, dict))
                except Exception:
                    pass
    except Exception:
        pass
    # 1.5) 【优化】从当前 complete 的可读流式文本解析节点（模型不再输出 SAVE_PLOT 卡片）
    try:
        _vi_for_parse = vi or _parse_volume_index_from_text(current_text) or 1
        _cpv_for_parse = cpv or 50
        _parsed = _nd_parse_readable_text_to_volume(current_text, _vi_for_parse, _cpv_for_parse)
        if _parsed:
            vols.append(_parsed)
    except Exception:
        pass
    # 2) 历史会话里所有 assistant.cards 里的 SAVE_PLOT
    try:
        if isinstance(session_history, list):
            for m in session_history:
                if not isinstance(m, dict) or m.get('role') != 'assistant':
                    continue
                cards_list = m.get('cards') or []
                if isinstance(cards_list, list):
                    for c in cards_list:
                        if not isinstance(c, dict):
                            continue
                        if c.get('type') != 'SAVE_PLOT':
                            continue
                        content = c.get('content') or ''
                        if content.startswith('['):
                            try:
                                arr = json.loads(content)
                                if isinstance(arr, list):
                                    vols.extend(v for v in arr if isinstance(v, dict))
                            except Exception:
                                pass
    except Exception:
        pass
    # 3) 解析 volume_index / cpv
    vi_set: set[int] = set()
    cpv_set: set[int] = set()
    for v in vols:
        vi = v.get('volume_index')
        if isinstance(vi, int) and 1 <= vi <= 99:
            vi_set.add(vi)
        cc = v.get('chapter_count')
        if isinstance(cc, int) and 10 <= cc <= 200:
            cpv_set.add(cc)
    detected_vi = next(iter(vi_set)) if len(vi_set) == 1 else None
    detected_cpv = next(iter(cpv_set)) if len(cpv_set) == 1 else None
    return vols, detected_vi, detected_cpv


def _nd_normalize_chapters_to_global(ch_map: dict[int, dict], vi: int, cpv: int) -> dict[int, dict]:
    """把 ch_map 里的章号统一规范化成【全书全局连续章号】。

    背景：节点设计师原始输出 / node_design_bp 卡片 / apply_card 采纳链路
    （_repair_volume_nodes_safe 用 start_ch=1+(vi-1)*cpv 推导区间）一律使用
    全书全局章号。_nd_build_*_card 之前误把全局号转成卷内号(1..cpv)，导致第2卷
    起采纳时节点全部落在修复区间外 → 被替换成占位符 → 落不到剧情分卷。
    这里统一改回全局号。

    判定：若 max(ch) <= cpv 视为卷内号 → 加 (vi-1)*cpv；若 min(ch) >= 全局起点
    视为全局号 → 保持；混合情况按单章判定。"""
    if not ch_map:
        return ch_map
    g_start = 1 + (vi - 1) * cpv
    g_end = vi * cpv
    keys = list(ch_map.keys())
    mx = max(keys)
    mn = min(keys)
    # 全部卷内号（1..cpv）→ 转全局
    if mx <= cpv:
        offset = (vi - 1) * cpv
        new_map = {}
        for k, nd in ch_map.items():
            g = k + offset
            nd['chapters'] = g
            new_map[g] = nd
        return new_map
    # 全部全局号 → 保持
    if mn >= g_start:
        return ch_map
    # 混合：逐章判定
    new_map = {}
    for k, nd in ch_map.items():
        if g_start <= k <= g_end:
            g = k
        elif 1 <= k <= cpv:
            g = k + (vi - 1) * cpv
        else:
            g = k  # 越界，保持原值由后续修复器处理
        nd['chapters'] = g
        new_map[g] = nd
    return new_map


def _nd_build_full_volume_card(vols_list: list[dict], vi: int, cpv: int) -> dict | None:
    """把 vols_list 里的所有 volume（可能来自多张续会卡片）按章节号增量合并，构建一张完整的全卷卡片：
      nodes 覆盖全书全局章号 [1+(vi-1)*cpv, vi*cpv]，
      如果所有分段加起来仍然缺章，调用 node_design_bp._repair_nodes_to_one_ch_per_node 补齐。
    返回 SAVE_PLOT 卡片字典 {id, type:'SAVE_PLOT', title, content:JSON 字符串} 或 None。"""
    try:
        from node_design_bp import _repair_nodes_to_one_ch_per_node, _parse_chapters_field
        # 汇总所有 nodes，按章节号做 {ch: node} 合并（后者覆盖前者）
        ch_map: dict[int, dict] = {}
        summary_holder: dict = {}
        main_plot = core_conflict = ending_hook = ''
        key_events = []
        vol_title = f'第{vi}卷'
        vol_id = str(vi)
        for v in vols_list:
            if not isinstance(v, dict):
                continue
            # 只合并目标卷：state 累计里可能混有别的卷，卷级字段/nodes 都不能串卷
            _vvi = v.get('volume_index')
            if not isinstance(_vvi, int) or not (1 <= _vvi <= 99):
                try:
                    _vvi = int(str(v.get('volume_id') or '0') or 0)
                except Exception:
                    _vvi = 0
            if _vvi and _vvi != vi:
                continue
            # 取卷级字段（非空才覆盖）
            if v.get('volume'):
                vol_title = str(v['volume'])
            if v.get('volume_id'):
                vol_id = str(v['volume_id'])
            s = v.get('summary')
            if isinstance(s, str) and len(s) > len(summary_holder.get('summary', '')):
                summary_holder['summary'] = s
            if v.get('main_plot'):
                main_plot = v['main_plot'] or main_plot
            if v.get('core_conflict'):
                core_conflict = v['core_conflict'] or core_conflict
            if v.get('ending_hook'):
                ending_hook = v['ending_hook'] or ending_hook
            if isinstance(v.get('key_events'), list) and len(v['key_events']) >= len(key_events):
                key_events = list(v['key_events'])
            nodes = v.get('nodes')
            if not isinstance(nodes, list):
                continue
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                chs = _parse_chapters_field(n.get('chapters'))
                if not chs:
                    continue
                a, b = chs
                for ch in range(a, b + 1):
                    cp = dict(n)
                    cp['chapters'] = ch
                    ch_map[ch] = cp
        # 统一规范化为全书全局章号（与 node_design_bp / apply_card 采纳链路一致）
        ch_map = _nd_normalize_chapters_to_global(ch_map, vi, cpv)
        # 补齐并修复 A+C：用全书全局区间 [g_start, g_end]
        g_start = 1 + (vi - 1) * cpv
        g_end = vi * cpv
        nodes_flat = list(ch_map.values())
        repaired, _ = _repair_nodes_to_one_ch_per_node(nodes_flat, g_start, g_end, vi, 0)
        final_vol = {
            'volume_id': vol_id,
            'volume': vol_title,
            'volume_index': vi,
            'volume_title': vol_title,
            'summary': summary_holder.get('summary', ''),
            'main_plot': main_plot,
            'core_conflict': core_conflict,
            'key_events': key_events,
            'ending_hook': ending_hook,
            'chapter_count': cpv,
            'start_chapter': g_start,
            'end_chapter': g_end,
            'nodes': repaired,
        }
        content = json.dumps([final_vol], ensure_ascii=False)
        title = f'第{vi}卷情节节点（{cpv}个）· 全卷合并版统一采纳卡片'
        card_id = 'SAVE_PLOT_' + str(vi) + '_' + str(int(__import__('time').time()))
        return {'id': card_id, 'type': 'SAVE_PLOT', 'title': title, 'content': content, 'target': 'plot'}
    except Exception:
        return None


def _nd_build_partial_volume_card(vols_list: list[dict], vi: int, cpv: int) -> dict | None:
    """构建半截 SAVE_PLOT 卡片（仅含已生成的节点，不补齐占位章）。
    用于模型未输出卡片、且全卷未完成时，让前端显示「分批临时保存」按钮。
    与 _nd_build_full_volume_card 的区别：不调用 _repair_nodes_to_one_ch_per_node
    补齐缺失章，nodes 只包含实际解析到的节点，前端据此判定为半截卡片。"""
    try:
        from node_design_bp import _parse_chapters_field
        ch_map: dict[int, dict] = {}
        vol_title = f'第{vi}卷'
        for v in vols_list:
            if not isinstance(v, dict):
                continue
            _vvi = v.get('volume_index')
            if not isinstance(_vvi, int) or not (1 <= _vvi <= 99):
                try:
                    _vvi = int(str(v.get('volume_id') or '0') or 0)
                except Exception:
                    _vvi = 0
            if _vvi and _vvi != vi:
                continue
            if v.get('volume'):
                vol_title = str(v['volume'])
            nodes = v.get('nodes')
            if not isinstance(nodes, list):
                continue
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                chs = _parse_chapters_field(n.get('chapters'))
                if not chs:
                    continue
                a, b = chs
                for ch in range(a, b + 1):
                    cp = dict(n)
                    cp['chapters'] = ch
                    ch_map[ch] = cp
        if not ch_map:
            return None
        # 统一规范化为全书全局章号（与采纳链路一致，避免第2卷起节点全部越界被替换成占位符）
        ch_map = _nd_normalize_chapters_to_global(ch_map, vi, cpv)
        nodes_sorted = [ch_map[k] for k in sorted(ch_map.keys())]
        final_vol = {
            'volume_id': str(vi),
            'volume': vol_title,
            'volume_index': vi,
            'volume_title': vol_title,
            'summary': '',
            'main_plot': '',
            'core_conflict': '',
            'key_events': [],
            'ending_hook': '',
            'chapter_count': cpv,
            'start_chapter': 1 + (vi - 1) * cpv,
            'end_chapter': vi * cpv,
            'nodes': nodes_sorted,
        }
        content = json.dumps([final_vol], ensure_ascii=False)
        done = len(nodes_sorted)
        title = f'第{vi}卷情节节点（{done}/{cpv}）· 中途进度快照'
        card_id = 'SAVE_PLOT_' + str(vi) + '_' + str(int(__import__('time').time()))
        return {'id': card_id, 'type': 'SAVE_PLOT', 'title': title, 'content': content, 'target': 'plot'}
    except Exception:
        return None


def _nd_build_continue_user_injection(state: dict) -> str:
    """命中续会时，拼一段『已完成第1~last_ch章，从last_ch+1开始不要重复』的用户消息补充上下文。
    同时按区间判断：中途段→禁止吐SAVE_PLOT卡片；收尾段（next_ch+剩余<≈1.2*cpv 保守判断=大概率写得完尾）→ 要求吐全卷合并版卡片。
    state['last_ch'] 是卷内号，下面统一换算成全书全局连续章号，避免模型把「卷内第1章」与「全书全局章号」混用。"""
    vi = int(state.get('volume_index') or 1)
    cpv = int(state.get('cpv') or 50)
    last_ch = int(state.get('last_ch') or 0)
    total = cpv
    # 全书全局连续章号（第 vi 卷覆盖 [(vi-1)*total+1, vi*total]）
    g_start = 1 + (vi - 1) * total
    g_end = vi * total
    g_done = g_start + last_ch - 1   # 已写到的最后一章（全书全局号）
    next_ch = last_ch + 1            # 卷内下一章
    g_next = g_done + 1              # 全书全局下一章
    if next_ch > total:
        # 已完成整卷还继续 → 提示已完成，如需修改按章节号改
        return ("\n【系统续会上下文】作者说「继续」，但本卷进度记录显示："
                f"第{vi}卷（共{total}章，全书第{g_start}~{g_end}章）已经完成到本卷第{last_ch}章=整卷写完。"
                f"请直接告诉作者：「这一卷{total}章已经全部设计完成啦。需要改某一章直接对我说『第X章改XXX』；要开新卷直接说『第N卷 节点设计』。」"
                "不要再重复输出已写完的章节节点，也不要再输出全卷卡片。\n")
    # 判定是不是收尾段：剩余章节数 ≤ 30（约占 cpv 60%以内），或 last_ch ≥ total*0.7
    remaining = total - last_ch
    is_final_leg = (remaining <= 30) or (last_ch >= int(total * 0.7))
    if not is_final_leg:
        # 中途段门禁
        g_est_end = min(g_done + 30, g_end)
        return (
            f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
            f"\n· 当前卷：第{vi}卷（共{total}章，全书章号 {g_start}~{g_end}）"
            f"\n· 已输出完成：全书第{g_start}章~第{g_done}章（本卷内第{1}~{last_ch}章，共{last_ch}个情节子节点）"
            f"\n· 本轮只输出：第{g_next}章~预计第{g_est_end}章左右（写不完没关系，下一轮作者发『继续』会从你写到的最后一章接着续）"
            f"\n· ❗门禁·中途段：本轮不会写到本卷最后一章（全书第{g_end}章），属于中途进度段："
            f"\n   · ✅ 本轮续写的所有节点写完后，只需要在末尾写一行中文进度快照："
            f"\n     「✅ 中途进度快照：已完成第{g_next}章~第<本轮实际写到的最后一章号>章，累计完成<N>/{total}。随时发『继续』接着生成。」"
            f"\n· ❗铁律：绝对不要重复写 第{g_start}章~第{g_done}章 的任何内容、标题、字段、节点；任何形式的复述都不允许。"
            f"\n· 输出顺序仍然按：章节号递增 → 开场白可省略或只说一句「继续第{g_next}章起节点」即可，不再啰嗦卷级设定。"
            f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
            f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
        )
    # 收尾段门禁
    return (
        f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
        f"\n· 当前卷：第{vi}卷（共{total}章，全书章号 {g_start}~{g_end}）"
        f"\n· 已输出完成：全书第{g_start}章~第{g_done}章（本卷内第{1}~{last_ch}章，共{last_ch}个情节子节点）"
        f"\n· 本轮必须只输出：第{g_next}章~第{g_end}章（剩余 {remaining} 个情节子节点）"
        f"\n· ❗门禁·收尾段：本轮会写到本卷最后一章（全书第{g_end}章），请务必完整写到末尾；全卷写完后："
        f"\n   · ✅ 只输出一行总结：「✅ 完成：共{total}章，{total}个节点，单章单节点+资源滚动+人物关系门禁合格」即可。"
        f"\n· ❗铁律：写第{g_next}~第{g_end}章正文节点时，仍然不许复述前面已写章节。"
        f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
        f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
    )


# ============================================================================
# 可读流式文本 → 结构化节点 解析器
# 背景：节点设计师原本要求模型在写完所有章节后，再把同样内容用 JSON SAVE_PLOT
# 卡片输出一遍（双倍 token + 聊天界面 JSON 刷屏）。现在改为：模型只输出可读流式
# 文本，由本解析器把文本转成结构化 nodes，后端自动组装 SAVE_PLOT 卡片写库。
# ============================================================================

# 章节块分隔：【第N章】 / 第N章 / 章节：第N章 等
_ND_CHAPTER_SPLIT_RE = re.compile(
    r'(?:^|\n)\s*(?:【)?\s*第\s*(\d+)\s*章\s*(?:】)?\s*(?:\n|$|（)'
)

# 行首项目符号（节点设计师可读输出为「- 字段：值」逐行列表）
_ND_LINE_BULLET_RE = re.compile(r'^\s*(?:[-•*·▪◦]\s*)+')

# 字段标签正则（按优先级排序，长的在前避免短标签误匹配）
_ND_FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ('chapter_beats', re.compile(r'chapter_beats\s*[:：]\s*', re.IGNORECASE)),
    ('resources_gained', re.compile(r'【?\s*资源(?:\s*[·•]\s*)?(?:本章)?\s*获得(?:\s*resources_gained)?\s*】?\s*[:：]\s*', re.IGNORECASE)),
    ('resources_used', re.compile(r'【?\s*资源(?:\s*[·•]\s*)?(?:本章)?\s*消耗(?:\s*resources_used)?\s*】?\s*[:：]\s*', re.IGNORECASE)),
    ('total_resources_owned', re.compile(r'【?\s*总资源(?:\s*total_resources_owned)?\s*】?\s*[:：]\s*', re.IGNORECASE)),
    ('main_event', re.compile(r'【?\s*所属大事件\s*】?\s*[:：]\s*|main_event\s*[:：]\s*', re.IGNORECASE)),
    # 人物：容忍标签里夹「（人物关系必写）」等括号注解，否则整个字段漏匹配 → 人物丢成默认「主角」，并污染 conflict
    ('characters', re.compile(r'人物(?:characters)?(?:[（(][^）)]*[）)])?\s*[:：]\s*', re.IGNORECASE)),
    ('summary', re.compile(r'摘要(?:summary)?\s*[:：]\s*', re.IGNORECASE)),
    ('conflict', re.compile(r'冲突(?:conflict)?\s*[:：]\s*', re.IGNORECASE)),
    ('location', re.compile(r'地点(?:location)?\s*[:：]\s*', re.IGNORECASE)),
    ('time', re.compile(r'时间(?:time)?\s*[:：]\s*', re.IGNORECASE)),
    ('foreshadowing', re.compile(r'伏笔(?:\s*/\s*回收)?\s*[:：]\s*|foreshadowing\s*[:：]\s*', re.IGNORECASE)),
    ('type', re.compile(r'类型\s*[:：]\s*|type\s*[:：]\s*', re.IGNORECASE)),
    ('title', re.compile(r'标题\s*[:：]\s*|title\s*[:：]\s*', re.IGNORECASE)),
    ('chapters_field', re.compile(r'章节\s*[:：]\s*|chapters?\s*[:：]\s*', re.IGNORECASE)),
]


def _nd_split_into_chapter_blocks(text: str) -> list[tuple[int, str]]:
    """把可读文本按章节切分成 [(chapter_num, block_text), ...]。"""
    if not text:
        return []
    blocks: list[tuple[int, str]] = []
    # 找所有章节标记的位置
    matches = list(_ND_CHAPTER_SPLIT_RE.finditer(text))
    for i, m in enumerate(matches):
        ch = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append((ch, block))
    return blocks


def _nd_split_location_time_line(val: str) -> str:
    """把「地点location / 时间time：X / Y」合并行里的取值部分拆成两行。"""
    val = (val or '').strip()
    parts = re.split(r'\s*/\s*', val, maxsplit=1)
    loc = parts[0].strip() if parts else ''
    tim = parts[1].strip() if len(parts) > 1 else ''
    return f'地点location：{loc}\n时间time：{tim}'


def _nd_extract_fields_from_block(block: str) -> dict:
    """从单个章节块文本里逐行提取各字段值。

    节点设计师的可读输出是「- 字段：值」逐行列表（字段值可能跨行续写）。
    旧实现用「相邻字段标签的位置差」截值，会把下一字段行的「- 」项目符号
    吃进上一字段尾部（title 变成「初入宗门\\n-」、characters 被污染），导致
    人物/主要事件/资源/伏笔等字段落地失真。改为逐行识别字段标签：
      行首（去掉项目符号后）命中字段标签 → 新建字段；否则作为上一字段续行。
    """
    if not block:
        return {}
    # 归一化：老格式「地点location / 时间time：X / Y」合并写在一行 → 拆成两行
    block = re.sub(
        r'地点(?:location)?\s*/\s*时间(?:time)?\s*[:：]\s*([^\n]+)',
        lambda m: _nd_split_location_time_line(m.group(1)),
        block,
    )
    fields: dict[str, str] = {}
    current: str | None = None
    for raw_line in block.split('\n'):
        line = raw_line.strip()
        if not line:
            continue
        line = _ND_LINE_BULLET_RE.sub('', line)
        if not line:
            continue
        matched = False
        for name, pat in _ND_FIELD_PATTERNS:
            m = pat.match(line)
            if m:
                fields[name] = line[m.end():].strip()
                current = name
                matched = True
                break
        if matched:
            continue
        # 命中不了标签 → 当作当前字段的续行（多行 value 拼接）
        if current is not None and current in fields:
            fields[current] = (fields[current] + '\n' + line).strip()
    return fields


def _nd_normalize_chapter_beats(raw: str) -> list[str]:
    """把 chapter_beats 文本规范成字符串数组。"""
    if not raw:
        return []
    # 按换行、分号、斜杠、编号(1. 2. ①②③) 切分
    parts = re.split(r'\n+|[；;]+|/+|\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*|\s*\d+[.、)\]]\s*', raw)
    beats = [p.strip(' 　\t-—·•\t') for p in parts if p.strip(' 　\t-—·•\t')]
    return beats[:10]  # 最多10条


def _nd_normalize_resource_list(raw: str) -> list[str]:
    """把资源文本（获得/消耗）规范成带【类别】前缀的字符串数组。"""
    if not raw or raw.strip() in ('无', '没有', 'none', 'None'):
        return []
    items: list[str] = []
    # 支持「、」分隔的多项资源（模型常把"下品灵石×300、洗髓丹×5"写在一行）
    for ln in re.split(r'\n+|[；;]+', raw):
        s = ln.strip()
        if not s or s in ('无', '没有'):
            continue
        for seg in re.split(r'[、,，]+', s):
            seg = seg.strip()
            if not seg or seg in ('无', '没有'):
                continue
            items.append(seg)
    return items


def _nd_parse_total_resources(raw: str) -> dict:
    """把总资源文本解析成 {钱财:[], 物品:[], 武器法宝:[], 功法能力:[], 其它:[]}。"""
    result = {'钱财': [], '物品': [], '武器法宝': [], '功法能力': [], '其它': []}
    if not raw or raw.strip() in ('无', '没有', 'none', '{}'):
        return result
    # 尝试按类别标签切分
    cat_keys = list(result.keys())
    # 构建类别匹配：【钱财】/钱财：/钱财：
    cat_pat = re.compile(r'【?\s*(钱财|物品|武器法宝|功法|功法能力|其它|其他)\s*】?\s*[:：]?\s*')
    matches = list(cat_pat.finditer(raw))
    if matches:
        for i, m in enumerate(matches):
            cat = m.group(1)
            if cat in ('功法',):
                cat = '功法能力'
            elif cat in ('其他',):
                cat = '其它'
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            seg = raw[start:end].strip()
            for ln in re.split(r'[、，,\n]+', seg):
                s = ln.strip().strip('；;')
                if s and s not in ('无', '没有'):
                    result.setdefault(cat, []).append(s)
    else:
        # 无类别标签，全部归到其它
        for ln in re.split(r'[、，,\n]+', raw):
            s = ln.strip()
            if s and s not in ('无', '没有'):
                result['其它'].append(s)
    return result


def _nd_parse_readable_text_to_volume(text: str, vi: int, cpv: int) -> dict | None:
    """把节点设计师输出的可读流式文本解析成一个 volume dict（含 nodes 数组）。
    返回 None 表示没解析到任何节点。"""
    blocks = _nd_split_into_chapter_blocks(text or '')
    if not blocks:
        return None
    nodes: list[dict] = []
    # 卷级字段（从文本里尽量提取）
    vol_title = f'第{vi}卷'
    vol_summary = ''
    main_plot = ''
    core_conflict = ''
    ending_hook = ''
    key_events: list[str] = []
    for ch, block in blocks:
        fields = _nd_extract_fields_from_block(block)
        # 标题可能在块的第一行（没带"标题："前缀时）
        title = fields.get('title', '')
        if not title:
            # 取块第一行作为标题兜底
            first_line = block.split('\n', 1)[0].strip()
            if first_line and len(first_line) < 80:
                title = first_line
        node = {
            'index': len(nodes) + 1,
            'chapters': ch,
            'type': (fields.get('type', 'M') or 'M').strip()[:3],
            'title': title[:200],
            'summary': (fields.get('summary', '') or '')[:2000],
            'chapter_beats': _nd_normalize_chapter_beats(fields.get('chapter_beats', '')),
            'conflict': (fields.get('conflict', '') or '')[:500],
            'characters': _nd_parse_characters_field(fields.get('characters', '')),
            'resources_gained': _nd_normalize_resource_list(fields.get('resources_gained', '')),
            'resources_used': _nd_normalize_resource_list(fields.get('resources_used', '')),
            'total_resources_owned': _nd_parse_total_resources(fields.get('total_resources_owned', '')),
            'location': (fields.get('location', '') or '')[:200],
            'time': (fields.get('time', '') or '')[:200],
            'foreshadowing': (fields.get('foreshadowing', '') or '')[:500],
            'main_event': (fields.get('main_event', '') or '')[:200],
        }
        # main_event 收集到 key_events
        me = node.get('main_event', '')
        if me and me not in key_events:
            key_events.append(me)
        nodes.append(node)
    if not nodes:
        return None
    return {
        'volume_index': vi,
        'volume': vol_title,
        'volume_id': str(vi),
        'summary': vol_summary,
        'main_plot': main_plot,
        'core_conflict': core_conflict,
        'ending_hook': ending_hook,
        'key_events': key_events,
        'chapter_count': cpv,
        'start_chapter': 1 + (vi - 1) * cpv,
        'end_chapter': vi * cpv,
        'nodes': nodes,
    }


def _nd_parse_characters_field(raw: str) -> list[str]:
    """把人物字段规范成 ['姓名|关系:关系类型', ...]。"""
    if not raw:
        return ['主角|关系:主角']
    # 按顿号、逗号、分号切分
    people = re.split(r'[、，,；;]+', raw)
    result: list[str] = []
    for p in people:
        p = p.strip()
        if not p:
            continue
        # 已有 |关系: 格式
        if '|关系:' in p:
            result.append(p)
            continue
        # 括号格式：姓名(关系:X) 或 姓名(关系类型)
        m = re.match(r'^(.+?)[（(]\s*(?:关?系?\s*[:：]\s*)?([^()）]+)\s*[)）]$', p)
        if m:
            name = m.group(1).strip()
            rel = m.group(2).strip()
            result.append(f'{name}|关系:{rel}')
        else:
            # 纯名字
            result.append(f'{p}|关系:主角')
    return result or ['主角|关系:主角']
