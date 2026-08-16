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


# 需要扫描的 bible 文本字段（JSON 结构字段也加进来：当 JSON 解析失败时，它们可能仍是带实体的纯文本，
# 智驾采纳落地卡片常写成"【标题】\n分点冒号实体名：内容"这种，即使是 JSON 字符串里也会包含实体线索）。
BIBLE_TEXT_FIELDS = [
    'concept', 'key_rules', 'worldbuilding', 'character_profiles',
    'timeline', 'foreshadowing', 'style_guide', 'plot_design',
    'generated_summary', 'relation_graph',
    # 以下本来是 JSON 结构字段，但智驾落地/用户手动写的内容可能不是严格 JSON，而是自然语言块
    # 文本扫描会把字符串内容里的实体也抽出来，不影响结构化抽取那一条分支
    'locations', 'inventory', 'dynamic_volumes', 'foreshadowing_graph',
    'outline_hierarchy', 'chapter_changes_log',
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


def extract_entities(bb, chapters_query=None) -> Dict:
    """从 BookBible 全部十个维度 + 最近章节抽取实体。
    chapters_query: 可选 SQLAlchemy query / Chapter 对象 list（或 None），
                   传入后会额外从 chapters 标题/卷标题/正文中抽取出现的实体。
    返回：{characters:[{name,aliases,dim_refs}], factions, locations, items, skills}"""
    characters: Dict[str, set] = {}  # name -> dim_refs
    factions: Dict[str, set] = {}
    locations: Dict[str, set] = {}
    items: Dict[str, set] = {}
    skills: Dict[str, set] = {}

    def _add(bucket: Dict, name: str, ref: str):
        name = (name or '').strip()
        # 常见标点清理（中文人名前后带引号/顿号/书名号）
        if name:
            name = re.sub(r'^[\s“"《〈【\(（,，、；:：]+|[\s”"》〉】\)）,，、；:：]+$', '', name)
        if not name or len(name) > 50 or len(name) < 2:
            return
        # 纯数字/英文短词等直接丢（与 registry._is_valid_name 一致）
        if re.match(r'^\d+$', name):
            return
        if re.match(r'^[a-zA-Z]{1,3}$', name):
            return
        if name not in bucket:
            bucket[name] = set()
        bucket[name].add(ref)

    # ====== 1. character_profiles 分两种格式：智驾人物卡落地为 JSON 数组 / 旧版 Markdown ## 角色： ======
    cp_text = bb.character_profiles or ''
    # 1a) 优先 JSON 数组解析（智驾人物卡落地默认写成 JSON，这是用户截图里"识别不到"的根因）
    try:
        cp_json = json.loads(cp_text) if cp_text else None
    except Exception:
        cp_json = None
    if isinstance(cp_json, list):
        for p in cp_json:
            if not isinstance(p, dict):
                continue
            for key in ('name', '姓名', 'character', '角色名'):
                v = p.get(key)
                if v:
                    _add(characters, str(v), 'character_profiles.json')
                    break
            # 人物卡关系/能力/物品里也会带相关实体（顺路抽取）
            # 【关键】统一构造多桶字典，避免 _extract_generic_text_entities 的 is_multi 误判成多桶后 KeyError
            for key, cat in (('关系', 'characters'), ('relationships', 'characters'),
                            ('能力', 'skills'), ('abilities', 'skills'),
                            ('物品', 'items'), ('items', 'items'),
                            ('身份', 'characters'), ('identity', 'characters')):
                v = p.get(key)
                if isinstance(v, str) and v:
                    mini = {k: (bucket if cat == k else characters if k == 'characters' else factions if k == 'factions' else locations if k == 'locations' else items if k == 'items' else skills)
                            for k, bucket in
                            [('characters', characters), ('factions', factions), ('locations', locations), ('items', items), ('skills', skills)]}
                    _extract_generic_text_entities(v, mini, f'character_profiles.json.{key}', _add)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, str) and x:
                            mini = {k: (bucket if cat == k else characters if k == 'characters' else factions if k == 'factions' else locations if k == 'locations' else items if k == 'items' else skills)
                                    for k, bucket in
                                    [('characters', characters), ('factions', factions), ('locations', locations), ('items', items), ('skills', skills)]}
                            _extract_generic_text_entities(x, mini, f'character_profiles.json.{key}', _add)
    # 1b) Markdown 旧版兜底：## 角色：<姓名>
    for m in re.finditer(r'##\s*角色[：:]\s*(.+?)(?:\n|$)', cp_text):
        _add(characters, m.group(1).strip(), 'character_profiles')

    # ====== 2. Character 表（通过 relationship 访问） ======
    try:
        for ch in bb.book.characters:
            _add(characters, ch.name, 'characters_table')
    except Exception:
        pass

    # ====== 3. inventory JSON ======
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

    # ====== 4. locations JSON（三级嵌套） ======
    loc = _safe_json_loads(bb.locations, [])
    for n in _extract_names_from_locations(loc):
        _add(locations, n, 'locations')

    # ====== 5. dynamic_volumes JSON ======
    dv = _safe_json_loads(bb.dynamic_volumes, [])
    if isinstance(dv, list):
        for vol in dv:
            if not isinstance(vol, dict):
                continue
            for field in ['characters', 'events', 'timeline', 'locations', 'factions']:
                txt = vol.get(field, '')
                if not txt:
                    continue
                # 同时做结构化冒号抽取 + 通用文本抽取（双保险）
                for seg in re.split(r'[；;\n]', str(txt)):
                    seg = seg.strip()
                    if not seg:
                        continue
                    if '：' in seg or ':' in seg:
                        name = re.split(r'[：:]', seg, 1)[0].strip()
                        if name and 1 < len(name) <= 20:
                            if field == 'factions':
                                _add(factions, name, f'dynamic_volumes.{field}')
                            elif field == 'locations':
                                _add(locations, name, f'dynamic_volumes.{field}')
                            else:
                                _add(characters, name, f'dynamic_volumes.{field}')
                _extract_generic_text_entities(str(txt), {
                    'characters': characters, 'factions': factions,
                    'locations': locations, 'items': items, 'skills': skills,
                }, f'dynamic_volumes.{field}', _add)

    # ====== 6. 从全部文本维度做通用扫描（修复用户反馈的"各维度已有实体识别不到"根因） ======
    SKILL_TITLE_RE = re.compile(r'##\s*(?:功法|技能|术法|神通|秘法)[：:]\s*(.+?)(?:\n|$)')
    FACTION_TITLE_RE = re.compile(r'##\s*(?:势力|门派|宗门|宗派|阵营|家族|商会|军团)[：:]\s*(.+?)(?:\n|$)')
    LOC_TITLE_RE = re.compile(r'##\s*(?:地点|区域|地图|秘境|遗迹|城池|大陆|海域|山脉)[：:]\s*(.+?)(?:\n|$)')
    ITEM_TITLE_RE = re.compile(r'##\s*(?:物品|宝物|法器|法宝|丹药|灵药|材料|装备)[：:]\s*(.+?)(?:\n|$)')
    CHAR_TITLE_RE = re.compile(r'##\s*(?:人物|角色|配角|龙套|反派|主角)[：:]\s*(.+?)(?:\n|$)')

    for field in BIBLE_TEXT_FIELDS:
        txt = getattr(bb, field, '') or ''
        if not txt:
            continue
        # 势力关键词上下文（行内出现"势力/门派/宗派/阵营/宗门/家族/商会：XXX"）
        for m in re.finditer(r'(?:势力|门派|宗派|宗门|阵营|家族|商会|军团)[：:]\s*([^\n，,；;]{1,20})', txt):
            _add(factions, m.group(1).strip(), field)
        # 地点关键词
        for m in re.finditer(r'(?:地点|区域|城池|秘境|遗迹|山脉|海域|大陆|村庄|小镇|学院|宗门驻地)[：:]\s*([^\n，,；;]{1,25})', txt):
            _add(locations, m.group(1).strip(), field)
        # 物品关键词
        for m in re.finditer(r'(?:物品|宝物|法宝|法器|丹药|灵药|材料|装备|储物袋)[：:]\s*([^\n，,；;]{1,25})', txt):
            _add(items, m.group(1).strip(), field)
        # 功法/技能标题
        for m in SKILL_TITLE_RE.finditer(txt):
            _add(skills, m.group(1).strip(), field)
        # 人物/角色标题块（手动在设定 Tab 写的"## 主角：XXX"这种）
        for m in CHAR_TITLE_RE.finditer(txt):
            _add(characters, m.group(1).strip(), field)
        # 势力标题块
        for m in FACTION_TITLE_RE.finditer(txt):
            _add(factions, m.group(1).strip(), field)
        # 地点标题块
        for m in LOC_TITLE_RE.finditer(txt):
            _add(locations, m.group(1).strip(), field)
        # 物品标题块
        for m in ITEM_TITLE_RE.finditer(txt):
            _add(items, m.group(1).strip(), field)
        # 通用格式抽取：智驾采纳落地卡片经常写"【标题】\n分点：实体名 | 信息"这种
        _extract_generic_text_entities(txt, {
            'characters': characters, 'factions': factions,
            'locations': locations, 'items': items, 'skills': skills,
        }, field, _add)

    # ====== 7. 最近 N 章正文（智驾落地人物后如果只在正文出现也要能被抓到） ======
    if chapters_query is not None:
        try:
            ch_iter = iter(chapters_query)
        except Exception:
            ch_iter = []
        for ch in ch_iter:
            title = getattr(ch, 'title', '') or ''
            content = getattr(ch, 'content', '') or ''
            if title:
                _extract_generic_text_entities(title, {
                    'characters': characters, 'factions': factions,
                    'locations': locations, 'items': items, 'skills': skills,
                }, 'chapter.title', _add)
            if content:
                # 仅从章节正文抽取 人物/地点 出现的已知候选（不做大规模无差别识别以免噪声）
                _extract_generic_text_entities(content, {
                    'characters': characters, 'factions': factions,
                    'locations': locations, 'items': items, 'skills': skills,
                }, 'chapter.content', _add)

    def _serialize(bucket: Dict) -> List[Dict]:
        return [{'name': k, 'aliases': [], 'dim_refs': sorted(v)} for k, v in sorted(bucket.items())]

    return {
        'characters': _serialize(characters),
        'factions': _serialize(factions),
        'locations': _serialize(locations),
        'items': _serialize(items),
        'skills': _serialize(skills),
    }


