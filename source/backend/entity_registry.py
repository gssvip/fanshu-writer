"""
实体注册表（P2-Entity Registry）
从 BookBible 全部十个维度抽取角色/势力/地点/物品/技能，提供跨维度重命名与合并。

抽取来源（覆盖平台十个维度）：
  - characters: character_profiles（## 角色：<姓名>）+ Character 表 + inventory.owner + dynamic_volumes 文本
  - factions: dynamic_volumes.factions + 各维度文本中"势力/门派/阵营"关键词
  - locations: locations 三级 JSON.name + dynamic_volumes.locations
  - items: inventory JSON.name（type≠功法/技能）
  - skills: inventory JSON.name（type=功法/技能）+ key_rules/worldbuilding 中"## 功法/## 技能"标题
            + character_profiles 中"功法/技能"行

重命名策略：事务性扫描 bible 全部文本字段 + chapters.content，整词替换并返回影响行数。
合并策略：将多个实体名归并到一个主名，其余名作为别名。
"""
import json
import re
from typing import Dict, List, Tuple


# 需要扫描的 bible 文本字段（不含 JSON 结构字段，那些走结构化替换）
BIBLE_TEXT_FIELDS = [
    'concept', 'key_rules', 'worldbuilding', 'character_profiles',
    'timeline', 'foreshadowing', 'style_guide', 'plot_design',
    'generated_summary', 'relation_graph',
]

# JSON 结构字段及其抽取键
BIBLE_JSON_FIELDS = {
    'locations': ['name'],  # 三级嵌套，递归抽 name
    'inventory': ['name'],
    'dynamic_volumes': ['characters', 'events', 'timeline', 'locations', 'factions'],
    'foreshadowing_graph': [],  # 结构复杂，仅做文本扫描
}


