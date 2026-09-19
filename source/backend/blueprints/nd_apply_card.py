"""【节点设计 SAVE_PLOT 采纳·纯辅助函数】（自 blueprints/chat_collab_bp.py 拆出，架构门禁）。

把 apply_card 内嵌的 timeline（SAVE_PLOT）落地逻辑抽为模块级纯函数，供 apply_card 复用：
  - _extract_volume_index_safe     卷号安全提取（dict/str -> int）
  - _volume_field_nonempty         卷字段"有有效值"判定
  - _merge_volume                  同卷合并（NEW 非空覆盖，OLD 兜底保留卷级字段）
  - _repair_volume_nodes_safe      节点 A+C 门禁（单章单节点·无重叠无跳章·50章/卷）
  - _merge_volume_nodes_incremental 续会/单章修改节点增量合并

依赖方向（无循环）：仅懒依赖 node_design_bp / app（_extract_volume_index），
不反向 import chat_collab_bp，故可被 chat_collab_bp 顶层 import。
"""
from __future__ import annotations

def _extract_volume_index_safe(vol):
    """从卷字典或字符串中提取卷号。dict 时取 volume/volume_id 字段。"""
    if isinstance(vol, dict):
        for k in ('volume_index', 'volume_id'):
            v = vol.get(k)
            if v is not None:
                try:
                    return int(v)
                except (ValueError, TypeError):
                    pass
        # 从 volume 名提取
        try:
            from app import _extract_volume_index
            return _extract_volume_index(vol.get('volume', '') or vol.get('volume_title', '') or '')
        except Exception:
            return 0
    if isinstance(vol, str):
        try:
            from app import _extract_volume_index
            return _extract_volume_index(vol)
        except Exception:
            return 0
    return 0

def _volume_field_nonempty(v):
    """判断卷字段是否"有有效值"：空字符串/空列表/None/false/零 volume_index 不算。"""
    if v is None: return False
    if isinstance(v, str): return bool(v.strip())
    if isinstance(v, (list, tuple, set, dict)): return len(v) > 0
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)):
        # volume_index=0 视为空，其他数值有效
        return v != 0
    return True  # 未知类型按非空处理

def _merge_volume(old_v: dict, new_v: dict) -> dict:
    """同卷合并：仅用 NEW 中非空字段覆盖旧字段，其余卷级字段一律从 OLD 保留。
       节点设计卡片（NEW 只带 nodes）不应把卷的 main_plot/core_conflict/main_events 等抹空。
       同时保证向后兼容：summary → main_plot，end_hook → ending_hook。"""
    if not isinstance(new_v, dict):
        return new_v
    # 所有已知卷级字段：NEW 有非空就用 NEW，否则从 OLD 继承
    VOL_FIELDS = (
        'volume_id', 'volume', 'volume_title', 'volume_index',
        'summary', 'main_plot', 'core_conflict', 'plot_summary',
        'ending_hook', 'end_hook', 'ending',
        'main_events', 'nodes', 'chapter_beats',
        'characters', 'timeline_anchor', 'location', 'locations',
        'realm_change', 'age_change', 'target_audience',
        'bury', 'payoff', 'cool_type', 'cool_level',
        'state', 'status', 'progress', 'notes',
    )
    merged: dict = {}
    old_is_dict = isinstance(old_v, dict)
    for k in VOL_FIELDS:
        new_val = new_v.get(k)
        old_val = old_is_dict and old_v.get(k)
        # NEW 有非空有效值 → 优先 NEW；否则 OLD（若是dict）→ 否则跳过
        if _volume_field_nonempty(new_val):
            merged[k] = new_val
        elif _volume_field_nonempty(old_val):
            merged[k] = old_val
    # 保留 NEW 中额外未知自定义字段（但仅当 OLD 里没有，避免覆盖未知保留字段）
    for k, vv in new_v.items():
        if k in VOL_FIELDS:
            continue
        if k not in merged:
            merged[k] = vv
    # 保留 OLD 中额外未知保留字段（NEW 未声明）避免被擦除
    if old_is_dict:
        for k, vv in old_v.items():
            if k not in merged:
                merged[k] = vv
    # 向后兼容：summary → main_plot（旧代码/旧 UI 只认 main_plot）
    if (not merged.get('main_plot') or not str(merged['main_plot']).strip()) and merged.get('summary'):
        merged['main_plot'] = str(merged['summary'])
    # 核心冲突兜底：用 main_plot 的首 200 字再撑一下
    if not merged.get('core_conflict') or not str(merged['core_conflict']).strip():
        merged['core_conflict'] = str(merged.get('main_plot') or '')[:200]
    # 结尾钩子兜底：end_hook → ending → old.ending_hook
    if not merged.get('ending_hook'):
        merged['ending_hook'] = merged.get('end_hook') or merged.get('ending') or (old_is_dict and old_v.get('ending_hook')) or ''
    # main_events 兜底：保证是数组，元素至少含 index/title/summary
    me = merged.get('main_events')
    if isinstance(me, list):
        cleaned = []
        for idx, ev in enumerate(me):
            if not isinstance(ev, dict):
                continue
            ev.setdefault('index', idx + 1)
            ev.setdefault('title', f'事件{idx+1}')
            ev.setdefault('summary', ev.get('summary') or ev.get('event') or ev.get('events') or '')
            ev.setdefault('bury', '')
            ev.setdefault('payoff', '')
            cleaned.append(ev)
        merged['main_events'] = cleaned
    else:
        merged['main_events'] = []
    # nodes 兜底：保证数组
    if not isinstance(merged.get('nodes'), list):
        merged['nodes'] = []
    # volume_index 绝不能为空
    if merged.get('volume_index') in (None, ''):
        merged['volume_index'] = old_is_dict and old_v.get('volume_index') or _extract_volume_index_safe(merged) or 1
    try:
        merged['volume_index'] = int(float(merged['volume_index']))
    except (TypeError, ValueError):
        merged['volume_index'] = 1
    if not merged.get('volume'):
        merged['volume'] = f"第{merged['volume_index']}卷"
    return merged