# ==================== 辅助：从自由文本里无差别抽取实体（冒号/竖线/【】/引号/分点） ====================

# 常见"实体关键词前缀"（出现在冒号/竖线前，用来判定这行冒号前的东西是什么类型）
_CATEGORY_HINTS = {
    'characters': {'姓名', '名字', '人物', '角色', '主角', '反派', '配角', '龙套', '师父', '师傅',
                   '师兄', '师弟', '师姐', '师妹', '父亲', '母亲', '儿子', '女儿', '兄弟', '姐妹',
                   '族长', '长老', '掌门', '城主', '皇帝', '殿下', '公子', '小姐'},
    'factions': {'势力', '门派', '宗门', '宗派', '阵营', '家族', '商会', '军团', '联盟', '道统', '教', '寺', '楼'},
    'locations': {'地点', '区域', '地图', '城池', '秘境', '遗迹', '大陆', '山脉', '海域', '宗门',
                  '驻地', '学院', '村庄', '小镇', '城市', '府', '阁', '殿', '楼', '塔', '山', '谷'},
    'items': {'物品', '宝物', '法宝', '法器', '丹药', '灵药', '材料', '装备', '储物袋',
              '令牌', '剑', '刀', '枪', '弓', '甲', '船', '车', '印', '符', '阵', '丹'},
    'skills': {'功法', '技能', '术法', '神通', '秘法', '心法', '招式', '武学', '法术', '咒语'},
}


