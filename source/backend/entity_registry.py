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


# ========= 新增 P0：严格噪声过滤 & 人名/地名/势力/物品/技能 形貌判定 =========

# 常见中文姓氏（百家姓 + 常见网文复姓，用于"像不像人名"判断）
_CHINESE_SURNAMES = set('''赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜
戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐
费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄
和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁
杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍
虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚
程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓
牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙
叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双
闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农
温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘
匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空
曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公''')
# 复姓
_CHINESE_COMPOUND_SURNAMES = {
    '欧阳', '太史', '端木', '上官', '司马', '东方', '独孤', '南宫', '万俟', '闻人',
    '夏侯', '诸葛', '尉迟', '公羊', '赫连', '澹台', '皇甫', '宗政', '濮阳', '公冶',
    '太叔', '申屠', '公孙', '慕容', '仲孙', '钟离', '长孙', '宇文', '司徒', '鲜于',
    '司空', '闾丘', '子车', '亓官', '司寇', '巫马', '公西', '颛孙', '壤驷', '公良',
    '漆雕', '乐正', '宰父', '谷梁', '拓跋', '夹谷', '轩辕', '令狐', '段干', '百里',
    '呼延', '东郭', '南门', '羊舌', '微生', '公户', '公玉', '公仪', '梁丘', '公仲',
    '公上', '公门', '公山', '公坚', '左丘', '公伯', '西门', '公祖', '第五', '公乘',
    '贯丘', '公皙', '南荣', '东里', '东宫', '仲长', '子书', '子桑', '即墨', '达奚',
    '褚师', '吴铭',
}

# 势力/门派尾缀（命中就像 faction，绝不可能是 character）
_FACTION_SUFFIXES = (
    '宗','门','派','教','寺','观','宫','殿','阁','楼','堂','院','会','盟','团','营','军',
    '司','府','署','局','帮','会','社','族','氏','家','堡','寨','岛','城','国','州','道',
    '联盟','商会','军团','阵营','家族','宗门','门派','道统','世家','镖局','赌坊','青楼',
    '皇室','王府','侯府','公府','伯府','朝堂','内阁','军机处','六部','行省','世家大族',
)
# 地点尾缀（命中 → location）
_LOCATION_SUFFIXES = (
    '城','市','镇','村','庄','乡','县','郡','府','州','省','国','岛','湾','港','山','峰','岭',
    '谷','峡','关','隘','原','野','林','森','湖','河','江','海','洋','潭','溪','泉','滩','沙',
    '漠','草原','沙漠','沼泽','秘境','遗迹','战场','旧址','废墟','深渊','界','域','位面','大6',
    '大陆','海域','山脉','盆地','高原','平原','丘陵','洞穴','洞窟','塔楼','桥','码头','车站',
    '学院','学府','书院','宗门驻地','驻地','秘境','禁地','试炼场','角斗场','擂台',
)
# 物品尾缀/特征（item）
_ITEM_SUFFIXES = (
    '剑','刀','枪','棍','棒','戟','斧','钩','叉','鞭','锏','锤','弓','箭','弩','矢','甲','铠',
    '盔','靴','袍','衣','裙','带','佩','环','镯','链','戒','玉','珠','石','丹','药','草','花',
    '果','茶','酒','符','印','令','牌','旗','伞','扇','琴','棋','书','画','钟','鼎','炉','塔',
    '舟','船','辇','车','马','袋','囊','盒','匣','盘','杯','盏','瓶','壶','杖','鞭','索','绳',
    '图','卷','册','碑','镜','冠','巾','簪','钗','宝','材料','矿石','金属','木材','兽材',
)
# 技能/功法尾缀（skill）
_SKILL_SUFFIXES = (
    '诀','法','术','招','式','阵','咒','印','功','心法','秘法','神通','武学','招式','斗技',
    '魔法','法术','咒文','奥义','禁术','玄功','灵诀','剑诀','刀诀','枪法','棍法','掌法','指法',
    '步法','身法','吐纳','冥想','锻体','炼体','拳','掌','腿','爪','指',
)