# ===== 节点设计 A+C 门禁：无论 LLM 按什么粒度/格式输出 nodes，
# 统一过 _repair_nodes_to_one_ch_per_node 保证：
#   · 单章单节点 · 无重叠无跳章无越界 · 节点数=本章数
# 先根据 volume_index 反推"该卷所在章节区间"，再对每个卷的 nodes 做后置修复。
def _repair_volume_nodes_safe(nv: dict) -> dict:
    try:
        if not isinstance(nv, dict):
            return nv
        nodes = nv.get('nodes')
        if not isinstance(nodes, list) or not nodes:
            return nv
        # 优先从 cpv / chapter_count / volume_index + 全局50默认推导起止章
        cpv = None
        for k in ('chapter_count', 'cpv', 'chapters_per_volume', 'chapters_count'):
            v = nv.get(k)
            if isinstance(v, (int, float)) and v > 0:
                cpv = int(v); break
        if cpv is None:
            # 尝试从已有节点里取最大 chapters 作为上界（不小于）
            mx = 0
            from node_design_bp import _parse_chapters_field
            for nd in nodes:
                rng = _parse_chapters_field(nd.get('chapters') if isinstance(nd, dict) else None)
                if rng and rng[-1] > mx: mx = rng[-1]
            if mx > 0:
                # 只做"不小于当前最大chapters"的 cpv 估计：用全局50默认或用mx本身
                cpv = mx
        if cpv is None or cpv <= 0:
            cpv = 50  # 默认兜底：每卷50章（和 node_design_bp cpv 默认一致）
        # 推导 start_ch：若卷内已含 chapters 且连续最小=X且X>1，说明非首卷；否则用volume_index推
        vi = None
        for k in ('volume_index', 'volume_idx', 'vol_index'):
            v = nv.get(k)
            if isinstance(v, (int, float)):
                vi = int(v); break
        if vi is None:
            vi = _extract_volume_index_safe(nv) or 1
        # 估计 start_chapter：首章起点=1+(vi-1)*cpv
        start_ch = 1 + (vi - 1) * cpv
        # 若已有节点覆盖到>end_ch的章节或首章明显小了，用nodes实际区间收缩
        real_min, real_max = None, None
        try:
            from node_design_bp import _parse_chapters_field
            for nd in nodes:
                rng = _parse_chapters_field(nd.get('chapters') if isinstance(nd, dict) else None)
                if not rng:
                    continue
                if real_min is None or rng[0] < real_min:
                    real_min = rng[0]
                if real_max is None or rng[-1] > real_max:
                    real_max = rng[-1]
        except Exception:
            real_min = real_max = None
        if real_min and real_min > start_ch:
            start_ch = real_min
        end_ch = start_ch + cpv - 1
        if real_max and real_max > end_ch:
            end_ch = real_max
        # 修复：节点按单章粒度重排
        from node_design_bp import _repair_nodes_to_one_ch_per_node
        repaired, _ = _repair_nodes_to_one_ch_per_node(nodes, start_ch, end_ch, me_index=vi, index_offset_start=0)
        # 给节点重新编 index
        for idx, nd in enumerate(repaired):
            if isinstance(nd, dict) and not nd.get('index'):
                nd['index'] = idx + 1
        nv['nodes'] = repaired
        nv['chapter_count'] = cpv
        if not nv.get('start_chapter'):
            nv['start_chapter'] = start_ch
        if not nv.get('end_chapter'):
            nv['end_chapter'] = end_ch
        return nv
    except Exception:
        # 修复失败不影响落卡（原内容保留，避免因为修复器bug导致无法采纳）
        return nv