def _category_from_label(label: str):
    if not label:
        return None
    for cat, hints in _CATEGORY_HINTS.items():
        for h in hints:
            if h in label:
                return cat
    return None


def _extract_generic_text_entities(text: str, buckets, ref: str, add_fn):
    """智驾落地/用户手动写的实体，格式千差万别：
    - 「【标题】\n姓名：林墨\n身份：玄骨宗弟子\n宗门：玄骨宗\n师父：姜无涯」
    - 「林墨 | 男 | 19 岁 | 主角」
    - 「主要势力：玄骨宗、镇魔司、大胤皇室」
    - 「核心人物：林墨、苏清鸢、老骨」
    - 出现于引号、【】、《》里的 2-6 字中文实体
    这里统一抽取，用户反馈的"各维度已有实体识别不到"就由这里兜底。

    buckets 约定：**始终传多桶字典格式**：
        {'characters': {name: set()}, 'factions': {...}, 'locations': {...}, 'items': {...}, 'skills': {...}}
    add_fn(bucket, name, ref)

    为了绝对不 500，整个函数外层 try/except；任何异常都静默吞掉，最多漏掉几个实体，不影响整条 API。
    """
    try:
        if not text:
            return
        # 只接受"多桶字典格式"，只要有一个 key 是固定类别名，就按多桶处理；
        # 否则打印一条日志（这里 pass）然后直接返回，避免把单桶当多桶扫 → KeyError
        if not isinstance(buckets, dict):
            return
        expected_keys = {'characters', 'factions', 'locations', 'items', 'skills'}
        if not (set(buckets.keys()) & expected_keys):
            return  # 传进来的 dict 不是多桶格式，直接跳过，不抛异常

        def add(cat, name):
            if cat in buckets:
                add_fn(buckets[cat], name, ref)

        # A. 行级扫："XX 标签(类别提示)：实体名1、实体名2、实体名3"
        for line in re.split(r'[\r\n]+', text):
            try:
                if not line or len(line) > 400:
                    continue
                # 冒号：前半是标签，后半是"顿号/逗号/分号"枚举实体
                m = re.match(r'^\s*[-*•·]*\s*([^：:\n]{1,20})\s*[：:]\s*(.+?)\s*$', line)
                if m:
                    label = m.group(1).strip()
                    content = m.group(2).strip()
                    cat = _category_from_label(label)
                    # 后半按顿号/逗号/分号切
                    candidates = [x.strip(' 、') for x in re.split(r'[、,，;；]+', content) if x.strip()]
                    for cand in candidates:
                        # 有时会写成"林墨（男·19岁）" → 先保留括号前主名
                        main = re.split(r'[（(（【《]', cand, 1)[0].strip()
                        if not main:
                            continue
                        # 如果能判定类别就直接加；否则默认先当人物（中文冒号前出现的姓名最常见）
                        if cat:
                            add(cat, main)
                        else:
                            # 没有类别提示时，名字长度合理 → 当人物候选加入
                            if 2 <= len(main) <= 8 and re.search(r'[\u4e00-\u9fa5]', main):
                                add('characters', main)
                    continue
                # 竖线分隔："林墨 | 男 | 主角" → 第1段当人名
                if '|' in line:
                    parts = [p.strip() for p in line.split('|') if p.strip()]
                    if len(parts) >= 2 and 2 <= len(parts[0]) <= 8 and re.search(r'[\u4e00-\u9fa5]', parts[0]):
                        add('characters', parts[0])
            except Exception:
                continue

        # B. 段级扫：
        #    1) 「主要人物：林墨、苏清鸢」"人物/势力/地点/物品/功法" 前缀枚举实体
        try:
            m2 = re.findall(r'(?:主要|核心|关键|重要|其他|已登场)?\s*'
                            r'(人物|角色|势力|门派|宗门|阵营|地点|区域|物品|功法|技能|法宝|丹药)'
                            r'[：:]\s*([^\n]{2,200})', text)
            for label, body in m2:
                cat = _category_from_label(label) or 'characters'
                for cand in re.split(r'[、,，;；\s]+', body):
                    cand = cand.strip()
                    if not cand:
                        continue
                    main = re.split(r'[（(【《]', cand, 1)[0].strip()
                    if 2 <= len(main) <= 12 and re.search(r'[\u4e00-\u9fa5]', main):
                        add(cat, main)
        except Exception:
            pass

        # C. 【姓名】/"姓名"/《功法名》：2-8 字中文实体
        #    仅在能抓到线索时使用，避免整段正文无差别扫入噪声
        try:
            brackets = re.findall(r'[【“\"《〈]([^\]】”\"》〉]{1,12})[】”\"》〉]', text)
            for cand in brackets:
                cand = cand.strip()
                if 2 <= len(cand) <= 10 and re.search(r'[\u4e00-\u9fa5]', cand):
                    # 默认当人物；后续由用户手动改类型或由 Character 表/技能标题纠正
                    add('characters', cand)
        except Exception:
            pass
    except Exception:
        # 绝对不抛异常到上层 → 防止整个 /api/books/:id/entities 500
        return



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