# 时间/年龄/阶段/数字噪声关键词 —— 一旦在 label 或候选名字里出现，且带数字→直接视为噪声
_TIME_AGE_WORDS = (
    '岁','年','月','日','时','点','分','秒','季','旬','周','星期','礼拜','农历','正月','腊月',
    '春','夏','秋','冬','早上','上午','中午','下午','晚上','凌晨','黄昏','傍晚','午夜','深夜',
    '纪元','朝代','时期','阶段','跨度','时长','龄','届','次','代','世','纪',
)
# 章/卷/节/回/幕 序号噪声关键词
_SECTION_WORDS = ('章','卷','节','回','幕','集','场','话','篇','部','册')
# 明确的"不是名字"的停用词/说明词/虚词
_STOP_WORDS = set('''我们 你们 他们 她们 它们 自己 咱们 大家 人家 别人 旁人
一个 两个 三个 四个 五个 一些 有些 所有 全部 整个 部分 其中 之一 而已
然后 后来 之后 之前 以前 以后 现在 于是 因此 所以 但是 然而 而且 并且
其实 当然 显然 果然 居然 竟然 突然 忽然 渐渐 终于 始终 一直 从来 永远
或许 也许 大概 大约 几乎 差不多 左右 上下 以内 以外 至少 最多 最少 不超过
主要 核心 重要 关键 普通 一般 常见 非常 特别 十分 极其 相当 比较 稍微
什么 怎么 为什么 哪里 哪个 哪些 怎样 如何 如果 即使 虽然 因为 所以
第一 第二 第三 第四 第五 第六 第七 第八 第九 第十 首先 其次 然后 最后
开始 结束 过程 结果 情况 状态 方式 方法 方面 问题 原因 影响 关系 意义
以上 以下 以内 以外 之内 之外 之前 之后 之间 目前 如今 至今 从此 以后
主角 配角 反派 龙套 客串 简介 说明 介绍 概述 总结 备注 注释 提示 注意 警告
所述 所说 所言 所写 所谓 所在 所思 所感 所为 所述的 总的来说 总而言之 综上所述'''.split())

# 维度噪声收紧策略：timeline / outline / style_guide / foreshadowing_graph 只允许"明确类别前缀冒号行"识别，不开兜底
_STRICT_DIM_FIELDS = {
    'timeline', 'foreshadowing_graph', 'outline_hierarchy', 'style_guide',
    'foreshadowing', 'plot_design',  # 大纲/文风/伏笔也紧一点，避免把"第三章：XXX"当人物
}


