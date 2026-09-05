"""节点设计师 · 续会工具与卡片聚合门禁（从 chat_collab_bp.py 拆出）。

背景：节点设计师生成整卷 50 章节点是长流式输出（4-10 分钟），中途断连/超时/
模型忘吐卡片是 P0 高频事故。本模块承载全部"断点续会"硬能力：
  1. 续会指令识别：_is_nd_continue（"继续/接着"纯续会 vs 指定章修改）
  2. 进度解析：_parse_last_chapter_from_text / _parse_volume_index_from_text
     （双路：优先 SAVE_PLOT 卡片 JSON 里的 nodes.chapters，正则兜底）
  3. 状态持久化：_nd_save/_load/_clear_state（session.meta_json['node_designer_state']）
  4. 终极兜底：_nd_collect_all_save_plot_volumes + _nd_build_full_volume_card
     （模型没给整卷合并卡时，从历史半截卡片聚合出全卷合并版）
  5. 续会注入：_nd_build_continue_user_injection（把断点进度转成续会 prompt）

对 parse_cards 的依赖用函数内延迟导入（chat_collab_bp 顶层 import 本模块，
反向顶层 import 会循环；parse_cards 是纯文本解析，运行时导入无副作用）。
使用方：blueprints/chat_collab_bp.py: chat_general 的 node_designer 分支。
"""
from __future__ import annotations

import json
import re

_ND_STATE_KEY = 'node_designer_state'

_ND_CONTINUE_HINTS = ('继续', '接着', '续', '往下', '没写完', '接着生成', '继续生成', '继续写', '接着写')
_ND_FULL_RE = re.compile(r'^\s*(?:继续|接着|续会?|往下(?:生成|写)?|没写完|(?:继续|接着|继续吧|接着吧)(?:生成|节点|写|出)?|(?:节点|节点设计)\s*(?:继续|接着))\s*[。.!！，,？?]*\s*$')

# 明确指令"第N卷"启动（命中就视为新区任务 → 清旧续会状态）
_ND_NEW_RE = re.compile(r'第\s*(\d+)\s*卷')
# 章号提取（用于从AI输出解析 last_ch）
_ND_CHAPTER_RE = re.compile(r'第\s*(\d+)\s*章')
# 卷号提取（用于从AI输出解析 volume_index）
_ND_VOLUME_RE = re.compile(r'第\s*(\d+)\s*卷')


def _is_nd_continue(msg: str) -> bool:
    """节点设计师「继续」指令识别：和圆桌同口径，避免误抢普通创作追问。"""
    m = (msg or '').strip().lstrip('，。,.！!？? ').strip()
    if not m:
        return False
    if _ND_FULL_RE.match(m):
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
    """若用户消息明确带"第N卷"+"节点设计/情节节点/设计情节"关键词 → 返回卷号 N（启动新区任务）。"""
    if not msg:
        return None
    m = _ND_NEW_RE.search(msg)
    if not m:
        return None
    vi = int(m.group(1))
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


def _nd_save_state(session, db, state: dict) -> None:
    """把节点设计师进度写回 session.meta_json（保留已有键，不影响圆桌/role/ai_config）。"""
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if not isinstance(meta, dict):
            meta = {}
        meta[_ND_STATE_KEY] = state
        session.meta_json = json.dumps(meta, ensure_ascii=False)
        db.session.add(session)
        db.session.commit()
    except Exception:
        pass


def _nd_load_state(session):
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if isinstance(meta, dict):
            st = meta.get(_ND_STATE_KEY)
            return st if isinstance(st, dict) else None
    except Exception:
        pass
    return None


def _nd_clear_state(session, db) -> None:
    try:
        meta = session.meta_json if isinstance(session.meta_json, dict) else json.loads((session.meta_json or None) or '{}')
        if not isinstance(meta, dict):
            meta = {}
        if _ND_STATE_KEY in meta:
            del meta[_ND_STATE_KEY]
            session.meta_json = json.dumps(meta, ensure_ascii=False)
            db.session.add(session)
            db.session.commit()
    except Exception:
        pass


def _nd_collect_all_save_plot_volumes(session_history: list, current_text: str) -> tuple[list[dict], int | None, int | None]:
    """从历史会话 messages + 当前 AI 输出 complete 里，搜集所有出现过的 SAVE_PLOT 卡片的 volume 对象。
    返回 ([volume_dict,...], detected_vi, detected_cpv)。"""
    vols: list[dict] = []
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