# ---------- M1b: EntityHub 统一注册表（从事件日志 + 章节内容增量更新） ----------

ENTITY_TYPES = {
    'characters': {'label': '人物', 'default_weight': 5},
    'factions': {'label': '势力', 'default_weight': 4},
    'locations': {'label': '地点', 'default_weight': 4},
    'items': {'label': '物品', 'default_weight': 3},
    'skills': {'label': '技能', 'default_weight': 3},
}


def _load_registry(bb) -> Dict:
    """加载实体注册表。结构：{characters: {name: {aliases, refs, first_seen_ch, last_seen_ch, weight}}, ...}"""
    if not bb or not bb.entity_registry_json:
        return {k: {} for k in ENTITY_TYPES}
    try:
        data = json.loads(bb.entity_registry_json)
        if isinstance(data, dict):
            return {k: data.get(k, {}) for k in ENTITY_TYPES}
    except Exception:
        pass
    return {k: {} for k in ENTITY_TYPES}


def _save_registry(bb, registry: Dict):
    if not bb:
        return
    bb.entity_registry_json = json.dumps(registry, ensure_ascii=False)


def _normalize_name(name: str) -> str:
    name = (name or '').strip()
    # 去掉常见前缀/后缀噪声
    name = re.sub(r'^[“"]?|[”"]?$', '', name)
    return name