def _is_valid_entity_name(name: str, allow_digit_ratio: float = 0.4) -> bool:
    """统一"名字像不像实体"的判定入口：
    - 结构：2-18 字，不能过短/过长
    - 不是纯数字/纯短英文
    - **强噪声过滤：**
      ①含句号/问号/感叹号/分号/省略号 → 整句，丢
      ②含"岁+数字"（年龄）、"年月/日/时+数字"（时间） → 丢；**含中文数字（第X/卷X/X品/X阶）+ 章节/品级字也丢**
      ③含"章/卷/节/回/幕/集 + 数字或中文序数" → 序号，丢
      ④数字占比 > allow_digit_ratio → 丢（"16岁3个月"里数字+单位占比高）
      ⑤属于通用停用词 / 纯虚词串 → 丢
      ⑥度量衡长度/重量/境界数短语（"5丈/300里/七品/八阶"）→ 丢
    """
    if not name:
        return False
    name = name.strip()
    n = len(name)
    if n < 2 or n > 18:
        return False
    # ① 整句标点噪声
    if any(ch in name for ch in '。？！；!?;…—-~') and n > 10:
        return False
    # 纯数字/纯短英文
    if re.fullmatch(r'\d+', name):
        return False
    if re.fullmatch(r'[A-Za-z]{1,3}', name):
        return False
    # 数字占比
    digit_cnt = sum(1 for ch in name if ch.isdigit())
    if digit_cnt / max(n, 1) > allow_digit_ratio:
        return False
    # ② 年龄/时间噪声（阿拉伯数字版）
    if re.search(r'\d+\s*岁', name) or ('至' in name and '岁' in name):
        return False
    # ②+ 时间/章节/品级 + 中文数字（"第三章/卷二/第五回/七品/八阶/三坛"）
    CN_NUM = '零〇一二三四五六七八九十百千万两'
    # 章节/卷/回/幕字 + 含中文数字/阿拉伯数字 → 噪声
    if any(w in name for w in _SECTION_WORDS):
        # 必须带数字（含中文数字）才是"第X章"类序号，比如"章台柳"这种真实人名/诗名不能误伤
        has_num = bool(re.search(r'\d', name)) or any(c in name for c in CN_NUM)
        if has_num or name.startswith('第'):
            return False
    # 时间词 + 数字/中文数字 → 噪声
    if any(w in name for w in _TIME_AGE_WORDS):
        has_num = bool(re.search(r'\d', name)) or any(c in name for c in CN_NUM)
        if has_num:
            return False
    # ④ 度量衡/品级长度重量等（含中文数字也命中）
    if re.search(rf'[\d{CN_NUM}]+\s*(丈|尺|寸|米|公里|里|斤|两|钱|克|吨|阶|品|级|层|重|段|等)', name):
        return False
    # ⑤ 通用停用词 / 纯虚词串：
    if name in _STOP_WORDS:
        return False
    # ⑤+ 长度 ≤ 8 且所有字符都能被 _STOP_WORDS 中任意一个词覆盖（纯虚词拼凑如"所以但是""以上所述""然后之后"）
    if n <= 8:
        # 贪心分词覆盖
        remain = name
        changed = True
        while changed and remain:
            changed = False
            for w in sorted(_STOP_WORDS, key=len, reverse=True):
                if remain.startswith(w):
                    remain = remain[len(w):]
                    changed = True
                    break
        if not remain:
            return False
    # 中文虚词纯串（字级）：所有字符都属于这组虚词字 → 丢
    EMPTY_CHARS = '所与或者然虽则因且故此之其于及并如果那这虽但而已还是又也都却只就再还'
    if all(ch in EMPTY_CHARS for ch in name):
        return False
    return True


def _looks_like_faction(name: str) -> bool:
    # 只看尾缀命中（"玄骨宗/镇魔司/大胤皇室/姜家/天星商会/青云联盟"）
    return isinstance(name, str) and any(name.endswith(s) for s in _FACTION_SUFFIXES)


def _looks_like_location(name: str) -> bool:
    return isinstance(name, str) and any(name.endswith(s) for s in _LOCATION_SUFFIXES)


def _looks_like_item(name: str) -> bool:
    return isinstance(name, str) and any(name.endswith(s) for s in _ITEM_SUFFIXES)


def _looks_like_skill(name: str) -> bool:
    return isinstance(name, str) and (
        any(name.endswith(s) for s in _SKILL_SUFFIXES)
        or ('之' in name and name.endswith(('功', '法', '诀', '术', '招', '式', '典', '录', '经')))
    )


def _looks_like_person_name(name: str) -> bool:
    """判定像不像中文人名：2-4 字纯中文(不含数字)，首字落在常见姓氏或前 2 字复姓。"""
    if not _is_valid_entity_name(name):
        return False
    if re.search(r'\d', name):  # 名字里不含数字（有数字就可能是时间/品级/度量）
        return False
    if not re.fullmatch(r'[\u4e00-\u9fa5]{2,4}', name):
        return False
    # 复姓
    if len(name) >= 2 and name[:2] in _CHINESE_COMPOUND_SURNAMES:
        return True
    # 单字姓
    return name[0] in _CHINESE_SURNAMES