def _safe_json_loads(s: str, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _extract_names_from_locations(loc_json) -> List[str]:
    """递归抽取 locations 三级结构的所有 name。"""
    names = []
    if not isinstance(loc_json, list):
        return names
    for region in loc_json:
        if isinstance(region, dict):
            if region.get('name'):
                names.append(region['name'])
            for city in region.get('secondaries', []) or []:
                if isinstance(city, dict):
                    if city.get('name'):
                        names.append(city['name'])
                    for scene in city.get('scenes', []) or []:
                        if isinstance(scene, dict) and scene.get('name'):
                            names.append(scene['name'])
    return names


def extract_entities(bb) -> Dict:
    """从 BookBible 全部十个维度抽取实体，返回 {characters, factions, locations, items, skills}。
    每个实体：{name, aliases:[], dim_refs:[字段名...]}"""
    characters: Dict[str, set] = {}  # name -> dim_refs
    factions: Dict[str, set] = {}
    locations: Dict[str, set] = {}
    items: Dict[str, set] = {}
    skills: Dict[str, set] = {}

    def _add(bucket: Dict, name: str, ref: str):
        name = (name or '').strip()
        if not name or len(name) > 50:
            return
        if name not in bucket:
            bucket[name] = set()
        bucket[name].add(ref)

    # 1. character_profiles 文本：## 角色：<姓名>
    cp_text = bb.character_profiles or ''
    for m in re.finditer(r'##\s*角色[：:]\s*(.+?)(?:\n|$)', cp_text):
        _add(characters, m.group(1).strip(), 'character_profiles')

    # 2. Character 表（通过 relationship 访问）
    try:
        for ch in bb.book.characters:
            _add(characters, ch.name, 'characters_table')
    except Exception:
        pass

    # 3. inventory JSON: name + owner；type=功法/技能 归入 skills，其余归入 items
    inv = _safe_json_loads(bb.inventory, [])
    if isinstance(inv, list):
        for it in inv:
            if isinstance(it, dict):
                itype = (it.get('type') or '').strip()
                if it.get('name'):
                    if itype in ('功法', '技能'):
                        _add(skills, it['name'], 'inventory')
                    else:
                        _add(items, it['name'], 'inventory')
                if it.get('owner'):
                    _add(characters, it['owner'], 'inventory.owner')

    # 4. locations JSON: 三级 name
    loc = _safe_json_loads(bb.locations, [])
    for n in _extract_names_from_locations(loc):
        _add(locations, n, 'locations')

    # 5. dynamic_volumes JSON: characters/events/timeline/locations/factions 文本字段
    dv = _safe_json_loads(bb.dynamic_volumes, [])
    if isinstance(dv, list):
        for vol in dv:
            if not isinstance(vol, dict):
                continue
            for field in ['characters', 'events', 'timeline', 'locations', 'factions']:
                txt = vol.get(field, '')
                if not txt:
                    continue
                # 按常见分隔符切分后取每段首词作为候选实体名
                for seg in re.split(r'[；;\n]', txt):
                    seg = seg.strip()
                    if not seg:
                        continue
                    # 取冒号前的部分作为名（"林墨：筑基" → "林墨"）
                    if '：' in seg or ':' in seg:
                        name = re.split(r'[：:]', seg, 1)[0].strip()
                        if name and 1 < len(name) <= 20:
                            if field == 'factions':
                                _add(factions, name, f'dynamic_volumes.{field}')
                            elif field == 'locations':
                                _add(locations, name, f'dynamic_volumes.{field}')
                            else:
                                _add(characters, name, f'dynamic_volumes.{field}')

    # 6. 从全部文本维度扫描：势力/门派/阵营 关键词 → factions；
    #    "## 功法/## 技能/<功法名>" 标题 → skills
    SKILL_TITLE_RE = re.compile(r'##\s*(?:功法|技能)[：:]\s*(.+?)(?:\n|$)')
    for field in BIBLE_TEXT_FIELDS:
        txt = getattr(bb, field, '') or ''
        if not txt:
            continue
        # 势力关键词上下文（行内出现"势力/门派/宗派/阵营：XXX"）
        for m in re.finditer(r'(?:势力|门派|宗派|阵营)[：:]\s*([^\n，,；;]{1,20})', txt):
            _add(factions, m.group(1).strip(), field)
        # 功法/技能标题
        for m in SKILL_TITLE_RE.finditer(txt):
            _add(skills, m.group(1).strip(), field)

    # 7. foreshadowing 文本：## 伏笔N：<标题> 不算实体，但内容里出现的人名靠章节正文交叉验证（略）

    def _serialize(bucket: Dict) -> List[Dict]:
        return [{'name': k, 'aliases': [], 'dim_refs': sorted(v)} for k, v in sorted(bucket.items())]

    return {
        'characters': _serialize(characters),
        'factions': _serialize(factions),
        'locations': _serialize(locations),
        'items': _serialize(items),
        'skills': _serialize(skills),
    }


def _word_boundary_replace(text: str, old: str, new: str) -> Tuple[str, int]:
    """整词替换（避免"林墨"匹配到"林墨儿"），返回 (新文本, 替换次数)。
    中文用零宽断言近似整词边界：前后非字母数字汉字。"""
    if not text or not old or old == new:
        return text, 0
    # 转义正则元字符
    pattern = re.escape(old)
    # 前边界：开头或非汉字字母数字
    # 后边界：结尾或非汉字字母数字
    full = r'(?<![A-Za-z0-9\u4e00-\u9fa5])' + pattern + r'(?![A-Za-z0-9\u4e00-\u9fa5])'
    new_text, count = re.subn(full, new, text)
    return new_text, count


def rename_entity(bb, chapters_query, old_name: str, new_name: str, entity_type: str = 'character') -> Dict:
    """跨维度重命名实体。
    chapters_query: SQLAlchemy query对象（Chapter 列表），由调用方传入避免循环依赖。
    返回 {success, fields_updated:[...], chapters_affected:N, total_replacements:N}"""
    summary = {'success': False, 'fields_updated': [], 'chapters_affected': 0, 'total_replacements': 0}

    if not old_name or not new_name or old_name == new_name:
        summary['error'] = '无效的旧名/新名'
        return summary

    # 1. 文本字段整词替换
    for field in BIBLE_TEXT_FIELDS:
        old_val = getattr(bb, field, '') or ''
        if not old_val:
            continue
        new_val, count = _word_boundary_replace(old_val, old_name, new_name)
        if count > 0:
            setattr(bb, field, new_val)
            summary['fields_updated'].append(field)
            summary['total_replacements'] += count

    # 2. JSON 结构字段：递归替换 name/owner 等
    def _replace_in_obj(obj):
        """递归替换 dict/list 中的字符串值（仅整词）。"""
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if isinstance(v, str):
                    new_v, c = _word_boundary_replace(v, old_name, new_name)
                    if c > 0:
                        obj[k] = new_v
                        summary['total_replacements'] += c
                elif isinstance(v, (dict, list)):
                    _replace_in_obj(v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str):
                    new_v, c = _word_boundary_replace(v, old_name, new_name)
                    if c > 0:
                        obj[i] = new_v
                        summary['total_replacements'] += c
                elif isinstance(v, (dict, list)):
                    _replace_in_obj(v)

    for json_field in ['locations', 'inventory', 'dynamic_volumes', 'foreshadowing_graph', 'outline_hierarchy', 'chapter_changes_log']:
        old_val = getattr(bb, json_field, '') or ''
        if not old_val:
            continue
        parsed = _safe_json_loads(old_val, None)
        if parsed is None:
            continue
        before_count = summary['total_replacements']
        _replace_in_obj(parsed)
        if summary['total_replacements'] > before_count:
            setattr(bb, json_field, json.dumps(parsed, ensure_ascii=False))
            summary['fields_updated'].append(json_field)

    # 3. 章节正文替换
    chap_count = 0
    for ch in chapters_query:
        if not ch.content:
            continue
        new_content, count = _word_boundary_replace(ch.content, old_name, new_name)
        if count > 0:
            ch.content = new_content
            chap_count += 1
            summary['total_replacements'] += count

    summary['chapters_affected'] = chap_count
    summary['fields_updated'] = list(dict.fromkeys(summary['fields_updated']))  # 去重保序
    summary['success'] = summary['total_replacements'] > 0
    return summary


def merge_entities(bb, chapters_query, main_name: str, alias_names: List[str], entity_type: str = 'character') -> Dict:
    """合并实体：把 alias_names 全部替换为 main_name，并记录到别名列表。
    实质是循环调用 rename_entity。"""
    summary = {'success': False, 'merged': [], 'total_replacements': 0, 'chapters_affected': 0}
    for alias in alias_names:
        if alias == main_name:
            continue
        r = rename_entity(bb, chapters_query, alias, main_name, entity_type)
        if r.get('success'):
            summary['merged'].append({'from': alias, 'to': main_name, 'replacements': r['total_replacements']})
            summary['total_replacements'] += r['total_replacements']
            summary['chapters_affected'] += r['chapters_affected']
    summary['success'] = len(summary['merged']) > 0
    return summary
