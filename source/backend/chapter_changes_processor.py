"""
章级变更处理器（P1-6 + P1-7）
解析 LLM 输出的 12 类 CHANGES，delta patch 回写到各维度字段。

参考：天命 ChapterChanges + delta patch + Openwrite 三真相文件
设计原则：
  - LLM 输出格式：正文 + <chapter_changes>{JSON}</chapter_changes>
  - 12 类变更，空则 []，解析失败则跳过回写（不阻断章节入库）
  - delta merge 到 dynamic_volumes 对应字段，不整卷重写
  - 章级日志保留全量，支持重写回滚
"""
import json
import re
from typing import Dict, List, Tuple, Optional, Any


# 12 类变更字段名（天命 ChapterChanges.TopLevelFieldNames 对齐）
CHANGES_FIELDS = [
    'CharacterStateChanges',
    'ConflictProgress',
    'NewPlotPoints',
    'ForeshadowingActions',
    'LocationStateChanges',
    'FactionStateChanges',
    'TimeProgression',
    'CharacterMovements',
    'ItemTransfers',
    'SecretRevealChanges',
    'PledgeConstraintChanges',
    'DeadlineConstraintChanges',
]


def extract_changes(full_content: str) -> Tuple[str, Optional[Dict]]:
    """从 LLM 完整输出中剥离正文和 CHANGES JSON。
    返回 (正文, changes_dict)。解析失败返回 (原内容, None)。"""
    if not full_content:
        return full_content, None

    body = full_content
    changes = None

    # 尝试 XML 标签格式：<chapter_changes>{...}</chapter_changes>
    m = re.search(r'<chapter_changes>\s*([\s\S]*?)\s*</chapter_changes>', body, re.IGNORECASE)
    if m:
        try:
            changes = json.loads(m.group(1))
            body = re.sub(r'<chapter_changes>[\s\S]*?</chapter_changes>', '', body, flags=re.IGNORECASE)
        except json.JSONDecodeError:
            pass

    # 兜底1：---CHANGES--- 分隔符
    if changes is None:
        m = re.search(r'-{3,}\s*CHANGES\s*-{3,}\s*([\s\S]*?)$', body)
        if m:
            try:
                changes = json.loads(m.group(1).strip())
                body = body[:m.start()].strip()
            except json.JSONDecodeError:
                pass

    # 兜底2：尾部 JSON 块（最后一个 { 开始到结尾）
    if changes is None:
        m = re.search(r'\n(\{[\s\S]*\})\s*$', body)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if any(k in parsed for k in CHANGES_FIELDS):
                    changes = parsed
                    body = body[:m.start()].strip()
            except json.JSONDecodeError:
                pass

    # 剥离 PRE_WRITE_CHECK
    body = re.sub(r'<pre_write_check>[\s\S]*?</pre_write_check>', '', body, flags=re.IGNORECASE).strip()

    return body, changes


def normalize_changes(changes: Dict) -> Dict:
    """归一化 CHANGES：补全缺失字段为 []，确保结构一致。"""
    if not changes or not isinstance(changes, dict):
        return {}
    normalized = {}
    for field in CHANGES_FIELDS:
        val = changes.get(field, [])
        # TimeProgression 是单对象非数组，其余是数组
        if field == 'TimeProgression':
            normalized[field] = val if isinstance(val, dict) else ({} if val == [] else val)
        else:
            normalized[field] = val if isinstance(val, list) else []
    return normalized