def _is_noise_label(label: str) -> bool:
    """判断行冒号前的 label 是否是"时间/年龄/阶段/序号/说明词"。
    如果是噪声 label → 这一行不要做任何默认塞桶，否则会出现 timeline 里 "16岁3个月：主角觉醒灵根"
    被识别成"16岁3个月"是个人物这种 bug（用户截图中的 874 条时间乱入的根因）。"""
    if not label:
        return True
    label = label.strip()
    # 过短/过长 → 噪声
    if len(label) < 1 or len(label) > 20:
        return True
    # 整句 label（说明/解释行）
    if any(ch in label for ch in '。？！；!?;'):
        return True
    # 时间/年龄+数字 → 噪声（16岁3个月、第3纪、3月15日、卷X章Y-Z 等）
    if re.search(r'\d', label):
        for w in list(_TIME_AGE_WORDS) + list(_SECTION_WORDS):
            if w in label:
                return True
    # 纯说明性词语 label（不含"人名/势力/地点/物品/功法"类关键词，又是纯概念）
    # 含"说明/介绍/概述/简介/备注/总结/提示/注意/正文/标题/章节/梗概/剧情/概要/阶段/背景/过程/结果"且不带人物/势力/地点/物品/功法前缀 → 噪声
    EXPLAIN_WORDS = (
        '说明','介绍','概述','简介','备注','总结','提示','注意','警告','正文','标题',
        '章节','梗概','剧情','概要','背景','过程','结果','原因','目标','任务','奖励',
        '惩罚','影响','意义','状态','情况','方式','方法','方面','问题','关系',
    )
    cat_hint_hit = _category_from_label(label) is not None
    if any(w in label for w in EXPLAIN_WORDS) and not cat_hint_hit:
        return True
    # label 数字占比过高（纯阶段号）
    digit_cnt = sum(1 for ch in label if ch.isdigit())
    if digit_cnt / max(len(label), 1) > 0.35:
        return True
    # label 含度量衡+数字（"三品/五阶/300里/5丈"这种品阶/尺寸/长度短语 → 不是实体）
    if re.search(r'\d+\s*(丈|尺|寸|米|公里|里|斤|两|钱|阶|品|级|层|重|段|等)', label):
        return True
    return False


def _categorize_candidate(name: str):
    """基于尾缀/形态猜测候选属于哪一 bucket，返回 categories 列表（可能性从高到低）。
    若完全猜不出来且不像人名 → 返回空列表，调用方就不塞任何桶（核心：**不再默认塞角色**）。"""
    cats = []
    if _looks_like_person_name(name):
        cats.append('characters')
    if _looks_like_faction(name):
        cats.append('factions')
    if _looks_like_location(name):
        cats.append('locations')
    if _looks_like_skill(name):
        cats.append('skills')
    if _looks_like_item(name):
        cats.append('items')
    return cats