def _is_valid_name(name: str) -> bool:
    if not name:
        return False
    if len(name) < 2 or len(name) > 30:
        return False
    # 排除纯数字、纯英文短词
    if re.match(r'^\d+$', name):
        return False
    if re.match(r'^[a-zA-Z]{1,3}$', name):
        return False
    return True


def register_event_entities(bb, events: List[Dict], source_chapter: int = 0):
    """从事件列表增量更新实体注册表。
    events: StoryEvent.to_dict() 列表或含 actors/location 的字典列表
    """
    if not bb or not events:
        return
    registry = _load_registry(bb)

    def _touch(bucket: str, name: str, ref_type: str):
        name = _normalize_name(name)
        if not _is_valid_name(name):
            return
        if name not in registry[bucket]:
            registry[bucket][name] = {
                'aliases': [],
                'refs': [],
                'first_seen_ch': source_chapter,
                'last_seen_ch': source_chapter,
                'weight': ENTITY_TYPES[bucket]['default_weight'],
            }
        entry = registry[bucket][name]
        entry['last_seen_ch'] = max(entry.get('last_seen_ch', 0), source_chapter)
        if ref_type not in entry['refs']:
            entry['refs'].append(ref_type)

    for ev in events:
        if not isinstance(ev, dict):
            continue
        ch_num = ev.get('chapter_num') or source_chapter
        for actor in ev.get('actors', []):
            _touch('characters', actor, 'event')
        loc = ev.get('location')
        if loc:
            _touch('locations', loc, 'event')
        # 物品/技能从 tags 或 type 推断（llm 抽取时可能给出）
        if ev.get('type') == 'item':
            # 从 summary 取前 10 字作为物品名候选（较糙，后续可由 LLM 精确抽取）
            candidate = ev.get('summary', '').split('，')[0][:12]
            _touch('items', candidate, 'event')

    _save_registry(bb, registry)