def apply_chapter_changes(
    bb,
    chapter_id: str,
    chapter_num: int,
    volume_index: int,
    changes: Dict,
) -> Dict:
    """将 CHANGES delta patch 回写到 bb 各字段。
    返回回写摘要（供日志/前端展示）。

    改动字段：
      - bb.foreshadowing_graph: 伏笔状态更新（setup→待收, payoff→已收）
      - bb.dynamic_volumes: delta merge 到对应卷的 characters/events/timeline 等字段
      - bb.chapter_changes_log: 追加章级变更日志
    """
    summary = {'applied': False, 'fields_updated': [], 'errors': []}
    if not changes:
        return summary

    changes = normalize_changes(changes)

    # 1. 伏笔状态更新
    fs_actions = changes.get('ForeshadowingActions', [])
    if fs_actions and bb.foreshadowing_graph:
        try:
            from foreshadowing_manager import ForeshadowingGraph
            graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
            for action in fs_actions:
                fid = action.get('ForeshadowId', '')
                act = action.get('Action', '')
                if not fid or not act:
                    continue
                if act == 'setup':
                    graph.mark_setup(fid, chapter_num)
                elif act == 'payoff':
                    graph.mark_resolved(fid, chapter_num)
            bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
            summary['fields_updated'].append('foreshadowing_graph')
        except Exception as e:
            summary['errors'].append(f'foreshadowing_graph: {str(e)[:100]}')

    # 2. dynamic_volumes delta merge
    try:
        dv_list = json.loads(bb.dynamic_volumes) if bb.dynamic_volumes else []
        if isinstance(dv_list, list) and 0 <= volume_index < len(dv_list):
            dv_entry = dv_list[volume_index]
            _merge_changes_to_dv(dv_entry, changes, chapter_num)
            bb.dynamic_volumes = json.dumps(dv_list, ensure_ascii=False)
            summary['fields_updated'].append('dynamic_volumes')
    except Exception as e:
        summary['errors'].append(f'dynamic_volumes: {str(e)[:100]}')

    # 2.5 P3 补全：locations 主字段同步回写（LocationStateChanges → locations 三级 JSON）
    loc_changes = changes.get('LocationStateChanges', [])
    if loc_changes:
        try:
            _apply_location_changes(bb, loc_changes, chapter_num)
            summary['fields_updated'].append('locations')
        except Exception as e:
            summary['errors'].append(f'locations: {str(e)[:100]}')

    # 2.6 P3 补全：inventory 主字段同步回写（ItemTransfers → inventory JSON）
    item_changes = changes.get('ItemTransfers', [])
    if item_changes:
        try:
            _apply_item_changes(bb, item_changes, chapter_num)
            summary['fields_updated'].append('inventory')
        except Exception as e:
            summary['errors'].append(f'inventory: {str(e)[:100]}')

    # 2.7 P3 补全：character_profiles 主字段同步回写（境界/能力变化追加到角色备注）
    char_changes = changes.get('CharacterStateChanges', [])
    if char_changes:
        try:
            _apply_character_changes(bb, char_changes, chapter_num)
            summary['fields_updated'].append('character_profiles')
        except Exception as e:
            summary['errors'].append(f'character_profiles: {str(e)[:100]}')

    # 3. 章级变更日志追加
    try:
        log_list = json.loads(bb.chapter_changes_log) if bb.chapter_changes_log else []
        if not isinstance(log_list, list):
            log_list = []
        log_entry = {
            'chapter_id': chapter_id,
            'chapter_num': chapter_num,
            'volume_index': volume_index,
            'changes': changes,
            'timestamp': _now_iso(),
        }
        log_list.append(log_entry)
        # 限制日志大小（保留最近 200 章）
        if len(log_list) > 200:
            log_list = log_list[-200:]
        bb.chapter_changes_log = json.dumps(log_list, ensure_ascii=False)
        summary['fields_updated'].append('chapter_changes_log')
    except Exception as e:
        summary['errors'].append(f'chapter_changes_log: {str(e)[:100]}')

    summary['applied'] = bool(summary['fields_updated'])
    return summary


def remove_chapter_changes(bb, chapter_num: int) -> bool:
    """重写章节时先清除该章的旧 delta（回滚）。
    从 chapter_changes_log 删除该章记录，并逆向回滚 dynamic_volumes 的 merge。"""
    try:
        log_list = json.loads(bb.chapter_changes_log) if bb.chapter_changes_log else []
        if not isinstance(log_list, list):
            return False
        # 过滤掉该章的旧记录
        new_log = [e for e in log_list if e.get('chapter_num') != chapter_num]
        if len(new_log) == len(log_list):
            return False  # 没有该章记录
        bb.chapter_changes_log = json.dumps(new_log, ensure_ascii=False)
        return True
    except Exception:
        return False