# 常见"实体关键词前缀"（出现在冒号/竖线前，用来判定这行冒号前的东西是什么类型）
_CATEGORY_HINTS = {
    'characters': {'姓名', '名字', '人物', '角色', '主角', '反派', '配角', '龙套', '师父', '师傅',
                   '师兄', '师弟', '师姐', '师妹', '父亲', '母亲', '儿子', '女儿', '兄弟', '姐妹',
                   '族长', '长老', '掌门', '城主', '皇帝', '殿下', '公子', '小姐', '演员', '登场人物'},
    'factions': {'势力', '门派', '宗门', '宗派', '阵营', '家族', '商会', '军团', '联盟', '道统', '教', '寺', '楼', '帮会'},
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
        # ===== 【P0 修复：统一强噪声过滤，不再两句话/时间/长句乱入】=====
        if not _is_valid_entity_name(name):
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

        # 判断当前维度是不是"严格模式"：timeline/大纲/文风/伏笔 等，只开明确类别前缀匹配，不开无差别括号/竖线兜底
        # ref 示例：'timeline'、'dynamic_volumes.timeline'、'chapter.content'
        strict_mode = False
        for sd in _STRICT_DIM_FIELDS:
            if ref == sd or ref.endswith('.' + sd):
                strict_mode = True
                break

        def safe_add_unknown(name):
            """未知类别候选：按形貌猜测 1-N 个桶；都猜不到就丢，**绝不默认塞角色**。"""
            if not _is_valid_entity_name(name):
                return
            cats = _categorize_candidate(name)
            # 去重保序：同一名字如果既像人物又像地点（极少），两个桶都加
            seen = set()
            for c in cats:
                if c not in seen:
                    seen.add(c)
                    add(c, name)

        # A. 行级扫："XX 标签(类别提示)：实体名1、实体名2、实体名3"
        for line in re.split(r'[\r\n]+', text):
            try:
                if not line or len(line) > 400:
                    continue
                # 冒号：前半是标签，后半是"顿号/逗号/分号"枚举实体
                m = re.match(r'^\s*[-*•·]*\s*([^：:\n]{1,30})\s*[：:]\s*(.+?)\s*$', line)
                if m:
                    label = m.group(1).strip()
                    content = m.group(2).strip()
                    # =====【P0：label 是噪声 → 整行跳】=====
                    if _is_noise_label(label):
                        continue
                    cat = _category_from_label(label)
                    # 后半按顿号/逗号/分号切
                    candidates = [x.strip(' 、') for x in re.split(r'[、,，;；]+', content) if x.strip()]
                    for cand in candidates:
                        # 有时会写成"林墨（男·19岁）" → 先保留括号前主名
                        main = re.split(r'[（(（【《]', cand, 1)[0].strip()
                        if not _is_valid_entity_name(main):
                            continue
                        if cat:
                            # 有明确类别前缀（"人物："、"势力："等）→ 直接进对应桶
                            add(cat, main)
                        else:
                            # 前缀没命中但前缀不是噪声（如"玄骨宗：""姜无涯："）→ 形貌猜桶，绝不默认塞角色
                            safe_add_unknown(main)
                    continue

                # 竖线分隔："林墨 | 男 | 19 岁 | 主角" → 第1段必须像人名才加（严格模式关闭）
                if (not strict_mode) and '|' in line:
                    parts = [p.strip() for p in line.split('|') if p.strip()]
                    if len(parts) >= 2:
                        cand0 = parts[0]
                        if _looks_like_person_name(cand0):
                            add('characters', cand0)
            except Exception:
                continue

        # B. 段级扫：
        #    1) 「主要人物：林墨、苏清鸢」"人物/势力/地点/物品/功法" 前缀枚举实体
        try:
            m2 = re.findall(r'(?:主要|核心|关键|重要|其他|已登场)?\s*'
                            r'(人物|角色|势力|门派|宗门|阵营|地点|区域|物品|功法|技能|法宝|丹药)'
                            r'[：:]\s*([^\n]{2,200})', text)
            for label, body in m2:
                cat = _category_from_label(label)
                if not cat:
                    # 极端情况没命中 → 丢，不默认塞角色
                    continue
                for cand in re.split(r'[、,，;；\s]+', body):
                    cand = cand.strip()
                    if not cand:
                        continue
                    main = re.split(r'[（(【《]', cand, 1)[0].strip()
                    if _is_valid_entity_name(main):
                        add(cat, main)
        except Exception:
            pass

        # C. 【姓名】/"姓名"/《功法名》：严格模式关闭；不同括号类型对应不同 bucket
        if not strict_mode:
            try:
                brackets_sq = re.findall(r'【([^\]】]{1,12})】', text)
                brackets_dq = re.findall(r'[\"“”]([^\"“”\n]{1,14})[\"“”]', text)
                brackets_book = re.findall(r'《([^》\n]{1,16})》', text)
                brackets_la = re.findall(r'〈([^〉\n]{1,12})〉', text)
                # 【】/引号 → 形貌猜桶，猜不到丢
                for cand in brackets_sq:
                    safe_add_unknown(cand.strip())
                for cand in brackets_dq:
                    safe_add_unknown(cand.strip())
                # 《》→ 优先技能/功法，其次物品（典籍类），绝不再默认塞角色
                for cand in brackets_book:
                    cand = cand.strip()
                    if not _is_valid_entity_name(cand):
                        continue
                    if _looks_like_skill(cand):
                        add('skills', cand)
                    elif _looks_like_item(cand):
                        add('items', cand)
                    else:
                        add('items', cand)
                for cand in brackets_la:
                    safe_add_unknown(cand.strip())
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
    """增量入口（事件/章节）的名字有效性统一走强噪声过滤，和抽取入口一致。
    以前只过滤长度/纯数字/短英文，会漏大量"16岁3个月/第三章/三品丹方"这种非实体短语。"""
    return _is_valid_entity_name(name, allow_digit_ratio=0.35)


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
    - chapters_query 可选：传入 Chapter list 后会额外从章节标题/正文抽实体。

    【P0 修复】：
    1. **历史脏数据清理**：以本次扫描 entities 作为真值，若旧 registry 里存在"完全由 bible_scan 产生且本次不再命中"的条目
       （典型就是用户截图里的 874 条"16岁3个月/16岁4个月…"时间短语由旧版 timeline 扫入）→ 从 registry 中删除，
       用户一点"🔄 刷新"就自动干净，不再需要手动逐条删。
    2. **所有写回前先过 _is_valid_name**（已升级为强噪声过滤），避免 events 增量写回残留的非实体短语。
    """
    if not bb:
        return {'characters': [], 'factions': [], 'locations': [], 'items': [], 'skills': []}
    entities = extract_entities(bb, chapters_query=chapters_query)
    registry = _load_registry(bb)
    ref_type = 'bible_scan'

    # 先收集本次扫描命中的所有 name（按 bucket）
    current_names: Dict[str, set] = {}
    for bucket_name in ENTITY_TYPES:
        bucket = entities.get(bucket_name) or []
        current_names[bucket_name] = set()
        for item in bucket:
            n = (item.get('name') or '').strip()
            if _is_valid_name(n):
                current_names[bucket_name].add(n)

    # 外部增量来源：只要 refs 里包含这些来源之一，说明不是"纯 Bible 扫描历史残留"，就不能因为
    # 本次 Bible 重抽没抽到就删掉（可能是事件日志/Character 表/章节正文增量识别的）。
    EXTERNAL_REF_SOURCES = {'event', 'chapter', 'character_table'}

    for bucket_name in ENTITY_TYPES:
        # === A. 清理历史脏数据（本次未命中 & 无外部来源）===
        old_entries = list(registry[bucket_name].keys())
        for old_name in old_entries:
            if old_name in current_names[bucket_name]:
                continue
            entry = registry[bucket_name][old_name]
            refs = entry.get('refs') or []
            has_external = any(r in EXTERNAL_REF_SOURCES for r in refs)
            if has_external:
                # 有外部来源 → 保留（即使 Bible 没抽到，事件/正文/Character 表里确实存在）
                # 但强过滤一下：如果名字现在已经被判定成噪声，强制删除（避免事件日志本身有脏）
                if not _is_valid_name(old_name):
                    del registry[bucket_name][old_name]
                continue
            # 纯 bible_scan 历史残留 & 本次未命中 → 一定是旧逻辑误识别的噪声，直接删除
            del registry[bucket_name][old_name]

        # === B. 把本次 Bible 扫描结果合并写回（保留 first_seen_ch/last_seen_ch/weight）===
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
                # 若 weight 缺失 → 补齐（防止老用户数据 upgrade）
                if 'weight' not in entry or not entry['weight']:
                    entry['weight'] = ENTITY_TYPES[bucket_name]['default_weight']

    _save_registry(bb, registry)
    return entities