def register_chapter_entities(bb, chapter_num: int, content: str, known_actors: List[str] = None):
    """从章节内容中增量发现已知人物的别名/新出现人物。
    目前策略：扫描所有已知人物名，若本章出现则更新 last_seen_ch。
    """
    if not bb or not content:
        return
    registry = _load_registry(bb)
    known_actors = known_actors or []

    # 1. 更新已有角色的 last_seen_ch
    for name, entry in list(registry.get('characters', {}).items()):
        if name in content:
            entry['last_seen_ch'] = max(entry.get('last_seen_ch', 0), chapter_num)

    # 2. 已知演员表（Character 表）中的人物，若未在注册表则加入
    for name in known_actors:
        if _is_valid_name(name) and name not in registry.get('characters', {}):
            registry['characters'][name] = {
                'aliases': [],
                'refs': ['character_table'],
                'first_seen_ch': chapter_num,
                'last_seen_ch': chapter_num,
                'weight': 6,
            }
        elif _is_valid_name(name):
            registry['characters'][name]['last_seen_ch'] = max(
                registry['characters'][name].get('last_seen_ch', 0), chapter_num)

    _save_registry(bb, registry)


def extract_and_save_registry(bb, chapters_query=None) -> Dict:
    """统一入口：运行全量抽取（extract_entities）后，把结果合并写回 bb.entity_registry_json。
    - 返回的格式与 extract_entities 一致（{characters, factions, ...} 列表），前端 list_entities 直接用。
    - 同步写入 entity_registry_json（后续 register_event_entities 增量更新能识别到）。
    - chapters_query 可选：传入 Chapter list 后会额外从章节标题/正文抽实体。"""
    if not bb:
        return {'characters': [], 'factions': [], 'locations': [], 'items': [], 'skills': []}
    entities = extract_entities(bb, chapters_query=chapters_query)
    # 合并写入 registry：不覆盖 first_seen_ch/last_seen_ch/weight 已有值，缺失字段用默认值补
    registry = _load_registry(bb)
    ref_type = 'bible_scan'
    for bucket_name in ENTITY_TYPES:
        bucket = entities.get(bucket_name) or []
        for item in bucket:
            name = (item.get('name') or '').strip()
            if not _is_valid_name(name):
                continue
            if name not in registry[bucket_name]:
                dim_refs = item.get('dim_refs') or []
                registry[bucket_name][name] = {
                    'aliases': [],
                    'refs': list(dim_refs) if isinstance(dim_refs, list) else [ref_type],
                    'first_seen_ch': 0,
                    'last_seen_ch': 0,
                    'weight': ENTITY_TYPES[bucket_name]['default_weight'],
                }
            else:
                entry = registry[bucket_name][name]
                dim_refs = item.get('dim_refs') or []
                if isinstance(dim_refs, list):
                    refs = entry.setdefault('refs', [])
                    for r in dim_refs:
                        if r not in refs:
                            refs.append(r)
    _save_registry(bb, registry)
    return entities