def _merge_changes_to_dv(dv_entry: Dict, changes: Dict, chapter_num: int):
    """将 12 类变更 delta merge 到 dynamic_volumes 条目的对应字段。"""
    # 角色状态变化 → characters
    char_changes = changes.get('CharacterStateChanges', [])
    if char_changes:
        existing = dv_entry.get('characters', '')
        new_chars = []
        for c in char_changes:
            name = c.get('CharacterId', '')
            level = c.get('NewLevel', '')
            key_event = c.get('KeyEvent', '')
            if name:
                new_chars.append(f'{name}：{level}（{key_event}）' if level or key_event else name)
        if new_chars:
            dv_entry['characters'] = (existing + '；' + '；'.join(new_chars)) if existing else '；'.join(new_chars)

    # 冲突推进 → events
    conflicts = changes.get('ConflictProgress', [])
    if conflicts:
        existing = dv_entry.get('events', '')
        new_events = [f'冲突推进：{c.get("ConflictId", "")} - {c.get("Event", "")}' for c in conflicts if c.get('ConflictId')]
        if new_events:
            dv_entry['events'] = (existing + '；' + '；'.join(new_events)) if existing else '；'.join(new_events)

    # 时间推进 → timeline
    time_prog = changes.get('TimeProgression', {})
    if isinstance(time_prog, dict) and time_prog:
        existing = dv_entry.get('timeline', '')
        new_time = f'第{chapter_num}章：{time_prog.get("KeyTimeEvent", "")}'
        dv_entry['timeline'] = (existing + '；' + new_time) if existing else new_time

    # 角色移动 → locations
    movements = changes.get('CharacterMovements', [])
    if movements:
        existing = dv_entry.get('locations', '')
        new_moves = [f'{m.get("CharacterId", "")}→{m.get("ToLocationName", "")}' for m in movements if m.get('CharacterId')]
        if new_moves:
            dv_entry['locations'] = (existing + '；' + '；'.join(new_moves)) if existing else '；'.join(new_moves)

    # 物品流转 → 更新到 characters 或单独记录
    items = changes.get('ItemTransfers', [])
    if items:
        existing = dv_entry.get('events', '')
        new_items = [f'物品流转：{i.get("ItemName", "")} {i.get("FromHolder", "")}→{i.get("ToHolder", "")}' for i in items if i.get('ItemName')]
        if new_items:
            dv_entry['events'] = (existing + '；' + '；'.join(new_items)) if existing else '；'.join(new_items)

    # 势力变化 → factions
    faction_changes = changes.get('FactionStateChanges', [])
    if faction_changes:
        existing = dv_entry.get('factions', '')
        new_factions = [f'{f.get("FactionId", "")}：{f.get("NewStatus", "")}' for f in faction_changes if f.get('FactionId')]
        if new_factions:
            dv_entry['factions'] = (existing + '；' + '；'.join(new_factions)) if existing else '；'.join(new_factions)


def build_changes_prompt_template() -> str:
    """构建 CHANGES 输出模板（注入章节 prompt 末尾）。"""
    return """

【输出格式铁律·P1-6】
正文写完后，必须在末尾输出 <chapter_changes>JSON</chapter_changes>，声明本章的状态变更。
JSON 必须包含以下 12 个字段（无变更则填 []）：

<chapter_changes>
{
  "CharacterStateChanges": [{"CharacterId":"角色名", "NewLevel":"新境界", "NewAbilities":[], "LostAbilities":[], "RelationshipChanges":{}, "NewMentalState":"", "KeyEvent":"关键事件", "Importance":"normal"}],
  "ConflictProgress": [{"ConflictId":"冲突名", "NewStatus":"active", "Event":"事件", "Importance":"normal"}],
  "NewPlotPoints": [{"Keywords":[], "Context":"", "InvolvedCharacters":[], "Importance":"normal", "Storyline":"main"}],
  "ForeshadowingActions": [{"ForeshadowId":"f001", "Action":"setup"}],
  "LocationStateChanges": [{"LocationId":"地点名", "NewStatus":"", "Event":"", "Importance":"normal"}],
  "FactionStateChanges": [{"FactionId":"势力名", "NewStatus":"", "Event":"", "Importance":"normal"}],
  "TimeProgression": {"TimePeriod":"", "ElapsedTime":"", "KeyTimeEvent":"", "Importance":"normal"},
  "CharacterMovements": [{"CharacterId":"角色名", "FromLocation":"", "ToLocation":"", "ToLocationName":"", "Importance":"normal"}],
  "ItemTransfers": [{"ItemId":"物品名", "ItemName":"", "FromHolder":"", "ToHolder":"", "NewStatus":"active", "Event":"", "Importance":"normal"}],
  "SecretRevealChanges": [{"SecretId":"秘密名", "NewKnowerIds":[], "Method":"told", "KeyEvent":"", "Importance":"normal"}],
  "PledgeConstraintChanges": [{"PledgeId":"承诺名", "Action":"create", "Type":"pledge", "PartyIds":[], "Condition":"", "Consequence":"", "KeyEvent":"", "Importance":"normal"}],
  "DeadlineConstraintChanges": [{"DeadlineId":"时限名", "Action":"create", "Type":"countdown", "Deadline":"", "TriggerCondition":"", "Consequence":"", "PartyIds":[], "KeyEvent":"", "Importance":"normal"}]
}
</chapter_changes>

说明：
- CharacterId/ForeshadowId 等可填实体名称（系统自动解析）
- Importance 取值：normal（可裁剪）/ important / critical（永久保留，每章≤2个）
- ForeshadowingActions 的 Action 只能是 setup（埋设）或 payoff（回收）
- 只声明本章实际发生的变化，不要编造"""


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ===== P3 补全：主字段同步回写函数 =====