def _merge_volume_nodes_incremental(old_vol: dict, new_vol: dict) -> tuple[dict, bool]:
    """续会/单章修改时的节点增量合并：按 chapters 章号去重。
    策略：
      1) 若 OLD 无 nodes → 返回 NEW.nodes（什么都不做）；无增量行为。
      2) 把 OLD / NEW 节点都展开成 {ch: node} 映射（单章粒度）；
         NEW 命中的章覆盖 OLD；OLD 没被 NEW 命中的章一律保留。
      3) 按章节号排序，重建 nodes 列表 + index 连续。
    返回 (merged_vol, merged)，merged=True 说明发生了增量合并。
    """
    try:
        if not isinstance(old_vol, dict) or not isinstance(new_vol, dict):
            return new_vol, False
        old_nodes = old_vol.get('nodes')
        new_nodes = new_vol.get('nodes')
        if not isinstance(old_nodes, list) or not old_nodes:
            return new_vol, False
        if not isinstance(new_nodes, list) or not new_nodes:
            return new_vol, False
        from node_design_bp import _parse_chapters_field
        def _expand(nodes_list: list) -> dict[int, dict]:
            res: dict[int, dict] = {}
            for nd in nodes_list:
                if not isinstance(nd, dict):
                    continue
                chs = _parse_chapters_field(nd.get('chapters'))
                if not chs:
                    continue
                # 单章单节点门禁：chs 长度>1（LLM把多章合并写1节点）→ 展开给每个章都挂一个浅拷贝
                if len(chs) == 1:
                    res[int(chs[0])] = nd
                else:
                    for c in chs:
                        d = dict(nd)
                        d['chapters'] = int(c)
                        res[int(c)] = d
            return res
        old_map = _expand(old_nodes)
        new_map = _expand(new_nodes)
        if not new_map:
            return new_vol, False
        # NEW 命中章 → 覆盖 OLD；OLD 保留 NEW 没命中的
        merged_map: dict[int, dict] = {}
        merged_map.update(old_map)
        merged_map.update(new_map)
        # 按章号升序重建 nodes，重写 index
        rebuilt: list[dict] = []
        for i, ch in enumerate(sorted(merged_map.keys())):
            nd = dict(merged_map[ch])
            nd['chapters'] = int(ch)
            nd['index'] = i + 1
            rebuilt.append(nd)
        new_vol['nodes'] = rebuilt
        # 同步 chapter_count/start_chapter/end_chapter（避免增量后写回仍然 cpv=50 但 nodes 实际只有后半段）
        if rebuilt:
            chs_sorted = sorted(merged_map.keys())
            s, e = int(chs_sorted[0]), int(chs_sorted[-1])
            new_vol['start_chapter'] = s
            new_vol['end_chapter'] = e
            # chapter_count 只在 NEW 没明确指定时，保持 OLD 原值（不把 OLD.chapter_count=50 收缩）
            if not _volume_field_nonempty(new_vol.get('chapter_count')) and _volume_field_nonempty(old_vol.get('chapter_count')):
                new_vol['chapter_count'] = old_vol['chapter_count']
        return new_vol, True
    except Exception:
        # 合并失败不中断，按 NEW.nodes 直接覆盖（不吞掉用户的采纳）
        return new_vol, False