def _nd_build_full_volume_card(vols_list: list[dict], vi: int, cpv: int) -> dict | None:
    """把 vols_list 里的所有 volume（可能来自多张续会卡片）按章节号增量合并，构建一张完整的全卷卡片：
      nodes 覆盖 [1, cpv]（按 volume_index=vi 的卷内章序 1..cpv），
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
        # 如果章节都在 vi 卷区间外（全局号），转换到卷内编号 1..cpv
        # 尝试：若所有 ch >= 1+(vi-1)*50 → 按全局号减去偏移
        start_global_guess = 1 + (vi - 1) * cpv
        if ch_map and all(k >= start_global_guess for k in ch_map.keys()):
            new_map = {}
            for g, nd in ch_map.items():
                local = g - (start_global_guess - 1)
                if 1 <= local <= cpv:
                    nd['chapters'] = local
                    new_map[local] = nd
            ch_map = new_map
        # 补齐并修复 A+C
        nodes_flat = list(ch_map.values())
        repaired, _ = _repair_nodes_to_one_ch_per_node(nodes_flat, 1, cpv, vi, 0)
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
            'start_chapter': 1 + (vi - 1) * cpv,
            'end_chapter': vi * cpv,
            'nodes': repaired,
        }
        content = json.dumps([final_vol], ensure_ascii=False)
        title = f'第{vi}卷情节节点（{cpv}个）· 全卷合并版统一采纳卡片'
        card_id = 'SAVE_PLOT_' + str(vi) + '_' + str(int(__import__('time').time()))
        return {'id': card_id, 'type': 'SAVE_PLOT', 'title': title, 'content': content, 'target': 'plot'}
    except Exception:
        return None


def _nd_build_continue_user_injection(state: dict) -> str:
    """命中续会时，拼一段『已完成第1~last_ch章，从last_ch+1开始不要重复』的用户消息补充上下文。
    同时按区间判断：中途段→禁止吐SAVE_PLOT卡片；收尾段（next_ch+剩余<≈1.2*cpv 保守判断=大概率写得完尾）→ 要求吐全卷合并版卡片。"""
    vi = int(state.get('volume_index') or 0)
    cpv = int(state.get('cpv') or 50)
    last_ch = int(state.get('last_ch') or 0)
    next_ch = last_ch + 1
    total = cpv
    if next_ch > total:
        # 已完成整卷还继续 → 提示已完成，如需修改按章节号改
        return ("\n【系统续会上下文】作者说「继续」，但本卷进度记录显示："
                f"第{vi}卷（共{total}章）已经完成到第{last_ch}章=整卷写完。"
                "请直接告诉作者：「这一卷50章已经全部设计完成啦。需要改某一章直接对我说『第X章改XXX』就行。」"
                "不要再重复输出已写完的章节节点。\n")
    # 判定是不是收尾段：剩余章节数 ≤ 30（约占 cpv 60%以内），或 last_ch ≥ total*0.7
    remaining = total - last_ch
    is_final_leg = (remaining <= 30) or (last_ch >= int(total * 0.7))
    if not is_final_leg:
        # 中途段门禁
        return (
            f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
            f"\n· 当前卷：第{vi}卷（共{total}章，章号 {1 + (vi-1)*total}~{vi*total}，卷内编号 {1}~{total}）"
            f"\n· 已输出完成：第{1}章~第{last_ch}章（共{last_ch}个情节子节点）"
            f"\n· 本轮只输出：第{next_ch}章~预计第{min(last_ch+30, total)}章左右（写不完没关系，下一轮作者发『继续』会从你写到的最后一章接着续）"
            f"\n· ❗门禁·中途段：本轮不会写到第{total}章（本卷最后一章），属于中途进度段："
            f"\n   · ❌ 绝对禁止输出任何 [[CARD:SAVE_PLOT|...]] 落地卡片。任何半截卡片都不允许，违者生成不合格。"
            f"\n   · ✅ 本轮续写的所有节点写完后，只需要在末尾写一行中文进度快照："
            f"\n     「✅ 中途进度快照：已完成第{next_ch}章~第<本轮实际写到的最后一章号>章，累计完成<N>/{total}。随时发『继续』接着生成，整卷{total}章全部设计完成后，会给出一张全卷合并版统一采纳卡片。」"
            f"\n· ❗铁律：绝对不要重复写 第1章~第{last_ch}章 的任何内容、标题、字段、节点；任何形式的复述都不允许。"
            f"\n· 输出顺序仍然按：章节号递增 → 开场白可省略或只说一句「继续第{next_ch}章起节点」即可，不再啰嗦卷级设定。"
            f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
            f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
        )
    # 收尾段门禁
    return (
        f"\n【系统续会上下文】作者说「继续」，这是节点设计续会。请严格按如下规则："
        f"\n· 当前卷：第{vi}卷（共{total}章，章号 {1 + (vi-1)*total}~{vi*total}，卷内编号 {1}~{total}）"
        f"\n· 已输出完成：第{1}章~第{last_ch}章（共{last_ch}个情节子节点）"
        f"\n· 本轮必须只输出：第{next_ch}章~第{total}章（剩余 {remaining} 个情节子节点）"
        f"\n· ❗门禁·收尾段：本轮会写到最后一章（第{total}章），请务必完整写到末尾；全卷写完后："
        f"\n   · ❌ 不要输出只包含『第{next_ch}~第{total}章』的半截 SAVE_PLOT 卡片！"
        f"\n   · ✅ 必须输出一张【全卷合并版统一采纳卡片】[[CARD:SAVE_PLOT|...]]："
        f"\n     · nodes 字段必须包含第 1 章 ~ 第 {total} 章的全部 {total} 个情节子节点（把你前面所有续会段已经输出的第1~{last_ch}章节点，和这次新写的第{next_ch}~第{total}章节点，按章节号升序完整整理进这一张卡片）。"
        f"\n     · 任何一个章号对应的节点缺失都不行；差一章=卡片不合格。后端也会从历史分段卡片自动做二次合并兜底，不怕你漏节点。"
        f"\n     · volume_index={vi}、chapter_count={total}、start_chapter={1 + (vi-1)*total}、end_chapter={vi*total}。"
        f"\n· ❗铁律：写第{next_ch}~第{total}章正文节点时，仍然不许复述前面已写章节；只是在最后输出卡片的 JSON 汇总时，把前面所有章节的节点完整合进去。"
        f"\n· 爽点/五幕/节奏仍然按整卷规则对齐，但只写剩余章。"
        f"\n· 仍然遵守 A+C 铁律：单章单节点、无重叠无跳章、chapters 严格落在卷区间内。\n"
    )