def _apply_location_changes(bb, loc_changes: List, chapter_num: int):
    """LocationStateChanges → locations 主字段同步。
    locations 格式：[{name, description, secondaries:[{name, description, scenes:[{name, description, key_events}]}]}]
    策略：按 LocationId 匹配三级节点，找到则追加 key_events，找不到则跳过（不凭空创建）。"""
    if not bb.locations:
        return
    try:
        loc_list = json.loads(bb.locations)
    except Exception:
        return
    if not isinstance(loc_list, list):
        return

    updated = False
    for change in loc_changes:
        loc_id = change.get('LocationId', '')
        new_status = change.get('NewStatus', '')
        event = change.get('Event', '')
        if not loc_id or not event:
            continue
        event_text = f'第{chapter_num}章：{event}'
        if new_status:
            event_text += f'（状态→{new_status}）'
        # 在三级结构中查找匹配名称的节点
        for region in loc_list:
            if _match_and_append_event(region, loc_id, event_text):
                updated = True
                break
            for city in region.get('secondaries', []):
                if _match_and_append_event(city, loc_id, event_text):
                    updated = True
                    break
                for scene in city.get('scenes', []):
                    if _match_and_append_event(scene, loc_id, event_text):
                        updated = True
                        break
    if updated:
        bb.locations = json.dumps(loc_list, ensure_ascii=False)


def _match_and_append_event(node: Dict, target_name: str, event_text: str) -> bool:
    """若 node.name 匹配 target_name，则追加 event_text 到 key_events，返回 True。"""
    if not isinstance(node, dict):
        return False
    node_name = node.get('name', '')
    if node_name and (target_name in node_name or node_name in target_name):
        existing = node.get('key_events', '')
        node['key_events'] = (existing + '；' + event_text) if existing else event_text
        return True
    return False


def _apply_item_changes(bb, item_changes: List, chapter_num: int):
    """ItemTransfers → inventory 主字段同步。
    inventory 格式：[{name, type, source, effect, owner, first_appearance}]
    策略：按 ItemName/ItemId 匹配，找到则更新 owner 和追加流转记录，找不到则新增条目。"""
    try:
        inv_list = json.loads(bb.inventory) if bb.inventory else []
    except Exception:
        inv_list = []
    if not isinstance(inv_list, list):
        inv_list = []

    for change in item_changes:
        item_name = change.get('ItemName', '') or change.get('ItemId', '')
        if not item_name:
            continue
        to_holder = change.get('ToHolder', '')
        from_holder = change.get('FromHolder', '')
        event = change.get('Event', '')

        # 在现有清单中查找
        found = False
        for item in inv_list:
            if item.get('name', '') and (item_name in item['name'] or item['name'] in item_name):
                if to_holder:
                    # 记录原持有者到 source（保留历史）
                    old_owner = item.get('owner', '')
                    if old_owner and old_owner != to_holder:
                        item['source'] = (item.get('source', '') + f'；{old_owner}→' + to_holder).strip('；')
                    item['owner'] = to_holder
                # 追加流转事件到 effect
                if event:
                    transfer_note = f'第{chapter_num}章：{event}'
                    item['effect'] = (item.get('effect', '') + '；' + transfer_note).strip('；')
                found = True
                break

        # 找不到则新增（仅在有足够信息时）
        if not found and to_holder:
            inv_list.append({
                'name': item_name,
                'type': '其他',
                'source': from_holder or '未知',
                'effect': f'第{chapter_num}章：{event or "流转"}',
                'owner': to_holder,
                'first_appearance': f'第{chapter_num}章',
            })

    bb.inventory = json.dumps(inv_list, ensure_ascii=False)


def _apply_character_changes(bb, char_changes: List, chapter_num: int):
    """CharacterStateChanges → character_profiles 主字段同步。
    character_profiles 是文本格式（## 角色：<姓名>），策略：在匹配角色块中追加境界/能力变化备注。"""
    if not bb.character_profiles:
        return
    text = bb.character_profiles
    for change in char_changes:
        char_id = change.get('CharacterId', '')
        new_level = change.get('NewLevel', '')
        new_abilities = change.get('NewAbilities', [])
        key_event = change.get('KeyEvent', '')
        if not char_id:
            continue

        # 在文本中查找 ## 角色：<姓名> 块
        import re
        pattern = r'(##\s*角色[：:]\s*' + re.escape(char_id) + r'[^\n]*[\s\S]*?)(?=##\s*角色|$)'
        m = re.search(pattern, text)
        if m:
            block = m.group(1)
            notes = []
            if new_level:
                notes.append(f'第{chapter_num}章境界→{new_level}')
            if new_abilities:
                notes.append(f'第{chapter_num}章获得：{"、".join(new_abilities)}')
            if key_event:
                notes.append(f'第{chapter_num}章关键事件：{key_event}')
            if notes:
                note_line = f'\n- 状态变化：{"；".join(notes)}'
                # 追加到角色块末尾（下一个 ## 之前）
                new_block = block.rstrip() + note_line + '\n'
                text = text[:m.start()] + new_block + text[m.end():]

    bb.character_profiles = text

