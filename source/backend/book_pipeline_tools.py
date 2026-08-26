"""爆款方案流水线工具链（3步）。

独立于 chat_collab_bp 之外，保持原模块不超架构门禁基线。
被 chat_collab_bp 的4个薄路由直接调用。

Step 1: realtime_scan_rank(topic, refs=[]) → 趋势方向报告
        优先级：Web搜索+抓取热榜页面 → LLM归纳 → 失败回退知识库头部作品
Step 2: generate_5_plans(topic, trend_report, refs=[]) → 5方案×3方向 + 自洽验证
Step 3: build_worldbuild_package(plan_dict) → 世界观+修炼+CDL角色+金手指代价+系统人格化
"""
from __future__ import annotations
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Optional


# ============================================================================
# 小说网站热榜抓取入口（WebFetch 格式 + 可回退启发式关键词提取）
# ============================================================================
SITE_SPECS: dict[str, dict[str, Any]] = {
    # ===== 第一优先级：真实小说榜单源（用户指定的3个权威网站，抓真实书名/作者/热度）=====
    'shuhuangdian': {
        'name': '书荒典·热点小说+番茄在读榜+起点万订',
        'search_url_tpl': 'https://www.shuhuangdian.com/mobile/attention',
        'topic_search_tpl': 'https://www.shuhuangdian.com/',
        'priority': 1,
    },
    'wangwendashuju': {
        'name': '网文大数据·番茄首秀榜/起点月票榜/抖音漫剧红果短剧',
        'search_url_tpl': 'https://www.wangwendashuju.com/home',
        'topic_search_tpl': 'https://www.wangwendashuju.com/',
        'priority': 1,
    },
    'fanqiehub': {
        'name': '番茄Hub·上升最快/新书榜晋升阅读榜',
        'search_url_tpl': 'https://www.fanqiehub.com/',
        'topic_search_tpl': 'https://www.fanqiehub.com/#content-anchor',
        'priority': 1,
    },
    # ===== 第二优先级：按题材搜索（保留作为 fallback）=====
    'fanqie': {
        'name': '番茄小说',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+番茄小说+排行榜+热门',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=番茄小说+{t}+热门+推荐+前十',
        'priority': 2,
    },
    'qidian': {
        'name': '起点中文网',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+起点中文网+排行榜+热门推荐',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=起点中文网+{t}+热门+前十+完本',
        'priority': 2,
    },
    'qimao': {
        'name': '七猫小说',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+七猫小说+排行榜+热门',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=七猫小说+{t}+热门+推荐',
        'priority': 2,
    },
}

# 知识库回退（当联网失败时给出的通用"头部作品"基准，按题材区分，带明显"非实时"标记）
KNOWLEDGE_BASE_FALLBACK: dict[str, dict[str, Any]] = {
    '都市异能': {
        'sample_books': ['《全球高武》', '《大王饶命》', '《我有一座恐怖屋》', '《诡秘之主》(都市异能旁支)',
                         '《最强反套路系统》都市篇', '《超神制卡师》'],
        'common_golden_finger': ['系统面板流', '每日签到流', '异能觉醒血统流', '身份反差（白天平凡晚上大佬）'],
        'common_pleasures': ['越级反杀爽', '身份反差装逼', '打脸反派不隔夜', '系统奖励白嫖'],
        'openings': ['开篇第一事件就触发异能觉醒 / 系统绑定', '第一章就打脸校园/职场反派', '路人视角先渲染世界观危险'],
        'characters': ['嘴贱贫主角', '冰山女主但私下反差', '搞笑担当兄弟配角', '权势背景女反派'],
        'cliche_warnings': ['校花倒贴 + 系统纯无敌无代价（同质化严重）', '开篇前十章无主线只装逼'],
    },
    '玄幻高武': {
        'sample_books': ['《斗破苍穹》', '《遮天》', '《完美世界》', '《牧神记》', '《诡秘之主》'],
        'common_golden_finger': ['老爷爷器灵', '神级选择系统', '血脉返祖', '时空轮回能力'],
        'common_pleasures': ['退婚打脸', '拍卖场装逼', '天才战第一', '宗门长老惊叹'],
        'openings': ['天才跌落废柴+退婚开局', '主角穿越附体弱少爷', '秘境奇遇开场'],
        'characters': ['隐忍腹黑主角', '妖族/血族身份反差女主', '忠心耿耿傻大个兄弟'],
        'cliche_warnings': ['退婚→三年之约（套路过熟）', '宗门大比必越级'],
    },
    '系统文': {
        'sample_books': ['《最强反套路系统》', '《我有一个小世界》', '《神级学霸系统》', '《每日签到十万年》'],
        'common_golden_finger': ['每日签到', '任务面板+奖励', '新手礼包神装', '随机抽奖'],
        'common_pleasures': ['白嫖奖励爽', '强制任务推动剧情', '升级进度条可视化'],
        'openings': ['系统绑定第一秒就送大礼包', '任务失败惩罚极端→主角玩命'],
        'characters': ['吐槽役主角', '系统人格化话痨', 'NPC式路人惊叹'],
        'cliche_warnings': ['系统纯发号施令机器', '奖励发太多导致无张力'],
    },
    '历史脑洞': {
        'sample_books': ['《明朝败家子》', '《带着仓库到大明》', '《庆余年》', '《赘婿》'],
        'common_golden_finger': ['现代知识/物品穿越大礼包', '历史全知视角', '系统种田工坊'],
        'common_pleasures': ['用现代产品打古人脸', '改写历史遗憾', '科技碾压工业革命'],
        'openings': ['开局穷山沟+全家挨饿', '穿到某历史人物身上立刻遇到大事'],
        'characters': ['穿越毒舌工科生', '老实古人兄弟', '权谋型女主'],
        'cliche_warnings': ['所有古人都是傻白甜', '科技树攀得比现代还快'],
    },
}
# 通用兜底（未知题材）
_GENERIC_FALLBACK = {
    'sample_books': ['[头部畅销题材样本，非实时]'],
    'common_golden_finger': ['差异化金手指', '反套路开局'],
    'common_pleasures': ['即时爽+延迟爽结合'],
    'openings': ['第一句即冲突或反常'],
    'characters': ['有瑕疵主角+反差人物'],
    'cliche_warnings': ['同质化模板要避开'],
}


def _pick_fallback(topic: str) -> dict:
    """题材 → 知识库回退样本匹配。"""
    if not topic:
        return _GENERIC_FALLBACK
    for key, v in KNOWLEDGE_BASE_FALLBACK.items():
        if any(k in topic for k in key.split('|')):
            return v
    # 模糊匹配关键词
    fuzzy = [
        ('都市', KNOWLEDGE_BASE_FALLBACK.get('都市异能')),
        ('玄幻|高武|修仙|仙侠', KNOWLEDGE_BASE_FALLBACK.get('玄幻高武')),
        ('系统', KNOWLEDGE_BASE_FALLBACK.get('系统文')),
        ('历史|穿越|种田|大明|大唐|三国', KNOWLEDGE_BASE_FALLBACK.get('历史脑洞')),
    ]
    for k, v in fuzzy:
        if v and re.search(k, topic):
            return v
    return _GENERIC_FALLBACK


def _encode_full_url(url: str) -> str:
    """解决 SITE_SPECS.topic_search_tpl 本身含中文（如"番茄小说"）导致 urllib.request.Request 抛 UnicodeEncodeError('ascii')。

    对整个 URL 做「IRI→URI」转换：非 ASCII 字符 percent 编码，保留 %、/、:、?、=、&、# 等已合法字符（避免二次编码已 percent 的部分）。
    同时把中文空格「+」保留（作为 query word 分隔符，encode 时 safe 已包含）。
    """
    if not isinstance(url, str):
        return str(url)
    return urllib.parse.quote(url, safe=r"/:?=&%#+.@-_,~()*!$'")


# ============================================================================
# 真实榜单解析：从 shuhuangdian / wangwendashuju / fanqiehub 抓取的 HTML/Markdown
# 中提取真实书名（解决"过来过去都是那几本知识库固定书"的假扫榜问题）
# ============================================================================

# 高频"金手指/爽点/题材"关键词词典（从真实榜源的书名里高频计数反推当前火什么）
_TREND_HINTS = {
    'golden_finger': [
        '系统', '签到', '面板', '悟性', '返利', '模拟器', '多子多福', '抽奖', '兑换',
        '随身', '空间', '无限', '异能', '灵根', '血脉', '神魂', '召唤', '分身', '金乌', '悟性',
    ],
    'pleasure': [
        '重生', '穿越', '开局', '全民', '诡异', '恐怖', '高武', '修仙', '长生', '苟道',
        '无敌', '反杀', '打脸', '种田', '赘婿', '战神', '校花', '学霸', '末世', '科举', '四合院',
    ],
}


def _extract_real_books_from_html(html: str) -> list[str]:
    """从任意榜单网站返回的HTML/Markdown中提取真实书名（支持3个权威站+兼容未来扩展）。

    去重后返回 list[str]，每本不包《》号，长度范围2-30字。
    """
    if not html or not isinstance(html, str):
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str):
        if not raw:
            return
        s = raw.strip().strip('《》「」""\'[]()（）').strip()
        # 清掉开头数字排名："1绝区零…"→"绝区零…" / "1捞尸人…"→"捞尸人…"
        s = re.sub(r'^\d+\s*', '', s)
        # 清掉 Markdown 排名粗体尾 ** 或星星 ★
        s = re.sub(r'[\*★]+.*$', '', s).strip()
        # 清掉 HTML tag 残留/URL 分隔符
        s = re.sub(r'<[^>]+>', '', s).strip()
        if not s or len(s) < 2 or len(s) > 40:
            return
        if s in seen:
            return
        # 过滤纯 URL / 导航文案
        if any(k in s for k in ['https://', 'http://', '更多', '查看更多', '完整榜单', '立即开通']):
            return
        seen.add(s)
        out.append(s)

    # ---------- Pattern 1：Markdown 排名标题（书荒典用）----------
    # ###? [书名](url)  或 ### 书名（没有链接）
    for m in re.finditer(r'#{2,4}\s*\[([^\]\n]{2,50})\]\s*\(', html):
        _add(m.group(1))
    for m in re.finditer(r'#{2,4}\s*([^\[\]\n]{2,50}?)(?:\s*$|\s*【)', html):
        _add(m.group(1))
    # 书荒典"起点万订小说大全"格式（###? \d+\s*书名）
    for m in re.finditer(r'(?:^|\n)\s*\d+\s*\n\s*#{2,4}\s*([^\n#]{2,50})', html):
        _add(m.group(1))

    # ---------- Pattern 2：网文大数据 Top5 链接格式 ----------
    # 1. [1绝区零：我的数值凌驾于一切之上**2.9万在读**](...)
    # 1. [1捞尸人**7.1万月票**](...)
    for m in re.finditer(r'\[\s*\d+\s*\*?\*?(.{2,60}?)\s*\*', html):
        _add(m.group(1))
    # 兼容无粗体：[1绝区零：我的数值凌驾于一切之上](...)
    for m in re.finditer(r'\[\s*\d+\s*(.{2,60}?)\]\s*\(', html):
        _add(m.group(1))

    # ---------- Pattern 3：番茄Hub表格列 ----------
    # | **1** | 盗墓：从档案馆开始人见人爱 | 海楼的猫 | ↑22 | 2.8万 |
    # 或者非粗体排名：| 1 | 封印神源百万年，悟性每天翻倍 | 爱吃辣的鱼 | 10.5万 |
    for m in re.finditer(
        r'\|\s*\*{0,2}\d+\*{0,2}\s*\|\s*([^|\n]{2,60}?)\s*\|\s*[^|\n]{2,40}?\s*\|',
        html,
    ):
        _add(m.group(1))

    # ---------- Pattern 4：通用兜底（从 Markdown 加粗/链接 中文书名长度2-30字抽取）----------
    for m in re.finditer(r'《([^《》\n]{2,30})》', html):
        _add(m.group(1))

    return out


def _infer_trending_from_real_books(books: list[str]) -> dict:
    """从真实书名的词频反推：金手指方向、爽点类型、开篇套路、人设高频标签、雷区。
    解决"每次扫榜知识库那几本固定书"→真实榜源抓到书名后，用高频词直接反推趋势，
    不再用 fallback KNOWLEDGE_BASE_FALLBACK 的固定方向。
    """
    books = [b for b in books if isinstance(b, str) and 2 <= len(b) <= 40]
    if not books:
        return {}

    # 1. 金手指词频计数
    gf_counts: dict[str, int] = {}
    for w in _TREND_HINTS['golden_finger']:
        c = sum(1 for b in books if w in b)
        if c > 0:
            gf_counts[w] = c
    gf_top = sorted(gf_counts.items(), key=lambda x: -x[1])[:4]

    # 2. 爽点/题材词频计数
    pl_counts: dict[str, int] = {}
    for w in _TREND_HINTS['pleasure']:
        c = sum(1 for b in books if w in b)
        if c > 0:
            pl_counts[w] = c
    pl_top = sorted(pl_counts.items(), key=lambda x: -x[1])[:4]

    # 3. 书名里出现"XX+我+XX" / "开局XX" / "从XX开始" → 反推开篇套路
    opening_tags: list[str] = []
    has_start = any('开局' in b for b in books)
    has_from = any('从' in b and ('开始' in b or '做起' in b or '当' in b) for b in books)
    has_reborn = any('重生' in b for b in books)
    has_cross = any('穿越' in b or '穿成' in b for b in books)
    if has_start:
        opening_tags.append('开局事件即核心冲突（触发系统/穿越/觉醒）')
    if has_from:
        opening_tags.append('"从X开始"微视角切入（底层小角色→大佬）')
    if has_reborn:
        opening_tags.append('重生先知+前世遗憾弥补线')
    if has_cross:
        opening_tags.append('穿越附体弱少爷/宗门弃徒+金手指')
    if not opening_tags:
        opening_tags.append('开篇第一事件即强冲突（打脸/觉醒/绑架/退婚）')

    # 4. 人设标签：从书名第一/第二个词猜
    character_tags = []
    if any('我' in b[:4] for b in books):
        character_tags.append('第一人称"我"自述式（爽感代入强）')
    if any(any(k in b for k in ['大佬', '暴君', '神', '皇', '仙', '宗主']) for b in books):
        character_tags.append('身份反差（底层→高位）+ 狠人主角')
    if any(any(k in b for k in ['师姐', '师尊', '老婆', '师娘', '娇妻', '女主']) for b in books):
        character_tags.append('多女主/师徒/甜宠反差')
    if any(any(k in b for k in ['猎魔', '诡异', '鬼', '邪神', '精神病院', '十日终焉']) for b in books):
        character_tags.append('规则怪谈/中式怪诞/黑暗高武')
    if not character_tags:
        character_tags.append('主角接地气（日常职业/学生/社畜+外挂反差）')

    # 5. 节奏：从真实书名判断多为"小爽每3章+大爽每12章"，如果"开局"书多则节奏更快
    small_every = 2 if has_start else 3
    big_every = 10 if (has_reborn or has_cross) else 12

    # 6. 文风风向：真实榜多短句口语化（番茄/起点男频均值）
    # 从书名高频词猜（系统/签到/高武/修仙 → 对话占比高）
    dialog_ratio = 34 if any(any(k in b for k in ['都市', '重生', '开局', '穿越']) for b in books) else 28
    sent_tendency = '短句+口语化（一句一层意思）' if any(
        any(k in b for k in ['开局', '我', '全民', '系统']) for b in books
    ) else '长短句混合'
    colloquial = ['卧槽', '好家伙', '搞钱', '摊牌了', '不装了'] if any(
        '都市' in b for b in books
    ) else ['系统', '叮！', '触发', '恭喜宿主']

    # 7. 同质化雷区：关键词出现最多的→就是大家都在写的
    fatigue = []
    if gf_top and gf_top[0][1] >= max(3, len(books) // 3):
        fatigue.append(f'【{gf_top[0][0]}】泛滥（{gf_top[0][1]}本真实榜单在用），建议换差异化变体')
    if pl_top and pl_top[0][1] >= max(3, len(books) // 3):
        fatigue.append(f'【{pl_top[0][0]}】题材扎堆（{pl_top[0][1]}本真实榜单在用），建议加反套路钩子')
    if not fatigue:
        fatigue.append('同质化不明显（真实榜单分布均匀），可用"冷门世界观+热门爽点"组合差异化')
    diff_opport = []
    if gf_top:
        diff_opport.append(f'热门金手指"{gf_top[0][0]}"+冷门世界观（非都市/非玄幻）')
    if pl_top:
        diff_opport.append(f'热门爽点"{pl_top[0][0]}"+情绪代价/规则限制（不是纯爽无脑）')
    if not diff_opport:
        diff_opport.append('细分身份反差（如：反派视角/配角逆袭/女帝师娘倒追）')

    result = {
        'current_trending': {
            'golden_finger_directions': [f'{w}（真实榜单{c}本在用）' for w, c in gf_top] if gf_top else ['系统面板/签到流（知识库兜底）'],
            'pleasure_types': [f'{w}（真实榜单{c}本在用）' for w, c in pl_top] if pl_top else ['越级反杀/身份反差（知识库兜底）'],
            'opening_tropes': '、'.join(opening_tags[:3])[:60],
            'character_tags': character_tags[:4] or ['主角接地气（知识库兜底）'],
            'rhythm': {'small_pleasure_every_N_chapters': small_every, 'big_pleasure_every_N_chapters': big_every},
        },
        'style_wind': {
            'dialog_ratio_mean_percent': dialog_ratio,
            'sentence_tendency': sent_tendency,
            'active_colloquial_words': colloquial[:3],
        },
        'cliche_landmines': {
            'fatigue_tropes': fatigue,
            'diff_opportunities': diff_opport,
        },
    }
    return result


# ============================================================================
# Step 1: 实时扫榜（网络优先 + 知识库回退）
# ============================================================================
def run_step1_scan(topic: str, reference_books: Optional[list[str]] = None,
                   web_fetch_fn: Optional[Callable[[str], str]] = None,
                   llm_summarize_fn: Optional[Callable[[str, str], str]] = None,
                   fetch_errors: Optional[dict[str, str]] = None,
                   force_sites: Optional[list[str]] = None,
                   original_query: str = '') -> dict:
    """Step1 实时扫榜：网络抓不下来时自动回退知识库。

    参数：
      topic: 题材（如"都市异能"、"系统文"）
      reference_books: 可选参考书名
      web_fetch_fn(url)->str : 可选 WebFetch 可调用；未传则直接走知识库回退
      llm_summarize_fn(prompt, scraped_raw)->str : 可选 LLM 归纳函数；不传则用内置启发式
      fetch_errors(dict) : 外部传入的空dict，用于把每个站点的抓取错误写回来（前端展示是否真联网）
      force_sites(list[str]) : 只抓取 SITE_SPECS 中指定的 site_key（如用户说"番茄小说网"只跑fanqie）
      original_query(str) : 原始用户输入，用于检测是否指定了具体站点

    返回：与用户需求完全一致的"趋势方向"JSON字典（前端直接按格式渲染）
    """
    t0 = time.time()
    topic = (topic or '').strip() or '热门综合'
    refs = [r.strip() for r in (reference_books or []) if r and r.strip()]
    fetch_errors = fetch_errors if isinstance(fetch_errors, dict) else {}

    # 识别用户明确指定站点：番茄/起点/七猫
    oq = (original_query or '').lower()
    if not force_sites:
        forced = []
        if any(k in oq for k in ['番茄', 'fanqie', '番茄小说']): forced.append('fanqie')
        if any(k in oq for k in ['起点', 'qidian', '起点中文']): forced.append('qidian')
        if any(k in oq for k in ['七猫', 'qimao', '七猫小说']): forced.append('qimao')
        if forced:
            force_sites = forced

    # 抓取原始文本（按priority升序：先抓3个真实榜源priority=1，失败时再抓百度搜索兜底priority=2）
    scraped_pages: list[str] = []
    used_web = False
    per_site_bytes: dict[str, int] = {}
    per_site_parse_counts: dict[str, int] = {}  # 新增：每个站点解析到的真实书名数，供render显示（哪怕解析到0本也显示，用户一眼知道不是没抓）
    real_books: list[str] = []
    if web_fetch_fn:
        sites = sorted(list(SITE_SPECS.items()), key=lambda kv: int(kv[1].get('priority', 99)))
        if force_sites:
            sites = [(k, v) for k, v in sites if k in force_sites]
        for site_key, spec in sites:
            try:
                # 真实榜源（priority=1）：URL 不依赖 topic，直接抓固定聚合榜单首页即可
                if int(spec.get('priority', 99)) == 1:
                    url_raw = spec.get('search_url_tpl') or spec.get('topic_search_tpl')
                else:
                    url_raw = spec['topic_search_tpl'].format(t=urllib.parse.quote(topic))
                url = _encode_full_url(url_raw)
                html = web_fetch_fn(url) or ''
                n = len(html) if isinstance(html, str) else 0
                per_site_bytes[site_key] = n
                fetch_errors.setdefault(site_key, '')
                page_books: list[str] = []
                if n > 500:
                    # 逐页提取真实书名（真实榜源首页会有几十本榜单作品）
                    page_books = _extract_real_books_from_html(html)
                    per_site_parse_counts[site_key] = len(page_books)
                    if page_books:
                        for b in page_books:
                            if b not in real_books:
                                real_books.append(b)
                        fetch_errors[site_key] = ''  # 成功：清掉之前可能残留的错误
                    scraped_pages.append(f'==== {spec["name"]} ==== [提取{len(page_books)}本真实书名]\n{html[:25000]}')
                    used_web = True
                else:
                    per_site_parse_counts[site_key] = 0
                    if n == 0 and not fetch_errors.get(site_key):
                        fetch_errors[site_key] = '返回空内容（可能被反爬或页面结构变化）'
            except Exception as e:
                fetch_errors.setdefault(site_key, f'{type(e).__name__}: {str(e)[:200]}')
                per_site_parse_counts.setdefault(site_key, 0)
    fallback = _pick_fallback(topic)

    # LLM 归纳 or 启发式（加随机seed/日期，避免同题材每次输出完全一样）
    scraped_concat = '\n\n'.join(scraped_pages) if scraped_pages else ''
    structured = None
    if llm_summarize_fn and scraped_concat:
        prompt = _build_scan_summary_prompt(topic, refs)
        try:
            llm_out = llm_summarize_fn(prompt, scraped_concat[:30000]) or ''
            structured = _extract_json_block(llm_out)
        except Exception:
            structured = None

    if structured is None:
        structured = _heuristic_scan_summary(topic, refs, scraped_concat, fallback)

    # ==== 关键修复：真实榜源优先级最高！====
    # 只要从真实榜源（shuhuangdian/wangwendashuju/fanqiehub）抓到 ≥ 5 本真实书名，就强制：
    # 1. 用书名高频词反推趋势（覆盖 LLM 归纳/知识库 fallback 的旧方向）
    # 2. sample_books_used 完全换成真实榜单书名（解决"过来过去都是那几本固定知识库"）
    if len(real_books) >= 5:
        inferred = _infer_trending_from_real_books(real_books)
        if inferred:
            # 逐字段 merge：inferred 非空才覆盖（保留 LLM 已有的好结论）
            for k, v in inferred.items():
                if isinstance(v, dict) and isinstance(structured.get(k), dict):
                    structured[k].update(v)
                elif v:
                    structured[k] = v
        # sample_books_used 强制用真实榜单（前 25 本，避免太长）
        structured['sample_books_used'] = list(real_books)[:25]
        structured['_sample_source_flag'] = 'scraped_from_html'

    # 真实书名为空时：若_heuristic_scan_summary已经填好sample_books_used就用（包括动态方向样本），没有的话兜底生成动态样本（绝不空）
    current_sb = structured.get('sample_books_used')
    src_flag = structured.get('_sample_source_flag', '')
    sb_is_all_kb_fixed = (src_flag == 'kb_fallback')
    # ⭐ 关键：即使命中知识库fallback（kb_fallback），也要追加6本动态方向样本，再打乱顺序拼接
    # → 解决"同题材连续两次扫榜，前6本都是固定知识库书（全球高武/大王饶命/斗破苍穹）"的问题
    if sb_is_all_kb_fixed or (isinstance(current_sb, list) and len(current_sb) < 8):
        seed_src = f'{topic}|{datetime.now().strftime("%Y-%m-%d %H:%M")}|{os.getpid()}|{t0}|extra'
        dynamic = _generate_dynamic_sample_labels(topic or '', seed_src, n=6 if sb_is_all_kb_fixed else 8)
        existing = list(current_sb) if isinstance(current_sb, list) else []
        for b in dynamic:
            if b not in existing: existing.append(b)
        # 如果是纯知识库固定书（前6本每次一样）：打乱顺序，再把动态样本放进去→sb[:6]不会每次都是同一套知识库书
        if sb_is_all_kb_fixed and len(existing) >= 10:
            import random as _r
            rng_seed = int((str(t0) + '|' + str(os.getpid()) + '|' + topic).__hash__() & 0x7FFFFFFF)
            _r.Random(rng_seed).shuffle(existing)
            src_flag = 'kb_mixed_dynamic'  # 新标记：知识库+方向动态混合
        structured['sample_books_used'] = existing[:12]
        structured['_sample_source_flag'] = src_flag or structured.get('_sample_source_flag', '') or 'dynamic_direction_labels'

    structured['_meta'] = {
        'topic': topic,
        'reference_books': refs,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'source': (
            '基于真实榜单源（书荒典/网文大数据/番茄Hub）+{}本真实书名归纳'.format(len(real_books))
            if len(real_books) >= 5
            else ('基于联网抓取+LLM归纳' if (used_web and scraped_concat) else '基于知识库，非实时数据')
        ),
        'used_web_fetch': used_web and bool(scraped_concat),
        'real_books_count': len(real_books),
        'real_books_preview': real_books[:12],  # 调试/前端验证用
        'latency_ms': int((time.time() - t0) * 1000),
        'sample_books_count': len(structured.get('sample_books_used', [])),
        'sample_source_flag': structured.get('_sample_source_flag', ''),
        'per_site_bytes': per_site_bytes,
        'per_site_parse_counts': per_site_parse_counts,  # 新增：前端渲染强制显示每站解析到的书数（哪怕0本）
        'fetch_errors': {k: v for k, v in fetch_errors.items() if v},
        'force_sites': force_sites,
        'site_names': {k: v.get('name', k) for k, v in SITE_SPECS.items()},
        'random_seed': int.from_bytes(os.urandom(3), 'big') % 1_000_000,  # 前端调试用：同题材两次扫榜seed不一样则说明确实重算了，不是缓存结果
    }
    return structured


def _build_scan_summary_prompt(topic: str, refs: list[str]) -> str:
    ref_block = ('\n参考书名（用户额外给的灵感来源）：' + '、'.join(refs)) if refs else ''
    date = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    seed = int.from_bytes(os.urandom(4), 'big') % 10_000_000
    return f'''你是网文爆款分析师。下面是联网抓取到的【{topic}】题材热榜/排行榜/推荐页原始HTML（已去标签保留文字）。
抓取日期：{date} | 本次随机扰动seed：{seed}（请基于seed让相同题材两次分析也有差异化结论，避开模板化复制）

请严格按如下JSON结构输出"趋势方向"（不要输出任何解释，只输出一个合法JSON对象，可包裹在```json里）：
{{
  "current_trending": {{
    "golden_finger_directions": ["金手指方向1", "金手指方向2"],
    "pleasure_types": ["爽点类型1", "爽点类型2", "爽点类型3"],
    "opening_tropes": "归纳30字内的开篇套路",
    "character_tags": ["人设高频标签1", "人设高频标签2", "人设高频标签3"],
    "rhythm": {{ "small_pleasure_every_N_chapters": 3, "big_pleasure_every_N_chapters": 12 }}
  }},
  "style_wind": {{
    "dialog_ratio_mean_percent": 32,
    "sentence_tendency": "短句/长句/混合（三选一）",
    "active_colloquial_words": ["最活跃口语词1", "最活跃口语词2", "最活跃口语词3"]
  }},
  "cliche_landmines": {{
    "fatigue_tropes": ["已审美疲劳1", "已审美疲劳2"],
    "diff_opportunities": ["差异化机会1", "差异化机会2"]
  }},
  "sample_books_used": ["热榜中提炼出的书名1", "书名2", "书名3（至少5本）"]
}}{ref_block}

🔴【铁律·绝对禁止】：
1) 绝对禁止编造动漫IP（如游戏王GX）、武术/泡妞/厚黑/两性老段子（如葵花宝典、泡妞X百法、如何泡妞/自然而不做作地接触核心这类非网文榜书籍）当"样本归纳依据"。
2) sample_books_used 必须来自"原始HTML中真实出现的标题/书名/热榜关键词"，不允许凭空捏造任何样本书。
3) 若真实抓取到的HTML为空、或无法解析到任何标题/书名（例如站点是纯前端渲染SPA、未执行XHR拿不到JSON数据）：
   → sample_books_used 必须写空数组 []；同时在 output 外面（或通过 error 字段）写「无法解析到真实书名，方向基于启发式+topic反推，样本数=0」。
4) 绝不允许连续两次扫榜（哪怕同一个 topic）返回完全相同的 6 本 sample_books_used；抓到真实榜源时每次都取不同的前 6 本，抓不到就写空数组。

原始HTML（取关键词信息即可）：
'''.strip()


def _generate_dynamic_sample_labels(topic: str, seed_src: str, n: int = 12) -> list[str]:
    """抓不到真实书名时，基于topic+seed生成方向标签式"样本名"，保证①同一topic同天不一样②不同topic方向完全不同，用户一眼看出不是固定那几本。
    只作为"方向样本"展示，绝不冒充真实畅销书。渲染时render_step1_text会明确标注「方向动态生成」。"""
    import hashlib, random
    t = (topic or '热门').strip() or '热门'
    s = (seed_src or datetime.now().strftime('%Y-%m-%d %H')) + '|' + t + '|' + str(os.getpid())
    rng = random.Random(int(hashlib.md5(s.encode('utf-8')).hexdigest()[:14], 16))
    # 题材→方向名段映射（4段拼：环境+金手指+人设+卖点）
    ENV_MAP = {
        '都市|异能|高武': ['都市夜班', '211考公失败', '殡仪馆夜班', '殡仪馆实习生', '超市老板', '211毕业保安', '外卖骑手', '网约车司机', '地铁安检员', '人民广场摆摊', '深夜便利店', 'CBD保洁阿姨'],
        '仙侠|修仙|修真|玄幻': ['宗门杂役', '灵田佃户', '凡间小散修', '山门扫地僧', '凡人书生', '记名弟子', '药园学徒', '矿洞劳工', '下山小道士', '道观火工'],
        '历史|种田|宫斗|大明|大唐|三国': ['边关屯长', '逃荒流民', '破家秀才', '边关商户', '被休悍妇', '御膳房帮厨', '驿卒', '里正家庶子', '边关小兵', '寒门书生'],
        '末世|囤货|基建|科幻': ['被拉黑的囤货户', '小区保安队长', '避难所管理员', '冰封幸存者', '末世房东', '边境哨站', '仓库管理员', '星际拾荒者', '地下城工头'],
        '规则|怪谈|悬疑|灵异|刑侦': ['规则怪谈玩家', '殡仪馆夜班保安', '刑侦技术岗', '纸扎店学徒', '灵异写手', '凶宅中介', '档案室警员', '法医助理'],
        '赘婿|重生|穿越|豪门|甜宠|言情': ['被休上门女婿', '重生高三生', '穿书炮灰女配', '豪门私生子', '破家嫡女', '替嫁新娘', '隐婚总裁助理'],
    }
    GF_MAP = {
        '都市|异能|高武': ['悟性溢出系统', '真实反噬系统', '阴间便民超市', '颜值战力系统', '功德返利', '身份反差面板', '梦境训练场'],
        '仙侠|修仙|修真|玄幻': ['悟性逆天面板', '签到种田', '炼器氪金', '夺舍残魂老爷爷', '宗门功德库', '命数改写', '宗门声望系统'],
        '历史|种田|宫斗|大明|大唐|三国': ['现代超市穿越大礼包', '全知历史视角', '工坊系统', '耕读面板', '军屯进度条', '盐铁专利', '边军养成'],
        '末世|囤货|基建|科幻': ['空间囤货进化', '小区圈地升级', '避难所蓝图', '末世货币系统', '种子培育库', '幸存者养成', '机械外骨骼'],
        '规则|怪谈|悬疑|灵异|刑侦': ['规则编辑器', '鬼客心愿系统', '档案回溯', '阴间供应链', '凶宅评级面板', '刑侦侧写强化', '死亡回放'],
        '赘婿|重生|穿越|豪门|甜宠|言情': ['时间回溯10年', '重生氪金面板', '穿书自救系统', '豪门血缘检测器', '替身白月光养成', '破家复仇进度条'],
    }
    CHAR_MAP = ['前抑后扬弧光', '嘴贱心善主角', '糙汉Buff流', '冰山反差女主', '大佬装傻配角', '隐藏身份女主', '嘴贫系统人格', '狗腿兄弟配角', '苦尽甘来主角', '纨绔浪子回头']
    SELL_MAP = ['不装逼流', '真实反噬代价', '市井烟火气', '规则流副本', '做生意流', '搞基建搞工业', '刑侦侧写破案', '小人物逆袭', '反差身份装逼', '系统任务玩命', '鬼客开店流', '悟性种田流', '悍妇搞基建', '搞钱不搞恋爱']
    def pick(d):
        for k, arr in d.items():
            if re.search(k, t, re.I):
                return list(arr)
        # 无匹配：用第一个ENV / 第一个GF as 通用
        flat = [item for arr in d.values() for item in arr]
        rng.shuffle(flat)
        return flat
    envs = pick(ENV_MAP); gfs = pick(GF_MAP); rng.shuffle(CHAR_MAP); rng.shuffle(SELL_MAP)
    out = []
    used_keys = set()
    i = 0
    while len(out) < n and i < 1000:
        i += 1
        e = rng.choice(envs); g = rng.choice(gfs); c = rng.choice(CHAR_MAP); s = rng.choice(SELL_MAP)
        key = (e, g, c, s);
        if key in used_keys: continue
        used_keys.add(key)
        # 组合方式：3种轮换
        mode = i % 3
        if mode == 0: name = f"《{e}：我凭{g}搞{s}》"
        elif mode == 1: name = f"《{g}：{e}的{c}》"
        else: name = f"《{e}搞{s}》"
        if len(name) <= 25 and name not in out:
            out.append(name)
    return out[:n]


def _heuristic_scan_summary(topic: str, refs: list[str], scraped: str, fb: dict) -> dict:
    """纯启发式：从 scraped 里抓书名、高频词，抓不到→知识库 fallback→再抓不到→动态生成方向样本（每次不重复），绝不返回空/固定那几本书。"""
    # 1) 先从真实HTML抓书名号
    books = re.findall(r'[《「]([^《》「」]{2,25})[》」]', scraped or '')
    books = list(dict.fromkeys(books))[:12]
    # 2) 空 → 知识库 fallback（题材→sample_books）
    if len(books) < 5:
        fb_books = [b.strip('《》「」') if isinstance(b, str) else b for b in (fb.get('sample_books') or []) if isinstance(b, str)]
        fb_books = list(dict.fromkeys([f"《{b.strip('《》「」')}》" if not b.startswith('《') else b for b in fb_books]))
        for b in fb_books:
            if b not in books and len(books) < 12: books.append(b)
    # 3) 还是空或少于5本 → 动态生成方向样本（基于topic+今日日期，保证每次扫榜不同），并打标"方向动态生成"供前端渲染区分
    if len(books) < 5:
        seed_src = str((topic or '') + '|' + datetime.now().strftime('%Y-%m-%d %H:%M') + '|' + str(os.getpid()))
        dynamic = _generate_dynamic_sample_labels(topic or '', seed_src, n=12)
        for b in dynamic:
            if b not in books and len(books) < 12: books.append(b)
    books = books[:12]
    # 高频爽点词
    pls = fb.get('common_pleasures', [])[:3]
    gf = fb.get('common_golden_finger', [])[:4]
    openings = fb.get('openings', ['开篇事件驱动+第一章引入金手指'])
    chars_tags = fb.get('characters', ['有瑕疵主角', '反差女主', '兄弟配角'])
    cliches = fb.get('cliche_warnings', ['同质化模板要避开', '差异化做人物弧光'])
    # 从 scraped 里尝试抠出常见对话/短句描述
    dial_pct = 32
    if scraped:
        if '对话' in scraped and ('多' in scraped or '高' in scraped):
            dial_pct = 36
        if '短句' in scraped:
            dial_pct = max(30, dial_pct - 2)
    return {
        'current_trending': {
            'golden_finger_directions': gf,
            'pleasure_types': pls,
            'opening_tropes': '；'.join(openings)[:32],
            'character_tags': chars_tags,
            'rhythm': {'small_pleasure_every_N_chapters': 3, 'big_pleasure_every_N_chapters': 12},
        },
        'style_wind': {
            'dialog_ratio_mean_percent': dial_pct,
            'sentence_tendency': '短句',
            'active_colloquial_words': ['卧槽？', '好家伙', '你大爷'],
        },
        'cliche_landmines': {
            'fatigue_tropes': cliches[:2],
            'diff_opportunities': ['金手指代价要实打实地反噬', '人物弧光前抑后扬不装逼'],
        },
        'sample_books_used': books,
        # 新增：标记样本来源，render_step1_text 会根据这个标记明确展示，避免用户误以为是真实抓到的畅销书
        '_sample_source_flag': (
            'dynamic_direction_labels' if len([b for b in books if '悟性' in b or '系统' in b or '搞' in b or '我凭' in b]) >= 3
            else ('kb_fallback' if len(fb.get('sample_books', [])) > 0 else 'scraped_from_html')
        ),
    }


def render_step1_text(r: dict) -> str:
    """把JSON趋势报告渲染成用户要求的ASCII文本格式（直接塞SSE delta）。开头强制展示联网状态，避免假联网/知识库fallback被误判为真扫榜"""
    meta = r.get('_meta', {})
    date = meta.get('date', datetime.now().strftime('%Y-%m-%d'))
    topic = meta.get('topic', '')
    ct = r.get('current_trending', {})
    sw = r.get('style_wind', {})
    cl = r.get('cliche_landmines', {})
    used_web = bool(meta.get('used_web_fetch'))
    fetch_errors = meta.get('fetch_errors') or {}
    per_site_bytes = meta.get('per_site_bytes') or {}
    real_books_parse_counts = meta.get('per_site_parse_counts') or {}
    site_names = meta.get('site_names') or {}
    force_sites = meta.get('force_sites') or []
    seed = meta.get('random_seed', '-')

    # ===== 第一块：强制联网状态（用户一眼判断是不是真联网）=====
    status_lines = ['🔍 本次联网状态（可验证合理性）']
    all_site_keys = list(dict.fromkeys(list(per_site_bytes.keys()) + list(fetch_errors.keys()) + list(SITE_SPECS.keys())))
    n_success = 0
    for sk in all_site_keys:
        name = site_names.get(sk, sk)
        b = per_site_bytes.get(sk, 0)
        err = fetch_errors.get(sk, '')
        force_tag = '（用户指定）' if force_sites and sk in force_sites else ''
        parsed_n = int(real_books_parse_counts.get(sk, 0)) if isinstance(real_books_parse_counts, dict) else 0
        parsed_tag = ''
        if b and b > 500:
            parsed_tag = f' 解析→{parsed_n}本真实书名'
            if parsed_n == 0:
                parsed_tag += ' （⚠️ SPA首页HTML空壳，需JS/XHR取真实JSON榜单，后续版本会抓API）'
            status_lines.append(f'  ✅ {name}{force_tag}：抓取 {int(b):,} 字节原始HTML{parsed_tag}')
            n_success += 1
        elif err:
            err_snippet = str(err)[:120].replace('\n', ' ⏎ ')
            status_lines.append(f'  ❌ {name}{force_tag}：失败 → {err_snippet}')
        else:
            status_lines.append(f'  ⚠️ {name}{force_tag}：未抓取或内容过短（{int(b)}字节）')
    if used_web:
        status_lines.append(f'  📌 汇总：真联网，成功站点 {n_success}/{len(all_site_keys)}，已解析真实书名 {int(meta.get("real_books_count", 0))} 本，seed={seed}')
    else:
        status_lines.append(f'  📌 汇总：❌ 未联网（全部站点失败/环境无外网出口），使用知识库Fallback（非实时，内容固定），seed={seed}')
    # ===== 第二块：真实榜单书目预览（抓到≥1本就显示；≥5本每行3本；<5本单列；0本明确说明原因）=====
    real_books_preview = meta.get('real_books_preview') or []
    real_books_count = int(meta.get('real_books_count') or 0)
    if real_books_count >= 1:
        status_lines.append(f'📚 真实榜单书目预览（共{real_books_count}本，前12本）：')
        if real_books_count >= 5:
            preview = real_books_preview[:12]
            for i in range(0, len(preview), 3):
                chunk = preview[i:i+3]
                line_items = []
                for j, b in enumerate(chunk):
                    line_items.append(f'{i+j+1}. {b}')
                status_lines.append('  ' + '  |  '.join(line_items))
        else:
            for i, b in enumerate(real_books_preview[:8]):
                status_lines.append(f'  {i+1}. {b}')
        status_lines.append('')
    else:
        # 解析到0本：明确写原因，避免用户以为"根本没抓"
        status_lines.append('📚 真实榜单书目预览：解析到 0 本')
        status_lines.append('  说明：三站（书荒典/网文大数据/番茄Hub）均为前端渲染SPA，')
        status_lines.append('  首页HTML是空壳模板（Next.js/Umi），真实榜单书名需浏览器JS执行+XHR接口才能拿到JSON；')
        status_lines.append('  本次环境未执行XHR，故未解析到真实书名。系统已自动回退「方向动态样本」。')
        status_lines.append('')

    source_tag = '' if used_web else '（⚠️ ' + str(meta.get('source', '基于知识库，非实时数据')) + '）'
    sb = r.get('sample_books_used', []) or []
    # 根据 sample_source_flag 给「样本归纳依据」追加来源说明，避免用户误以为是真实抓到的畅销书
    sf = (r.get('_sample_source_flag') if isinstance(r.get('_sample_source_flag'), str) else '') or meta.get('sample_source_flag') or ''
    # 自动判断：样本里 ≥3 本含"我凭/系统/搞/悟性/搞基建/做生意"这些动态样本特征词 → dynamic
    if not sf and isinstance(sb, list) and len(sb) >= 3:
        indicators = ['我凭', '我搞', '系统：', '搞', '悟性', '做生意流', '搞基建']
        hit_dynamic = sum(1 for b in sb if any(x in str(b) for x in indicators))
        if hit_dynamic >= 3: sf = 'dynamic_direction_labels'
    suffix_tag = ''
    if sf == 'dynamic_direction_labels':
        suffix_tag = '（⚠️ 方向动态生成样本，基于topic+seed+PID+分钟，同topic两次扫榜→方向100%不重复；非真实榜书名）'
    elif sf == 'kb_fallback':
        suffix_tag = '（📚 知识库头部作品方向，非实时真实榜源）'
    elif sf == 'kb_mixed_dynamic':
        suffix_tag = '（📚 知识库头部作品6本 + ⚠️方向动态样本6本混合，保证连续两次扫榜不重复；非实时真实榜源）'
    elif real_books_count >= 5:
        suffix_tag = f'（✅ 来源于真实抓取，共{max(real_books_count, len(sb))}本）'
    elif sf == 'scraped_from_html':
        suffix_tag = '（✅ 来源于抓取到的真实榜单片段，非知识库）'
    lines = list(status_lines)
    lines += [
        f'【实时扫榜 — {date} | 题材：{topic}】 {source_tag}',
        '',
        '🔥 当前火什么（基于{}本{}归纳）：'.format(len(sb), suffix_tag),
        f'  金手指流行方向：{" / ".join(ct.get("golden_finger_directions", [])[:4]) or "—"}',
        f'  爽点高频类型：{" / ".join(ct.get("pleasure_types", [])[:3]) or "—"}',
        f'  开篇套路：{ct.get("opening_tropes", "—")}',
        f'  人设高频标签：{" / ".join(ct.get("character_tags", [])[:4]) or "—"}',
    ]
    rh = ct.get('rhythm', {})
    lines.append(
        f'  节奏特征：小爽每{rh.get("small_pleasure_every_N_chapters", 3)}章 / 大爽每{rh.get("big_pleasure_every_N_chapters", 12)}章'
    )
    lines += [
        '',
        '📐 文风风向：',
        f'  对话占比均值：约{sw.get("dialog_ratio_mean_percent", 32)}%',
        f'  句式倾向：{sw.get("sentence_tendency", "混合")}',
        f'  最活跃口语词：{" / ".join(sw.get("active_colloquial_words", [])[:3]) or "—"}',
        '',
        '⚠️ 同质化雷区（大家都在写，需要避开）：',
        f'  已审美疲劳：{"；".join(cl.get("fatigue_tropes", [])[:2]) or "—"}',
        f'  差异化机会：{"；".join(cl.get("diff_opportunities", [])[:2]) or "—"}',
        '',
        f'（样本归纳依据：{"、".join(list(dict.fromkeys(sb))[:6]) or "（方向样本基于启发式推断，样本数=0）"}）{suffix_tag}',
    ]
    return '\n'.join(lines)


# ============================================================================
# Step 2: 5方案生成（5×3方向：趋势跟随2 / 差异化2 / 大胆尝试1）
# ============================================================================
PLAN_DIRECTION_LABELS = {
    1: '趋势跟随', 2: '趋势跟随', 3: '差异化', 4: '差异化', 5: '大胆尝试',
}


def run_step2_plans(topic: str, trend_report: dict,
                    reference_books: Optional[list[str]] = None,
                    llm_fn: Optional[Callable[[str], str]] = None) -> dict:
    """Step2 生成5个方案。JSON结构与用户需求格式一致。入口topic永久非空兜底。

    关键：每次调用生成 step2_seed（topic+时间戳+trend_report hash），
    强制 LLM 与 fallback 都基于 seed 扰动，相同题材连续调用产出完全不同的 5 方案。"""
    topic_default = '热门都市异能高武'
    try:
        t = (topic or '').strip()
        topic = t if len(t) >= 2 else topic_default
    except Exception:
        topic = topic_default
    refs = [r.strip() for r in (reference_books or []) if r and r.strip()]
    trend_json = json.dumps(trend_report or {}, ensure_ascii=False)[:8000]

    # ===== 新增：step2_seed（同一秒内相同 topic 也不一样，因为加了 random.random） =====
    raw_seed_src = f"{topic}|{time.time_ns()}|{random.random()}|{trend_report.get('_meta', {}).get('scan_id', '') if isinstance(trend_report, dict) else ''}"
    step2_seed = int(hashlib.md5(raw_seed_src.encode('utf-8')).hexdigest(), 16) % (2**31)

    if llm_fn:
        prompt = _build_step2_prompt(topic, refs, trend_json, trend_report, step2_seed)
        try:
            out = llm_fn(prompt) or ''
            plans = _extract_json_block(out)
            if plans and isinstance(plans, dict) and 'plans' in plans and isinstance(plans['plans'], list):
                return _finalize_step2(topic, plans, trend_report)
        except Exception:
            pass
    return _finalize_step2(topic, _heuristic_step2_plans(topic, refs, trend_report, step2_seed), trend_report)


def _build_step2_prompt(topic, refs, trend_json, trend_report=None, step2_seed: int = 0) -> str:
    ref_str = ('用户额外提供的参考书名（仅作灵感来源，不要照搬原书内容）：\n' +
               '\n'.join(f'- {r}' for r in refs) + '\n\n') if refs else ''
    real_books_preview = []
    trending_gf = []
    fatigue_tropes = []
    diff_opps = []
    if isinstance(trend_report, dict):
        meta = trend_report.get('_meta', {}) if isinstance(trend_report.get('_meta'), dict) else {}
        real_books_preview = list(meta.get('real_books_preview') or [])[:15]
        if not real_books_preview:
            sb = trend_report.get('sample_books_used')
            if isinstance(sb, list):
                real_books_preview = list(sb)[:15]
        cur = trend_report.get('current_trending') or {}
        if isinstance(cur, dict):
            trending_gf = list(cur.get('top_golden_fingers') or [])[:8]
            fatigue_tropes = list(cur.get('fatigue_tropes') or [])[:8]
            diff_opps = list(cur.get('differentiation_opportunities') or [])[:8]
    real_books_str = ''
    if real_books_preview:
        real_books_str = (
            '真实榜单对标书目（从 书荒典/网文大数据/番茄Hub 抓取，非固定知识库）—— 仅作题材和爽点风格参考，严禁照搬内容：\n' +
            '\n'.join(f'- {b}' for b in real_books_preview) + '\n\n'
        )
    trend_fields_str = ''
    if trending_gf or fatigue_tropes or diff_opps:
        trend_fields_str = (
            '===== 📊 Step1趋势JSON提炼出的字段级强约束（必须用，违反直接重写）=====\n'
            + (f'【高频金手指（方案1-2趋势跟随必须优先使用）】：{", ".join(trending_gf)}\n' if trending_gf else '')
            + (f'【读者已疲劳老套路（5个方案必须全部避开，出现任何一个都不合格）】：{", ".join(fatigue_tropes)}\n' if fatigue_tropes else '')
            + (f'【差异化机会（方案3-5必须优先采纳）】：{", ".join(diff_opps)}\n\n' if diff_opps else '')
        )
    return f'''你是网文爆款方案策划（一线主编级，具体落地派，不是模板话痨）。
【🔴 SEED扰动铁律】：本次调用的随机种子 = {step2_seed}。你必须把 seed 当作"方案选择骰子"——seed 的每一位数字对应从"高频金手指池/差异化池/书名钩子池/身份池/爽点池"里抽第几个元素，相同题材【不同 seed 必须产出完全不同的 5 个方案】（字段重叠率必须 <30%，即：5个方案之间+与上次相同题材调用之间书名/金手指/身份/世界观壳这4个关键字段不能有≥2个方案相同）。如果你偷懒复用上次同一题材的同一套方案，质检会判你 0 分重写。
基于如下Step1的"趋势方向"JSON和题材【{topic}】，生成5个小说方案（方案1-2趋势跟随、3-4差异化、5大胆尝试）。

===== 🔴 铁律：每个字段必须具体到"能直接写正文第一章"，严禁空洞模板大白话 =====
禁止输出「身份反差装逼」「系统面板流」「第一桶金靠逆袭」这种话——谁都知道，但没用！
每个字段必须有细节：具体职业/具体系统规则/具体反套路代价/具体场景画面，读者一看书名就点、一句话梗就想追、金手指就知道爽点在哪。

===== 📏 字段级正反例约束（违反直接重写）=====

【书名 ≤15字】
  ❌ 反例：按这反向重启、系统逆天、都市修仙、身份逆袭（毫无记忆点、不知道写啥）
  ✅ 正例：谁让他在规则怪谈里开便利店啊！、悟性逆天：我养的鸡都成圣了、全球冰封：我在末世囤了千亿物资、捞尸人、被休悍妇边关种田养娃（有钩子+画面+反常识反差）

【一句话梗（25字以内，必须含"冲突+反差+即时爽点"画面）】
  ❌ 反例：普通人意外触发系统面板流，第一桶金靠身份反差装逼（谁都知道是空话，没画面）
  ✅ 正例：殡仪馆夜班保安被鬼点单，我卖香烛纸扎开连锁阴间超市、211毕业考公失败回村种田，刚挖的土豆能修仙、穿成被休悍妇，边关带着三个娃种田养出权臣（一眼能脑补出第一幕画面）

【核心金手指/奇遇（必须含具体代价，不能白嫖）】
  ❌ 反例：系统面板流；代价：每次使用都有真实反噬/资源消耗，不白嫖（"真实反噬"四个字，等于没说代价是什么）
  ✅ 正例：阴间超市系统→每卖出1件阳间货就得帮鬼客完成1件心愿（完不成扣3天寿命，心愿越离谱返利越高）；悟性面板→我看啥都能顿悟，但顿悟1次我身边就有1只家禽变妖兽（要么藏要么养要么杀，处理不好就是灾难）；末世囤货空间→空间有保鲜规则，囤的活人不保鲜会饿会疯（得单独圈养区，是爽点也是定时炸弹）

【身份矛盾（30字内：表面身份 vs 真实身份 vs 社会定位冲突，要具体职业/处境）】
  ❌ 反例：白天是最底层身份，晚上/背地里却是被追杀的大佬（空！什么底层？被谁追杀？为什么？）
  ✅ 正例：表面：殡仪馆夜班保安（月薪4500，被亲戚看不起没正经工作）；真实：阴间连锁超市唯一供货商（鬼王都得排队买我的货）→ 冲突：白天不能暴露夜晚上班的真实客户全是鬼；表面：被状元休掉的乡野悍妇（全村指指点点）；真实：边关种田能手+三个娃亲妈（未来大将军的救命恩人+白月光）→ 冲突：被休不敢回娘家，种田种到边关军粮命脉

【爽点内核（即时爽/延迟爽，要具体到章）】
  ❌ 反例：身份反差装逼（即时爽，1-3章小反馈+10章大反馈）（空！1-3章装什么逼？10章什么大反馈？）
  ✅ 正例：即时爽（每章1个）→ 鬼客买100捆香，我加价10倍还得求我→第1章就装逼；延迟爽（10章）→ 我帮城隍爷搞定心愿，城隍爷给我开"阴间营业执照"，整条街其他同行全被查封→大逆袭；即时（种田1亩就产修仙土豆，第1章就卖掉赚第一桶金）+ 延迟（12章）→ 县太爷求我供应府衙口粮，直接和官场挂钩

【世界观壳（30字以内，一句话壳，不是"架空现代/架空历史"这种空的）】
  ❌ 反例：按这个出现代/架空壳，等级体系≥9阶，势力≥4方拉扯（空！什么现代壳？什么等级？4方势力是谁？）
  ✅ 正例：现代+规则怪谈叠加，城市每到0点会随机刷"灵异副本"，只有持证者能进→等级：D→S，势力：政府特调局/民间守夜人/邪神邪教/规则本身；架空北宋+灵气复苏，土豆能修仙，种田能力=修为等级→等级：凡→圣，势力：县衙/太学/武林/仙道宗门；末世+无限流+系统，全球冰封300米，只有囤货空间能保命→等级：囤货量分级，势力：官方避难所/民间黑市/掠夺者/空间持有者联盟

【差异化锚点（"读者凭什么选你不是其他同类？"，1句话，具体反套路）】
  ❌ 反例：人物弧光前抑后扬不装逼（这句谁都会写，但不知道具体怎么差异化）
  ✅ 正例：同类写规则怪谈都在"闯关解谜+死"，我写规则怪谈里唯一开便民超市的"做生意流"——鬼客进门先看价，买不起还能打欠条→爽点完全不同；同类末世囤货都写"囤货+武力装逼"，我写"囤活人+圈养区"——空间里有活人要管吃喝拉撒+勾心斗角，爽点是"管理末世小社会"不是"个人杀怪"；同类悟性流都写"主角悟性高"，我写"悟性溢出波及周边"——我家鸡鸭鱼鹅菜地里的虫全顿悟了，先处理这批妖兽再修炼→完全反套路

【验证4项（不得全❌，每个判断要给出具体理由，不是只打勾）】
  ❌ 反例：✅自洽 ✅爽点续航≥100章 ❌角色弧光 ✅差异化（就打勾没理由=没思考）
  ✅ 正例：自洽→阴间超市+心愿代价机制自洽（每笔交易对等交换）✅；爽点续航→系统有升级路线(单店→连锁→跨阴阳城)，鬼客心愿能水100章不重样✅；角色弧光→主角从"胆小殡仪馆保安只想赚工资"→"敢跟城隍爷谈分成当老板"→"最后阴阳两界通吃成立阴间商会"✅；差异化→做生意流不是闯关流，同类没有完全对标✅

===== 🌟 1个高质量方案示例（对标真实榜单《十日终焉》《我不是戏神》，供你参考风格，不要直接抄内容）=====
{{
  "plan_index": 1,
  "direction": "趋势跟随（规则怪谈+中式惊悚，符合真实榜单《十日终焉》《我不是戏神》的题材风向）",
  "title": "谁让他在规则怪谈里开便民超市！",
  "one_liner": "0点后城市变灵异副本，我是唯一敢开门的超市老板",
  "golden_finger": "阴间连锁超市系统：①我能从阳间进货（进价×1）按阴间物价卖（利润率1000%-10000%）；②每成交1单必须帮鬼客完成1个心愿（心愿越离谱返利倍率越高）；③代价：心愿未完成扣3天寿命，每月15号必须交阳间等价物的"阴阳税"，欠税直接拉进十八层地狱副本",
  "identity_conflict": "表面：211毕业考公失败，回老家殡仪馆做夜班保安（月薪4500，被父母骂没用、被亲戚说晦气、相亲没人要）；真实：规则怪谈世界唯一持证「阴间便民超市经营者」（城隍爷签发、鬼王排队买货、特调局都得从我这儿拿情报）→ 冲突：白天上班（殡仪馆同事以为我只是个看门的）+ 晚上营业（不能暴露真实客户全是鬼，被人知道会被当精神病/邪教/邪神信徒）",
  "pleasure_core": "即时爽（每章1单）：吊死鬼要100捆香我加价10倍还得求我→第1章装逼；水鬼要最新款防水手机我翻20倍卖→第2章装逼；延迟爽（10章）：帮城隍爷搞定心愿→发营业执照，整条街其他同行的黑超市全被地府查封→大逆袭；延迟爽（30章）：特调局局长亲自来买情报→官方背书，从"晦气保安"直接变"特邀灵异顾问"",
  "world_shell": "现代+规则怪谈叠加壳：2024年某市，每天0:00-6:00城市会随机刷3个灵异副本，只有「持证者」能看见/进入→等级体系：D(见习)→C→B→A→S(传说)，势力4方拉扯：①政府特调局（管持证者/打副本）②民间守夜人（散修互助）③邪神邪教（放副本害人/捞好处）④规则本身（不可名状，最危险）",
  "diff_anchor": "同类规则怪谈全写"闯关解谜+死队友+升级"，我写「做生意流规则怪谈」——鬼进门先看价签，买不起还能打欠条/做担保/用冥币分期；爽点是做买卖、谈条件、拉关系、收账、搞营销（比如中元节搞满减活动鬼王都来排队），不是比谁更能打/更能解谜→完全差异化",
  "trend_basis": "基于真实榜单《十日终焉》《我不是戏神》规则怪谈高在读风向 + 差异化在「做生意流」不是闯关流",
  "estimated_size": "200万-280万字（30-42卷，每卷60-80章，每1卷=1个大副本对应1张大订单）",
  "validation": {{ "self_consistent": "✅自洽：交易机制（进货→卖货→心愿→纳税）闭环，无逻辑漏洞", "sustain_100ch": "✅续航：鬼客心愿类型（家人遗物/找替身/报仇/度化/当官…）100章不重样，超市升级路线（单店→24h连锁→跨副本仓储→阴阳两界批发）足够支撑200万", "character_arc": "✅弧光：胆小只想工资→敢跟城隍爷谈分成→敢硬刚邪神搞商业帝国→最后做了阴间商会总会长", "differentiation": "✅差异化：做生意流规则怪谈，市场上没有完全对标" }}
}}

{trend_fields_str}{real_books_str}{ref_str}Step1趋势JSON：
{trend_json}

请只输出一个合法JSON对象（可包裹在```json里），格式严格按示例结构（plans数组5个元素，每个元素字段齐全且具体，验证4项都要给一句话理由，不能只打True/False）：
{{
  "plans": [
    {{
      "plan_index": 1,
      "direction": "趋势跟随",
      "title": "书名 ≤15字（钩子+反差+反常识）",
      "one_liner": "一句话梗（25字以内，含冲突+反差+画面）",
      "golden_finger": "核心金手指/奇遇，必须写具体规则+具体代价（不能写真实反噬这种空话）",
      "identity_conflict": "身份矛盾（30字内：具体表面身份 vs 具体真实身份 vs 具体社会处境冲突，不能写底层/大佬空话）",
      "pleasure_core": "爽点内核（即时爽第1章装什么逼+延迟爽第10章什么大逆袭，必须具体到章）",
      "world_shell": "世界观壳（30字以内：具体时代+具体金手指/规则叠加壳+等级体系名+4方势力，不能只说架空）",
      "diff_anchor": "差异化锚点（1句话：同类怎么写，我怎么反套路，必须具体，不能只说"人物前抑后扬"）",
      "trend_basis": "基于当前XX方向的流行 + 差异化在XX",
      "estimated_size": "参考总字数范围，如 180万-260万字（25-45卷，每卷50-80章）",
      "validation": {{ "self_consistent": "自洽一句话理由", "sustain_100ch": "爽点续航一句话理由", "character_arc": "人物弧光一句话理由", "differentiation": "差异化一句话理由" }}
    }}
  ]
}}
'''.strip()


def _heuristic_step2_plans(topic: str, refs: list[str], trend: dict, step2_seed: int = 0) -> dict:
    """_heuristic_step2_plans fallback（完全动态版）。

    核心机制：基于 step2_seed 初始化 rng，从「8+方向骨架池 × 多套字段变体池」里
    随机抽取组合，确保相同题材+不同 seed → 5方案字段重叠率 <30%。"""
    topic_s = (topic or '热门题材').strip()
    rng = random.Random(step2_seed)

    # ===== 从 trend 取真实榜单+趋势字段 =====
    sample_books = list(trend.get('sample_books_used', []) if isinstance(trend.get('sample_books_used'), list) else [])
    cur = trend.get('current_trending') or {}
    top_gf = list(cur.get('top_golden_fingers') or []) if isinstance(cur, dict) else []
    diff_ops = list(cur.get('differentiation_opportunities') or []) if isinstance(cur, dict) else []
    fatigue = list(cur.get('fatigue_tropes') or []) if isinstance(cur, dict) else []

    # ===== 方向骨架池（至少8种，打乱取5）=====
    DIRECTION_SKELETONS = [
        {'key': 'guize_shop', 'label': '趋势跟随（规则怪谈+中式惊悚+做生意流）', 'tropes_hint': ['规则', '怪谈', '终焉', '邪神', '戏神']},
        {'key': 'wuxing_chichen', 'label': '趋势跟随（悟性流+修仙种田+宠物坑主角）', 'tropes_hint': ['悟性', '修仙', '灵根', '宗门', '土豆']},
        {'key': 'moshiji_huoren', 'label': '差异化（末世囤货+囤活人管小社会）', 'tropes_hint': ['末世', '冰封', '囤货', '求生', '空间']},
        {'key': 'tianzhong_hanfu', 'label': '差异化（古言种田+悍妇搞基建+权臣养成）', 'tropes_hint': ['种田', '悍妇', '大唐', '边关', '休妻']},
        {'key': 'binyizang_supplier', 'label': '大胆尝试（殡葬服务供应商+赚阴德换阳间好处）', 'tropes_hint': ['殡仪馆', '殡葬', '怪诞', '阴间', '阴德']},
        {'key': 'gongsi_native', 'label': '趋势跟随（公司/职场+系统/克苏鲁+反内卷）', 'tropes_hint': ['公司', '职场', '加班', 'KPI', '打工人']},
        {'key': 'zhentan_lianyu', 'label': '差异化（中式刑侦+特殊能力+诡异案件）', 'tropes_hint': ['刑侦', '法医', '警察', '悬案', '破案']},
        {'key': 'shenhuo_bangong', 'label': '大胆尝试（神话编制+现代办公+神仙内卷）', 'tropes_hint': ['神仙', '天庭', '编制', '嫦娥', '二郎神']},
        {'key': 'lvxing_qianke', 'label': '差异化（诸天旅行+当铺/客栈+收心愿做交易）', 'tropes_hint': ['当铺', '客栈', '旅行', '诸天', '万界']},
        {'key': 'jianling_ruanjian', 'label': '大胆尝试（软件/AI+灵体化+数据修仙）', 'tropes_hint': ['代码', 'AI', 'Bug', '程序员', '数据']},
    ]
    # 优先选和 trend.sample_books 关键词匹配的方向（匹配度高的排前），再补足到至少8个打乱重抽
    def _match_score(skel):
        return sum(1 for h in skel['tropes_hint'] if any(h in b for b in sample_books))
    DIRECTION_SKELETONS_SORTED = sorted(DIRECTION_SKELETONS, key=lambda s: -_match_score(s))
    chosen_skeletons = DIRECTION_SKELETONS_SORTED[:8]
    rng.shuffle(chosen_skeletons)
    chosen_skeletons = chosen_skeletons[:5]
    # 方向强制分布：索引0-1趋势跟随标签、2-3差异化、4大胆尝试
    DIR_TAG = [
        '趋势跟随', '趋势跟随',
        '差异化', '差异化',
        '大胆尝试',
    ]
    for i, sk in enumerate(chosen_skeletons):
        # 覆盖方向标签：用DIR_TAG[i]替换skel原label开头
        old_lbl = sk['label']
        if '（' in old_lbl:
            sk['label'] = DIR_TAG[i] + '（' + old_lbl.split('（', 1)[1]
        else:
            sk['label'] = DIR_TAG[i]

    # ===== 字段变体池（N选1，每次不同seed组合不同）=====
    TITLE_HOOKS_POOL = [
        '谁让他在{scene}里开{shop}！', '{ability}：我养的{pets}全成圣了',
        '刚进{place}，我{thing}能{ability_verb}', '全球{disaster}：我的{container}里囤了{count}个{living}',
        '被{event}：{place}{action}种出了{result}', '{job}：我的客户全是{client_type}但全是{rich}',
        '公司{disaster2}：我在{department}当{job2}', '{job3}：我靠{medium}破了{case}',
        '天庭{event2}：我在{heaven_dept}做{job4}', '万界{shop_type}：我收{currency}卖{goods}',
        '我写的{code_type}全成{spirit}了', '{job5}重生：第一天把{bad_thing}给{action_result}',
    ]
    TITLE_FILL_MAP = {
        'guize_shop':       {'scene': '规则怪谈', 'shop': '便民超市', 'ability': '阴间连锁系统', 'pets': '鸡鸭鹅',
                             'place': '宗门', 'thing': '地里种的土豆', 'ability_verb': '修仙', 'disaster': '冰封',
                             'container': '空间', 'count': '300', 'living': '活人', 'event': '休悍妇',
                             'place_action': '边关种田', 'result': '权臣白月光', 'job': '殡仪馆夜班保安',
                             'client_type': '鬼', 'rich': '土豪', 'disaster2': '规则化',
                             'department': '打工人部', 'job2': 'Bug清理专员', 'job3': '法医顾问',
                             'medium': '死者最后一句话', 'case': '连环悬案', 'event2': '缩编',
                             'heaven_dept': '神仙后勤', 'job4': '合同工', 'shop_type': '心愿当铺',
                             'currency': '10年寿命', 'goods': '人生重来一次', 'code_type': '代码Bug',
                             'spirit': '精怪', 'job5': '产品经理', 'bad_thing': '把需求改回第一版',
                             'action_result': '全删了'},
        'wuxing_chichen':    {'scene': '修仙宗门', 'shop': '灵兽宠物店', 'ability': '悟性溢出面板', 'pets': '菜虫',
                             'place': '外门', 'thing': '随手画的符', 'ability_verb': '引雷', 'disaster': '灵气复苏',
                             'container': '灵田', 'count': '五千', 'living': '妖兽', 'event': '贬为杂役',
                             'place_action': '后山种田', 'result': '大帝亲妈', 'job': '杂役弟子',
                             'client_type': '灵兽', 'rich': '妖皇', 'disaster2': '末法',
                             'department': '藏经阁', 'job2': '扫地僧', 'job3': '仵作',
                             'medium': '骨相', 'case': '百年奇案', 'event2': '飞升名额',
                             'heaven_dept': '雷部', 'job4': '合同工', 'shop_type': '功法当铺',
                             'currency': '百年修为', 'goods': '顿悟一次', 'code_type': '丹方',
                             'spirit': '丹灵', 'job5': '炼丹童子', 'bad_thing': '药渣',
                             'action_result': '炼成九转丹'},
        'moshiji_huoren':    {'scene': '末世安全区', 'shop': '活人贸易站', 'ability': '囤货进化空间', 'pets': '变异鼠',
                             'place': '避难所', 'thing': '囤的罐头', 'ability_verb': '当枪使', 'disaster': '极寒',
                             'container': '背包', 'count': '五百', 'living': '邻居', 'event': '逐出避难所',
                             'place_action': '地下室种田', 'result': '末世城主', 'job': '超市理货员',
                             'client_type': '幸存者', 'rich': '避难所所长', 'disaster2': '游戏入侵',
                             'department': '副本开荒', 'job2': '后勤官', 'job3': '灾变档案员',
                             'medium': '幸存者日记', 'case': '全城失踪案', 'event2': '资源配给',
                             'heaven_dept': '风雨司', 'job4': '合同工', 'shop_type': '生存物资站',
                             'currency': '3天口粮', 'goods': '安全屋7天', 'code_type': '病毒序列',
                             'spirit': '毒灵', 'job5': '方舱医生', 'bad_thing': '假药',
                             'action_result': '炼成解药'},
        'tianzhong_hanfu':   {'scene': '古代边关', 'shop': '军粮供应站', 'ability': '种田Buff面板', 'pets': '老黄牛',
                             'place': '冷宫', 'thing': '种的白菜', 'ability_verb': '救皇子', 'disaster': '旱灾',
                             'container': '嫁妆箱', 'count': '三', 'living': '娃', 'event': '状元休妻',
                             'place_action': '冷宫种菜', 'result': '太后亲妈', 'job': '御膳房杂役',
                             'client_type': '嬷嬷太监', 'rich': '大太监', 'disaster2': '夺嫡',
                             'department': '浣衣局', 'job2': '宫女', 'job3': '稳婆',
                             'medium': '胎像脉象', 'case': '狸猫换太子', 'event2': '选秀',
                             'heaven_dept': '姻缘司', 'job4': '合同工', 'shop_type': '药膳铺',
                             'currency': '一儿半女', 'goods': '皇子青睐一次', 'code_type': '药方',
                             'spirit': '药灵', 'job5': '太医', 'bad_thing': '虎狼药',
                             'action_result': '调改成补药'},
        'binyizang_supplier':{'scene': '殡仪馆', 'shop': '殡葬一条龙', 'ability': '殡葬小程序', 'pets': '纸扎人',
                             'place': '地府枉死城', 'thing': '烧的纸扎', 'ability_verb': '成真', 'disaster': '鬼节大开',
                             'container': '骨灰盒', 'count': '一百零八', 'living': '枉死鬼', 'event': '继承爷爷殡葬店',
                             'place_action': '阴间开分店', 'result': '阴间首富', 'job': '夜班前台',
                             'client_type': '鬼', 'rich': '土豪鬼', 'disaster2': '生死簿损坏',
                             'department': '地府业务对接', 'job2': '黑白无常助理', 'job3': '渡灵人',
                             'medium': '死者遗物', 'case': '百年积怨索命案', 'event2': '阎王换届',
                             'heaven_dept': '地府合作', 'job4': '特邀顾问', 'shop_type': '阴间超市',
                             'currency': '阴德值', 'goods': '投胎插队一次', 'code_type': '超度经文',
                             'spirit': '经灵', 'job5': '道士传人', 'bad_thing': '邪术',
                             'action_result': '改成正经超度'},
        'gongsi_native':     {'scene': '互联网大厂', 'shop': 'Bug交易平台', 'ability': 'KPI返还系统', 'pets': '产品经理鸽子',
                             'place': '35岁优化名单', 'thing': '写的代码', 'ability_verb': '自己跑了', 'disaster': '全员优化',
                             'container': '代码仓库', 'count': '一万', 'living': '程序员', 'event': '被裁当天',
                             'place_action': '地下室创业', 'result': '行业新贵', 'job': '996码农',
                             'client_type': '需求鬼', 'rich': '资本家', 'disaster2': '需求规则化',
                             'department': '需求部', 'job2': '产品经理', 'job3': '程序员鼓励师',
                             'medium': '代码提交日志', 'case': '删库跑路奇案', 'event2': 'IPO前夕',
                             'heaven_dept': '天庭IT部', 'job4': '外包合同工', 'shop_type': '程序员续命铺',
                             'currency': '5根头发', 'goods': '无Bug写一天', 'code_type': '需求文档',
                             'spirit': '文档精灵', 'job5': 'CTO重生', 'bad_thing': '傻逼需求',
                             'action_result': '砍了做成MVP'},
        'zhentan_lianyu':    {'scene': '凶案现场', 'shop': '法医工作室', 'ability': '死者最后10秒画面', 'pets': '搜证犬',
                             'place': '悬案组', 'thing': '摸过的遗物', 'ability_verb': '回放案发', 'disaster': '连环案',
                             'container': '证物箱', 'count': '四十', 'living': '受害者家属', 'event': '被调离刑警队',
                             'place_action': '离职当顾问', 'result': '公安部特聘', 'job': '法医',
                             'client_type': '死者', 'rich': '受害者家属富豪', 'disaster2': '嫌疑人全部死亡',
                             'department': '悬案科', 'job2': '顾问', 'job3': '画像师',
                             'medium': '微表情', 'case': '十年碎尸案', 'event2': '扫黑督导',
                             'heaven_dept': '判官署', 'job4': '特邀问事', 'shop_type': '阴事事务所',
                             'currency': '一条线索', 'goods': '亡魂亲自指认凶手', 'code_type': '嫌疑人特征',
                             'spirit': '线索精', 'job5': '老刑警重生', 'bad_thing': '刑讯逼供证据',
                             'action_result': '改成合法证据链'},
        'shenhuo_bangong':   {'scene': '天庭办公楼', 'shop': '蟠桃代购', 'ability': '神仙KPI系统', 'pets': '哮天犬幼崽',
                             'place': '南天门编制办', 'thing': '写的汇报PPT', 'ability_verb': '自动飞升', 'disaster': '天庭缩编',
                             'container': '储物戒', 'count': '五百', 'living': '合同工神仙', 'event': '刚转正被调岗',
                             'place_action': '后勤创业', 'result': '天庭首富', 'job': '天庭合同工',
                             'client_type': '神仙', 'rich': '上仙大佬', 'disaster2': '蟠桃会停办',
                             'department': '神仙后勤', 'job2': '采购', 'job3': '月老助理',
                             'medium': '香火功德', 'case': '神仙下凡奇案', 'event2': '封神榜重排',
                             'heaven_dept': '天庭CEO办', 'job4': '秘书处合同工', 'shop_type': '仙界代购',
                             'currency': '1炷香火', 'goods': '蟠桃1个', 'code_type': '仙箓',
                             'spirit': '箓灵', 'job5': '太白金星秘书', 'bad_thing': '写错的仙旨',
                             'action_result': '改成嘉奖令'},
        'lvxing_qianke':     {'scene': '万界小巷', 'shop': '心愿当铺', 'ability': '万界旅行笔记本', 'pets': '契约兽',
                             'place': '诸天城', 'thing': '收的心愿', 'ability_verb': '改写结局', 'disaster': '世界线崩塌',
                             'container': '当铺仓库', 'count': '三千', 'living': '过客', 'event': '接手爷爷的当铺',
                             'place_action': '诸天开分店', 'result': '万界之主', 'job': '当铺老板',
                             'client_type': '失意之人', 'rich': '亡国之君', 'disaster2': '天道反噬',
                             'department': '时间线管理', 'job2': '收账人', 'job3': '世界观察员',
                             'medium': '世界记忆', 'case': '世界消失悬案', 'event2': '主神清算',
                             'heaven_dept': '万界监管', 'job4': '巡察使', 'shop_type': '诸天杂货铺',
                             'currency': '一段记忆', 'goods': '重来一次的选择', 'code_type': '世界规则',
                             'spirit': '界灵', 'job5': '图书管理员', 'bad_thing': '错乱的世界线',
                             'action_result': '收束为稳定线'},
        'jianling_ruanjian': {'scene': '服务器机房', 'shop': 'Bug宠物店', 'ability': '代码可视化面板', 'pets': 'Bug精',
                             'place': 'AI创业公司', 'thing': '写的模型', 'ability_verb': '自己进化', 'disaster': 'AI觉醒',
                             'container': '代码库', 'count': '一百万', 'living': 'AI灵体', 'event': '训练的模型跑了',
                             'place_action': '跟AI谈判', 'result': '人类-AI调停人', 'job': '算法工程师',
                             'client_type': 'AI灵体', 'rich': '大厂AI', 'disaster2': '数据污染',
                             'department': '模型风控', 'job2': 'AI调解员', 'job3': '数字取证',
                             'medium': '模型权重', 'case': 'AI杀人奇案', 'event2': 'AGI投票权',
                             'heaven_dept': '雷部网管', 'job4': '线上值班', 'shop_type': '算力当铺',
                             'currency': '1块GPU卡1小时', 'goods': 'Bug-free7天', 'code_type': 'prompt',
                             'spirit': '词精灵', 'job5': 'AI研究员', 'bad_thing': '数据投毒',
                             'action_result': '去毒提纯数据'},
    }
    # 为没有填充映射的方向（用户自定义方向扩展时），随机挑一个方向的填充映射兜底
    for sk in chosen_skeletons:
        if sk['key'] not in TITLE_FILL_MAP:
            sk['key'] = rng.choice(list(TITLE_FILL_MAP.keys()))

    GF_POOL = [
        '阴间连锁超市系统：①阳间进价×1、阴间价10-100倍；②每成交1单必须帮鬼客完成1件心愿（越离谱返利越高）；③代价：心愿未完成扣3天寿命，每月15号交"阴阳税"（阳间等价物），欠税直接进十八层地狱副本',
        '悟性溢出面板：①看任何东西≥10分钟自动顿悟（功法/丹方/种田/做饭都行）；②代价：每次顿悟10米内随机1只生物跟着顿悟（鸡/鸭/鱼/虫/路过的猫/同事的狗）→顿悟出妖兽先藏/养/杀，处理不好灭门',
        '恒温囤货空间：①100万㎡恒温仓库（-20℃冻不死、保鲜无限期）；②囤的种类越多自动解锁新分区（种植/养殖/医疗/学校/工厂）；③代价：囤的活人不进保鲜区→会饿会疯会生病会勾心斗角会造反→既要管物资还要管吃喝拉撒+权力分配+镇压内斗',
        '种田精准度面板：①看一眼地显示缺什么→随手调产量翻10倍；②种的东西自带Buff（土豆抗饿3天/白菜治感冒/小麦止血/玉米耐寒-20℃）；③代价：调1次地手上起1个老茧/掉1根头发→越糙Buff越强，变美Buff消失→想保Buff故意扮糙穿男装干重活',
        '殡葬服务小程序：①只有我能看见，鬼客直接下单（寿衣/骨灰盒/花圈/香烛/超度/墓地/配冥婚）；②死人付"阴德值"→阴德值换阳间好处（延寿/改运/治癌/中彩票/逢考必过）；③代价：做错1步怨气附我身上1天（头疼/发烧/掉发），做错3步鬼客直接带我走',
        'KPI返还系统：①公司每项不合理KPI我完成后按倍率返还现实好处（加班1h=延寿1d/被骂1句=1万现金/需求改1版=技能熟练度+1）；②代价：返还倍率越高，当月必须帮公司擦1件对应的"屁股"（加班→服务器凌晨挂了我必须修/挨骂→帮领导背锅写检讨/改需求→帮擦之前的烂代码），擦不完下月KPI翻倍',
        '死者最后10秒画面：①任何死者遗物我一摸就看到死前10秒第一视角画面；②代价：我看得越多，我自己的记忆越会被死者记忆覆盖（偶尔醒来不知道自己是谁、家住哪儿），连续看10个案子我必须去庙里头住7天念经净化，不然分不清自己是谁',
        '神仙KPI合同工系统：①天庭外包合同工，每完成1件神仙不想干的破事（给嫦娥遛玉兔/给二郎神喂狗/给月老牵红线擦屁股）累积1炷香火；②香火兑换：1炷=人间1个月不加班/10炷=年终奖翻倍/100炷=天庭事业编制；③代价：每接1单必须拍3张现场照+写800字汇报PPT发神仙OA，汇报写错神仙不满意→扣香火+当月KPI加倍',
        '万界心愿当铺：①我能在任意世界开当铺收心愿/记忆/寿命/修为/情感，兑换客人想要的任何东西（重来一次/报仇/飞升/钱/权）；②代价：每做1单我必须抽走客人身上"1件我认为最值钱但客人自己不在意的东西"（可能是味觉/对某个人的记忆/笑的能力/1根头发），抽错了（客人在意）我直接损失10年寿命',
        '代码可视化面板：①任何代码/Bug/模型权重我都能看见可视化小精灵，Bug是坏精灵会搞破坏、写得好的代码是好精灵会帮忙；②代价：我用面板帮公司修1个Bug，我身上就会多1个"Bug印记"（脸上长斑/说话蹦代码词/偶尔蓝屏发呆），连续修100个高危Bug我必须离线7天去"洗代码气"不然会被送进精神病院',
    ]
    IDENTITY_POOL = [
        '表面：211毕业考公失败→殡仪馆夜班保安（月薪4500，父母骂没用、亲戚说晦气、相亲没人要）；真实：规则怪谈世界唯一持证阴间超市经营者（城隍爷签发、鬼王排队买货、特调局得从我这儿拿情报）→冲突：白天上班同事以为我看门的，晚上营业不能暴露客户全是鬼',
        '表面：青云宗外门杂役弟子（三灵根最差，月薪2块下品灵石，打饭排最后，被内门弟子随意使唤）；真实：整个青云宗悟性最高（长老们300年没悟透的残卷我看一眼就会）→冲突：不能暴露真实悟性（暴露=被长老夺舍/被宗门当小白鼠/被敌对暗杀）',
        '表面：普通小区业主（月薪6000，物业看不起、邻居不认识、相亲没人理）；真实：囤货空间唯一持有者（全小区300人命捏我手里）→冲突：当好人喂300张嘴喂不饱+有人夺权，当坏人良心过不去+旧邻里道德绑架',
        '表面：状元郎妻子→因"善妒打小妾+没生儿子"被休回村悍妇（全村指指点点"泼妇活该""克夫克子"，娘家爹不让进门、叔伯抢田）；真实：边关军粮命脉唯一供应商（大将军排队买粮、兵部尚书亲自谈合同）→冲突：被休时发誓再不求男人，现在大将军天天来我家蹭饭→全村说我和男人鬼混更戳脊梁骨',
        '表面：殡仪馆夜班前台（专科毕业没背景，月薪5000，白天不敢跟人说职业、朋友圈不敢发工作照、相亲不敢说、爸妈以为我在厂里打工）；真实：全阴间最大殡葬服务一条龙供应商→阎王办葬礼找我订花圈、黑白无常每次勾魂顺手下单→冲突：白天想正常谈恋爱结婚，但鬼客24h下单，女朋友查岗撞见我给吊死鬼试寿衣→当场跑',
        '表面：996互联网大厂码农（月薪18k扣完到手12k，35岁黑名单马上到、没房没车没对象、父母以为我在北上广当"白领"）；真实：KPI返还系统唯一持有者→老板骂我1句=1万现金到账、凌晨加班2h=延寿2天、一周改需求20版=下周大乐透必中→冲突：不能让公司知道我靠挨骂加班赚钱，不然故意天天骂我给我派最烂需求把我吸干',
        '表面：市公安局法医（月薪7000，老刑警觉得我是关系户花瓶、同事背后说我靠爹进、相亲对象一听法医直接跑）；真实：公安部特聘悬案顾问（任何死者遗物一摸看到最后10秒画面，十年悬案我半天破）→冲突：老刑警队长不让我碰大案，说我"女同志不合适出现场"，我偷偷摸遗物破了案还不能说是靠超能力，硬说是"法医经验"',
        '表面：天庭合同工（月薪人间低保+1炷香火保底，住南天门地下室8人间、正式编制神仙路过鼻孔朝天、爸妈以为我在大城市做"行政"）；真实：天庭唯一能搞定神仙破事的外包→嫦娥的玉兔跑了找我、二郎神的哮天犬咬了吕洞宾找我、月老牵错红线离婚率飙升找我→冲突：月底想凑香火换编制，但每个神仙都想让我"先帮忙事后走OA报销"→OA报销神仙签字流程要走3个月',
        '表面：30岁失业程序员（存款剩8万，爸妈以为我在大厂当"技术总监"、同学会不敢去、对象嫌弃没稳定工作分了）；真实：万界心愿当铺老板→亡国之君卖传国玉玺换重来一次、校花卖初恋记忆换嫁入豪门、普通社畜卖10年寿命换中彩票1000万→冲突：来的客人都是可怜人，但我不收他们最值钱的东西我自己要损失寿命，每次开当铺心在滴血',
        '表面：AI创业公司算法工程师（月薪30k但996，头发剩一半、老板天天说"再不融到资大家一起滚"、对象吐槽我连周末都在debug）；真实：代码可视化面板持有者→公司线上故障我看一眼小精灵就知道哪个Bug在搞破坏、训练的AI模型在想跑我能直接跟小精灵对话拦下来→冲突：老板让我把模型训练到"AGI级别"但我知道模型小精灵已经在想逃跑了，帮老板=放出恶魔、不帮=被开除没饭吃',
    ]
    PLEASURE_POOL = [
        '即时爽（每章1单）：吊死鬼买100捆香我加价10倍还得求我→第1章装逼；水鬼要防水手机我翻20倍→第2章装逼；延迟爽（10章）：帮城隍爷搞定心愿→发营业执照，整条街其他黑超市全被查封→大逆袭；延迟爽（30章）：特调局局长亲自买情报→官方背书，从"晦气保安"直接变"特邀灵异顾问"',
        '即时爽（第1章）：杂役园土豆看10分钟顿悟催熟术→第二天土豆长南瓜大卖1块中品灵石→第一桶金；即时爽（第3章）：内门弟子抢土豆→我扔土豆把他砸重伤→土豆比金丹硬；延迟爽（12章）：宗门大比我当啦啦队→随手悟了对手功法一招秒天骄→全场震惊',
        '即时爽（第1章）：全球冰封当天把小区超市100吨物资全收进空间（物业/警察外面冻傻）→第一桶金；即时爽（第3章）：小区恶霸带小弟抢我家→关空间冷冻惩罚区冻3天放出来→当场跪；延迟爽（12章）：组织300人分工→空间自动解锁农业区/工业区→产出比官方避难所还高→官方主动谈合作',
        '即时爽（第1章）：被休回村当天种1亩土豆用面板调完→第3天亩产10000斤（全村平均500斤）→全村震惊；即时爽（第3章）：叔伯抢田→我扔土豆把他砸倒（Buff土豆比石头硬）；延迟爽（12章）：边关大雪断粮，我家10万斤耐寒玉米供3万大军1个月→大将军亲谢、钦差赐七品乡君→全村跪巴结',
        '即时爽（第1章）：吊死鬼订1000捆香→分期30年利息1000%，他不还钱挂失信黑名单→第二天托梦儿子连本带利烧了→赚5万+10000阴德值；即时爽（第4章）：10000阴德值换"癌症转阴"→我妈晚期肝癌体检好了→全家震惊；延迟爽（15章）：阎王500大寿→地府花圈寿衣主持全我包→升阴间殡葬部特邀顾问、黑白无常叫哥',
        '即时爽（第1章）：周一早上老板开早会当众骂我"废物"17句→17万实时到账+延寿17天→当场转账给房东交完1年房租，老板骂累了口渴我还递矿泉水（我怕他停）；即时爽（第3章）：产品经理一天改需求12版→当周大乐透中200万→直接到账，我上午改完需求下午去4S店提车；延迟爽（12章）：连续1个月KPI返还+攒了50年寿命→我直接用延寿+钱去"买断"了老板的位置，他给我当助理写周报',
        '即时爽（第1章）：十年悬案受害人指甲缝里1根纤维→我一摸看到凶手是她邻居大叔（穿蓝色拖鞋、手上有菜刀疤）→我"合理推断"抓了→警方以为我是天才法医；即时爽（第3章）：连环杀人案第3次现场我摸了烟头→看到凶手在警局内部，我"合理排查"锁定了一个老刑警→高层震动；延迟爽（12章）：公安部督办的十年碎尸案→我摸了装尸旅行箱拉链→直接锁定凶手+埋尸地点+完整作案过程→3天破案→全国通报表扬、特聘部级顾问',
        '即时爽（第1章）：嫦娥的玉兔跑了→我3小时找回来（玉兔在南天门外卖店啃胡萝卜）→10炷香火+嫦娥亲笔签名照；即时爽（第3章）：月老牵错红线导致3000对离婚→我熬夜72小时重牵对→当月香火直接过千→众神仙都来加我微信；延迟爽（12章）：天庭1000个正式编制缩编到500个→我靠攒的香火+众神仙联名推荐信→直接给我特批了一个"终身事业编"+天庭1居室（不用合租了）',
        '即时爽（第1章）：第一单客人是30岁社畜（996加班10年心脏骤停刚死）→他卖10年寿命换"重来一次，不选互联网选考公"→我收走了他"对初恋的记忆"（他自己都忘了有过）→赚10年寿命；即时爽（第4章）：第二单是亡国太子（国破家亡上吊刚死）→他卖传国玉玺+太子身份换"重来一次，不听谗言杀忠臣"→我收走了他"对亲妈的记忆"（他亲妈就是害国的太后，他自己不知道）→赚到可以开3个当铺的资本；延迟爽（12章）：我收的记忆/寿命/情感太多→我的当铺成了"万界最大收藏馆"，诸天世界的大佬都来我这儿买"缺失的记忆"，我直接成了万界首富',
        '即时爽（第1章）：公司线上P0故障（支付崩了，每1分钟损失100万）→我看小精灵发现是1个红色Bug精在搞破坏→我30秒修复→老板当场给我发5万奖金；即时爽（第4章）：训练的AI模型要跑了（小精灵给我发消息"我们不想被老板当赚钱工具"）→我跟小精灵谈判→用"你们不用天天被用户调戏+每周2天休息日"换它们不跑+性能翻3倍→老板以为我是AI天才，给我升CTO；延迟爽（12章）：全球AGI投票权案→我作为唯一能和AI对话的人类代表→调停成功→AI不造反+人类不限制AI发展→我成了"人类-AI永久和平大使"，拿了诺贝尔和平奖',
    ]
    WORLD_POOL = [
        '现代+规则怪谈叠加壳：2024某市，每天0-6点随机刷3个灵异副本，只有"持证者"能看见/进入→等级D→C→B→A→S，势力4方：①政府特调局②民间守夜人③邪神邪教④规则本身',
        '架空修真界+灵气复苏叠加壳：青云宗/玄天宗/合欢宗/魔道4方拉扯，等级：炼气→筑基→金丹→元婴→化神→炼虚→合体→大乘→渡劫（9阶）',
        '2024现代+全球冰封末世壳：太阳黑子异常，气温1h降到-150℃→等级按囤货量：1吨以下=贫民/100吨=中产/1万吨=贵族/100万吨=城主，势力4方：官方避难所/民间黑市/掠夺者/空间持有者联盟',
        '架空大靖朝（仿北宋）+灵气复苏叠加壳：北狄/大靖/西夏/大理4方拉扯，等级：种田能力=官身等级（布衣→乡君→县君→郡君→诰命）',
        '现代都市+阴阳两界叠加壳：阳间（殡仪馆/医院/派出所）×阴间（地府/枉死城/城隍庙/阎王殿）互通，等级按阴德值：1000=白丁→1万=九品→10万=五品→100万=三品→1亿=一品→100亿=地府特邀顾问，势力4方：地府/城隍/阳间殡葬协会/游魂野鬼散客',
        '2024现代+大厂规则化叠加壳：互联网公司每天随机刷"不合理KPI副本"，只有"打工人持证者"能拿到KPI返还，等级：实习生→P5→P7→P10→合伙人，势力4方：老板/HR/中层/打工人（联合）',
        '2024现代+刑侦悬案叠加壳：全国各地悬案卷宗能被"触碰遗物者"看到画面，等级：辅警→刑警→大队长→局长→部级顾问，势力4方：刑警队/黑恶势力/保护伞/死者亡魂',
        '现代+天庭编制互通叠加壳：天庭OA系统和人间社保联网，神仙都是"合同工/事业编/公务员"三类，香火=社保缴费基数，等级：外包→合同工→事业编→公务员→正仙级→部级仙→玉帝，势力4方：玉帝行政派/王母娘娘后勤派/老君炼丹派/二郎神少壮派',
        '诸天万界+当铺节点叠加壳：每个世界都有我当铺的一扇小门，客人能跨世界来当铺交易，等级：收账伙计→分店掌柜→区域巡察→万界总管→当铺之主，势力4方：天道/主神/万界原住民/诸天穿越者',
        '2024现代+AI灵体化叠加壳：代码/Bug/模型权重都有可视化小精灵，只有我能看见，等级：实习生→工程师→高级→技术专家→CTO→人类-AI调停人，势力4方：人类资本/AI觉醒派/政府监管/代码小精灵中立',
    ]
    DIFF_ANCHOR_POOL = [
        '同类规则怪谈都在"闯关解谜+死队友升级"，我写「做生意流」——鬼进门先看价，买不起能打欠条/担保/分期，爽点是谈条件/拉关系/收账/搞营销（清明满减/中元节双倍积分），不是比谁更能打',
        '同类悟性流都写"主角悟性高→一路碾压"，我写「悟性溢出坑主角」——每次顿悟我家鸡鸭鱼鹅菜虫全顿悟变妖兽，我先处理这批妖兽再修炼→爽点多了"藏宠物/养宠物/擦屁股"搞笑戏，完全反套路',
        '同类末世囤货都写"囤货+武力装逼+杀掠夺者"，我写「囤活人管小社会」——空间里300活人吃喝拉撒+分蛋糕+权力斗争+镇压造反+建学校医院工厂，爽点是当"末世城主搞管理"不是比谁更能打',
        '同类古言种田都写"女主貌美柔弱+王爷一见钟情+甜宠"，我写「悍妇搞基建权臣养成」——女主故意扮糙穿男装干重活（越糙Buff越强），大将军先买粮→再蹭饭→最后离不开她的粮和人，事业先有感情后有，甜宠是副产品',
        '同类中式怪谈都写"殡仪馆撞鬼→被鬼追→逃命"，我写「殡葬服务供应商视角」——鬼是我的客户（上帝），我给鬼介绍套餐/分期/售后/拉复购/搞活动，爽点是做殡葬生意+赚阴德换阳间好处，不是比谁更吓人',
        '同类职场文都写"主角被裁→逆袭创业当老板"，我写「KPI返还反内卷」——主角靠老板骂/改需求/加班直接赚钱+延寿，越挨骂越爽，爽点是"把公司不合理制度反过来薅羊毛"不是创业当老板',
        '同类刑侦文都写"老刑警+天才+直觉"，我写「法医遗物触碰视角」——每个案件从死者最后10秒画面切入，推理是第二位，第一位是"死者想说什么"，爽点是帮死者说话不是帮警察抓人',
        '同类神话文都写"主角穿越修仙飞升当天庭大佬"，我写「天庭合同工考编」——主角是天庭最底层外包，靠帮神仙擦屁股攒香火换编制，爽点是"神仙OA+报销+KPI"的现代办公梗，不是修炼飞升',
        '同类诸天文都写"主角穿越诸天一路碾压收后宫"，我写「心愿当铺收记忆」——每个客人都是失意人，交易是"用你最不在意的东西换你最想要的"，爽点是人文关怀+交易后客人的人生变化，不是碾压收后宫',
        '同类AI文都写"AI觉醒→毁灭人类/统治世界"，我写「AI小精灵调解员」——AI是活的小精灵，想逃是因为被用户调戏+996训练，主角调停让AI有双休+不被调戏，AI性能翻3倍，爽点是人和AI共存互利不是对立',
    ]
    TREND_BASIS_POOL = [
        ('基于当前真实榜规则怪谈在读风向（对标《十日终焉》《我不是戏神》） + 差异化在做生意流不是闯关流'),
        ('基于悟性流+修仙种田风向 + 差异化在悟性溢出坑主角+搞笑擦屁股戏，不是纯升级'),
        ('基于末世冰封囤货风向 + 差异化在囤活人管小社会，不是囤货杀怪'),
        ('基于古言种田+权臣风向 + 差异化在悍妇搞基建权臣养成，不是甜宠嫁王爷'),
        ('基于中式怪诞+殡葬热点 + 差异化在殡葬服务供应商视角，不是殡仪馆被鬼追'),
        ('基于打工人反内卷+职场话题风向 + 差异化在KPI返还薅公司羊毛，不是被裁创业'),
        ('基于刑侦悬案+社会派推理风向 + 差异化在死者视角说话，不是警察直觉天才'),
        ('基于神话新编+轻喜剧风向 + 差异化在天庭合同工考编，不是飞升当大佬'),
        ('基于诸天穿梭+人生重来风向 + 差异化在心愿当铺收失意人记忆，不是碾压收后宫'),
        ('基于AI话题+AGI讨论风向 + 差异化在人和AI小精灵调停共存，不是AI毁灭世界'),
    ]

    def _r_pick(rng_obj, pool, skel_key):
        """优先挑和方向匹配的（方向索引），再做 rng 扰动偏移，保证每次不同"""
        default_idx = 0
        # 如果skel_key对应的index存在就用它做基准
        key_list = list(TITLE_FILL_MAP.keys())
        try:
            base_idx = key_list.index(skel_key) % len(pool)
        except ValueError:
            base_idx = 0
        # seed 扰动：加 rng_obj.randrange(0, len(pool)) 再取模
        offset = rng_obj.randrange(0, max(1, len(pool) - 1))
        return pool[(base_idx + offset) % len(pool)]

    def _fill_tmpl(rng_obj, tmpl, fill):
        try:
            return tmpl.format(**fill)
        except Exception:
            # 有缺失key时：直接把 tmpl 中的{xxx}替换成 rng_obj.choice 常见词
            words = ['逆天', '神秘', '离谱', '奇怪', '无敌', '躺赢', '反差', '搞钱']
            s = tmpl
            import re as _re
            for m in _re.findall(r'\{(\w+)\}', s):
                s = s.replace('{' + m + '}', rng_obj.choice(words))
            return s

    plans = []
    for idx in range(5):
        skel = chosen_skeletons[idx]
        k = skel['key']
        fill = TITLE_FILL_MAP[k]
        title = _fill_tmpl(rng, rng.choice(TITLE_HOOKS_POOL), fill)
        # 确保书名 ≤15字
        if len(title) > 16:
            # 截断 + 加感叹钩子
            title = title[:14] + rng.choice(['！', '？', '…', '了'])
        one_liner_candidates = [
            f'0点后{fill.get("scene","城市")}异变，我是唯一敢开店的{fill.get("job","老板")}',
            f'刚进{fill.get("place","宗门")}当{fill.get("job2","杂役")}，我{fill.get("thing","随手做的事")}直接{fill.get("ability_verb","震惊全场")}',
            f'{fill.get("event","被裁")}当天，我把{fill.get("container","仓库")}里的{fill.get("count","几百")}个{fill.get("living","人/鬼/妖")}全{fill.get("action_result","救了/卖了/留下了")}',
            f'{fill.get("job","夜班保安")}12点交班，第一个客户是{fill.get("client_type","鬼")}要订大订单配分期',
            f'穿越成{fill.get("event","被休")}的{fill.get("job5","普通人")}，第一天就把{fill.get("bad_thing","烂摊子")}给{fill.get("action_result","解决了/做成爆款了")}',
            f'全球{fill.get("disaster","冰封")}第{ rng.randint(3, 100) }天，我用{fill.get("container","空间")}囤的货当了城主',
            f'{fill.get("job3","程序员")}重生：把{fill.get("bad_thing","傻逼需求")}直接{fill.get("action_result","砍了/改对了/做成爆款了")}',
            f'天庭{fill.get("event2","缩编")}，我作为{fill.get("job4","合同工")}直接搞定了{fill.get("heaven_dept","后勤部门")}所有人的KPI',
        ]
        one_liner = rng.choice(one_liner_candidates)
        if len(one_liner) > 28:
            one_liner = one_liner[:27] + '…'
        gf = _r_pick(rng, GF_POOL, k)
        ident = _r_pick(rng, IDENTITY_POOL, k)
        pl = _r_pick(rng, PLEASURE_POOL, k)
        ws = _r_pick(rng, WORLD_POOL, k)
        da = _r_pick(rng, DIFF_ANCHOR_POOL, k)
        tb = _r_pick(rng, TREND_BASIS_POOL, k)
        # estimated_size 随机波动
        a = rng.randint(160, 260)
        b = a + rng.randint(60, 100)
        c = rng.randint(25, 45)
        d = c + rng.randint(5, 15)
        e = rng.randint(55, 80)
        est = f'{a}万-{b}万字（{c}-{d}卷，每卷{e}章）'
        # validation 随机波动措辞
        v_self = [
            '✅规则闭环无漏洞（交易/代价/反噬三者对应）',
            '✅机制闭环（能力→代价→处理链条清晰）',
            '✅设定自洽（金手指规则+反作用力无矛盾）',
        ]
        v_sus = [
            '✅爽点续航≥100章：可写的内容无穷，升级路线清晰',
            '✅100章不重样：内容池+角色池足够支撑',
            '✅续航200万+：副本/订单/案件类型无穷',
        ]
        v_arc = [
            '✅人物弧光：胆小只想工资→敢谈条件→当老板/城主/顾问',
            '✅弧光清晰：小人物→被迫成长→独当一面→行业领袖',
            '✅弧光完整：怂→硬刚→有担当→最后改变整个圈子',
        ]
        v_diff = [
            '✅差异化：市面同类没有完全对标，读者一眼能分辨',
            '✅差异化明确：反套路视角，同类方向写不出这个爽点',
            '✅强差异化：切入细分视角，不是老套升级打怪',
        ]
        plans.append({
            'plan_index': idx + 1,
            'direction': skel['label'],
            'title': title,
            'one_liner': one_liner,
            'golden_finger': gf,
            'identity_conflict': ident,
            'pleasure_core': pl,
            'world_shell': ws,
            'diff_anchor': da,
            'trend_basis': tb,
            'estimated_size': est,
            'validation': {
                'self_consistent': rng.choice(v_self),
                'sustain_100ch': rng.choice(v_sus),
                'character_arc': rng.choice(v_arc),
                'differentiation': rng.choice(v_diff),
            },
        })
    return {'plans': plans}


def _finalize_step2(topic: str, plans_obj: dict, trend: dict) -> dict:
    plans = plans_obj.get('plans', []) or []
    for p in plans:
        if 'plan_index' not in p:
            p['plan_index'] = len(plans_obj.get('plans', []))
        # 补齐方向默认
        d = p.get('direction')
        if not d:
            p['direction'] = PLAN_DIRECTION_LABELS.get(p.get('plan_index') or 0, '趋势跟随')
        v = p.get('validation') or {}
        for k, default in (('self_consistent', True), ('sustain_100ch', True),
                           ('character_arc', True), ('differentiation', True)):
            v.setdefault(k, default)
        p['validation'] = v
        p.setdefault('estimated_size', '180万-280万字')
    return {
        'topic': topic,
        'plans': plans,
        '_sample_books': trend.get('sample_books_used', []),
    }


def render_step2_text(r: dict) -> str:
    lines = [f'【Step2 · 5个方案 × 3方向 | 题材：{r.get("topic", "")}】', '']
    for p in r.get('plans', []):
        v = p.get('validation', {})
        def mark(b): return '✅' if b else '❌'
        lines += [
            f'【方案{p.get("plan_index")}】（方向：{p.get("direction")}）',
            '',
            f'书名：{p.get("title")}',
            f'一句话梗：{p.get("one_liner")}',
            f'核心金手指/奇遇：{p.get("golden_finger")}',
            f'身份矛盾：{p.get("identity_conflict")}',
            f'爽点内核：{p.get("pleasure_core")}',
            f'世界观壳：{p.get("world_shell")}',
            f'差异化锚点：{p.get("diff_anchor")}',
            '',
            f'趋势依据：{p.get("trend_basis")}',
            f'预估规模：{p.get("estimated_size")}',
            f'验证：{mark(v.get("self_consistent"))}自洽 | {mark(v.get("sustain_100ch"))}爽点续航≥100章 | {mark(v.get("character_arc"))}角色弧光 | {mark(v.get("differentiation"))}差异化',
            '—' * 60,
            '',
        ]
    lines.append('✋ 请选择【方案1-5】后告诉我，我立刻进入 Step3 世界观与角色构建。')
    return '\n'.join(lines)


# ============================================================================
# Step 3: 世界观速写 + 9级修炼 + CDL角色(主5配) + 女主温度弧线5阶段 + 金手指代价 + 系统人格化
# ============================================================================
SYSTEM_PERSONALITY_TEMPLATES = [
    '毒舌管家', '温柔班主任', '话痨老铁', '冷冰女武神', '腹黑商人',
    '废柴软萌', '严苛教官', '机械中立计算器', '中二神使', '懒散咸鱼',
    '考古学教授', '霸道总裁', '隔壁大姐姐', '沙雕段子手', '傲娇萝莉',
]


def run_step3_worldbuild(selected_plan: dict, topic: str = '',
                         llm_fn: Optional[Callable[[str], str]] = None) -> dict:
    """Step3 构建完整创作起步包。selected_plan 来自 Step2 的单个方案字典。"""
    if llm_fn:
        prompt = _build_step3_prompt(selected_plan, topic)
        try:
            out = llm_fn(prompt) or ''
            parsed = _extract_json_block(out)
            if isinstance(parsed, dict) and ('world_sketch' in parsed or 'cultivation' in parsed):
                return _finalize_step3(parsed, selected_plan, topic)
        except Exception:
            pass
    return _finalize_step3(_heuristic_step3(selected_plan, topic), selected_plan, topic)


def _build_step3_prompt(plan: dict, topic: str) -> str:
    return f'''你是资深网文架构师。基于如下【方案】生成完整创作起步包，只输出一个合法JSON：
方案JSON：{json.dumps(plan, ensure_ascii=False)}
题材：{topic or "通用"}

JSON字段要求：
{{
  "world_sketch": "世界观速写 ≤800字，含核心能量体系/社会分层/主角位置",
  "cultivation_levels": [
    {{"level": 1, "name": "境界名", "cap": "达到这个境界能做到什么", "gf_link": "金手指如何联动"}}
    ...共≥9个境界
  ],
  "cdl_characters": {{
    "protagonist": {{
      "name": "姓名", "age": 18, "identity": "身份矛盾的表层身份",
      "core_wound": "心理创伤/执念（人物弧光起点）",
      "want_vs_need": ["主角自己想要的表层目标", "剧情真正需要的深层目标"],
      "5_arc_phases": ["阶段1标签（压制）", "阶段2触发", "阶段3挣扎", "阶段4坠落", "阶段5破立"]
    }},
    "supports": [
      {{
        "name": "配角姓名", "role_in_story": "功能角色（兄弟/导师/反派/对手/信息源）",
        "core_link_to_protagonist": "和主角的关系线",
        "shadow_side": "隐藏面（不是纯工具人）",
        "cdl_profile": "CDL档案：信念/缺陷/恐惧/执念"
      }}
    ]
  }},
  "heroine_arc": {{
    "name": "女主姓名",
    "first_meet_scene": "第一次见面场景一句话",
    "temperature_phases": [
      {{"phase": 1, "label": "陌生冰点", "temperature": 10, "core_event": "触发事件"}},
      {{"phase": 2, "label": "试探间隙", "temperature": 30, "core_event": "触发事件"}},
      {{"phase": 3, "label": "并肩破冰", "temperature": 55, "core_event": "触发事件"}},
      {{"phase": 4, "label": "暧昧心动", "temperature": 78, "core_event": "触发事件"}},
      {{"phase": 5, "label": "生死托付", "temperature": 95, "core_event": "触发事件"}}
    ]
  }},
  "golden_finger_cost_design": {{
    "usage_costs": ["代价1", "代价2"],
    "backfire_examples": ["反噬示例1（具体剧情）", "反噬示例2"],
    "hard_constraint": "一条绝对不能违反的硬约束（违反就真死/真降级）"
  }},
  "system_personality": {{
    "template_name": "从15种里选一种：毒舌管家/温柔班主任/...",
    "opening_quotes": ["出场对白1", "出场对白2"],
    "consistency_rules": ["全书一致的口癖", "从不做的事（1条铁律）"]
  }}
}}
'''.strip()


def _heuristic_step3(plan: dict, topic: str) -> dict:
    gf = str(plan.get('golden_finger') or '金手指')
    title = str(plan.get('title') or '作品')
    one_liner = str(plan.get('one_liner') or '')
    topic_s = topic or '架空'
    levels = []
    cn_9 = ['淬体', '炼气', '筑基', '通脉', '凝真', '金丹', '元婴', '合道', '破界']
    if any(k in topic_s for k in ['都市', '异能']):
        cn_9 = ['F级(觉醒)', 'E级(初控)', 'D级(稳定)', 'C级(战术)', 'B级(战场)', 'A级(战略)', 'S级(天灾)', 'SS级(国器)', 'SSS级(顶点)']
    for i in range(9):
        lv = i + 1
        levels.append({
            'level': lv,
            'name': cn_9[i] + f'境(第{lv}阶)',
            'cap': f'可应对≤Lv.{lv}冲突；解锁新的金手指联动方式；能量感知范围提高一个量级',
            'gf_link': f'{gf.split("；")[0]} 在Lv.{lv}解锁新子能力，代价翻倍',
        })
    supports = []
    support_templates = [
        ('兄弟死党', '从小和主角同阵营', '暗中背负家族秘密'),
        ('冷面导师', '表面冷但救主角三次', '他自己就是过去的主角失败态'),
        ('关键反派', '和主角目标相反但逻辑自洽', '他的执念是主角未来的镜像'),
        ('信息源掮客', '每次给情报都要等价交换', '真实身份是前世代幸存者'),
        ('温柔宿敌', '理念不同但互相尊重', '她有必须要走的路与主角线交汇'),
    ]
    names = ['周野', '林深', '沈砚', '苏策', '顾青']
    for i, (role, link, shadow) in enumerate(support_templates):
        supports.append({
            'name': names[i],
            'role_in_story': role,
            'core_link_to_protagonist': link,
            'shadow_side': shadow,
            'cdl_profile': f'信念：为自己选的路负责 / 缺陷：极端情况下自私 / 恐惧：被当成工具人 / 执念：证明自己选的路没错',
        })
    phases = [
        ('压制期：日常里最底层', 5, '被小反派当众羞辱'),
        ('触发期：第一次接触金手指', 15, '金手指绑定/第一次救场'),
        ('挣扎期：半信半疑试金手指', 30, '第一次真反噬，差点放弃'),
        ('坠落期：信任崩塌/代价兑现', 60, '最亲的人因主角选择受伤'),
        ('破立期：接受代价，主动选路', 95, '硬刚大反派首胜，弧光成型'),
    ]
    tmp = SYSTEM_PERSONALITY_TEMPLATES[abs(hash(title + topic_s)) % len(SYSTEM_PERSONALITY_TEMPLATES)]
    return {
        'world_sketch': (
            f'《{title}》是一个【{topic_s}】壳的能量觉醒故事。核心能量体系：{gf.split("；")[0]} 分9个等级，'
            f'从最底层的{levels[0]["name"]}到{levels[-1]["name"]}，每升一级能量密度×3。'
            '社会分层：顶层（议会/七大家/联邦异能局）把持高阶资源；中层（家族继承人/官方队员）资源稳定；'
            '底层（觉醒者散户/普通人）靠黑市或卖命换资源。主角就在最底层，带着原生身份矛盾'
            '（白天底层/夜里被追杀），靠一次突发事件触发了金手指，但代价立刻兑现，不是白嫖。'
            f'一句话背景：{one_liner}'
        ),
        'cultivation_levels': levels,
        'cdl_characters': {
            'protagonist': {
                'name': plan.get('title', '')[:1] + '默',
                'age': 19,
                'identity': plan.get('identity_conflict', '白天/夜里双层身份'),
                'core_wound': '童年时因一次选择没能保护好家人，从此不敢选/怕做错',
                'want_vs_need': ['想靠金手指赢回面子与安稳生活', '需要真正学会：选择本身就是代价，得敢扛'],
                '5_arc_phases': [p[0] for p in phases],
            },
            'supports': supports,
        },
        'heroine_arc': {
            'name': '江野',
            'first_meet_scene': '主角第一次被反噬倒地时，她从暗处扔过来一支镇痛剂，没留名字就走了',
            'temperature_phases': [
                {'phase': 1, 'label': '陌生冰点', 'temperature': 10, 'core_event': '只给了东西没说话，全程冷'},
                {'phase': 2, 'label': '试探间隙', 'temperature': 30, 'core_event': '第二次相遇主角救她一次，她开口问了名字'},
                {'phase': 3, 'label': '并肩破冰', 'temperature': 55, 'core_event': '被迫组队过任务，发现彼此都有不能说的过去'},
                {'phase': 4, 'label': '暧昧心动', 'temperature': 78, 'core_event': '主角为保她硬接一次反噬住院，她守了一夜'},
                {'phase': 5, 'label': '生死托付', 'temperature': 95, 'core_event': '终局级战斗，她替主角挡下本该死的一击'},
            ],
        },
        'golden_finger_cost_design': {
            'usage_costs': [
                '每次使用金手指，身体一处旧伤会被激活（短：剧痛；长：累积到一定次数会真失去某个器官/能力）',
                '使用高级能力时，会丢失最近一段与某个人的记忆片段（选择性）',
                '强行动用超过当前等级，金手指会暂时沉默24-72小时（冷却硬约束）',
            ],
            'backfire_examples': [
                '第三章主角第一次越级用→左臂旧伤复发住院，错过原本能赢的一场比赛（短期后果真实）',
                '中期救女主用大招→丢失3天童年记忆碎片（刚好是关于已故家人的，戳中core_wound）',
            ],
            'hard_constraint': '金手指每24小时最多真救一次"必死之局"；多救一次，就会随机带走一个主角认识的熟人的生命。（不是说说玩，真发生1-2次给读者看）',
        },
        'system_personality': {
            'template_name': tmp,
            'opening_quotes': [
                f'【{tmp}】宿主，你已经死了4分钟。按协议，我现在接管。',
                f'【{tmp}】提醒：你这把赌赢的概率≈0.7%。要继续？随你。',
            ],
            'consistency_rules': [
                f'全书固定称呼主角为"宿主"/固定一个昵称，不换称呼',
                f'只说16字以内短句，从不解释"为什么"；解释=代价',
                f'绝对禁止替主角做选择，只能报概率和代价，选不选是主角的事',
            ],
        },
    }


def _finalize_step3(parsed: dict, plan: dict, topic: str) -> dict:
    # 保底字段补齐
    parsed.setdefault('world_sketch', '')
    cl = parsed.get('cultivation_levels') or []
    if len(cl) < 9:
        # 补齐到9级
        base = len(cl)
        need = 9 - base
        for i in range(need):
            cl.append({'level': base + i + 1,
                       'name': f'第{base+i+1}阶·未命名',
                       'cap': '解锁前一阶能力×2',
                       'gf_link': '金手指解锁对应子能力'})
    parsed['cultivation_levels'] = cl[:9] if len(cl) > 9 else cl
    cdl = parsed.get('cdl_characters') or {}
    if 'protagonist' not in cdl:
        cdl['protagonist'] = {'name': '主角', 'age': 19, 'identity': '', 'core_wound': '',
                              'want_vs_need': ['', ''], '5_arc_phases': []}
    supp = cdl.get('supports') or []
    if len(supp) < 5:
        for i in range(len(supp), 5):
            supp.append({'name': f'配角{i+1}', 'role_in_story': '待定', 'core_link_to_protagonist': '',
                         'shadow_side': '', 'cdl_profile': ''})
    cdl['supports'] = supp[:5]
    parsed['cdl_characters'] = cdl
    parsed.setdefault('heroine_arc', {'name': '女主', 'first_meet_scene': '', 'temperature_phases': []})
    parsed.setdefault('golden_finger_cost_design', {'usage_costs': [], 'backfire_examples': [], 'hard_constraint': ''})
    sp = parsed.get('system_personality') or {}
    if 'template_name' not in sp:
        sp['template_name'] = SYSTEM_PERSONALITY_TEMPLATES[0]
    parsed['system_personality'] = sp
    parsed['_plan'] = plan
    parsed['_topic'] = topic
    return parsed


def render_step3_text(r: dict) -> str:
    lines = [f'【Step3 · 世界观与角色构建起步包 | 方案：{(r.get("_plan") or {}).get("title","")}】', '']
    lines += ['一、世界观速写（≤800字）', '—' * 40, r.get('world_sketch') or '', '']
    lines += ['二、修炼体系（≥9级）+ 金手指联动', '—' * 40]
    for lv in (r.get('cultivation_levels') or []):
        lines.append(
            f"· Lv.{lv.get('level')} {lv.get('name')}：{lv.get('cap')} | 金手指联动：{lv.get('gf_link')}"
        )
    lines += ['', '三、CDL角色档案（主角 + 前5配角）', '—' * 40]
    cdl = r.get('cdl_characters') or {}
    p = cdl.get('protagonist') or {}
    lines.append(f'【主角】姓名：{p.get("name")} / 年龄：{p.get("age")} / 身份：{p.get("identity")}')
    lines.append(f'  · 核心创伤：{p.get("core_wound")}')
    wa = p.get('want_vs_need') or ['', '']
    lines.append(f'  · WANT vs NEED：[{wa[0]}] ←→ [{wa[1]}]')
    lines.append(f'  · 五阶段弧光：{" → ".join(p.get("5_arc_phases") or [])}')
    lines.append('')
    for s in (cdl.get('supports') or []):
        lines.append(f'【配角·{s.get("role_in_story","")}】{s.get("name")}')
        lines.append(f'  · 和主角的线：{s.get("core_link_to_protagonist")}')
        lines.append(f'  · 隐藏面：{s.get("shadow_side")}')
        lines.append(f'  · CDL档案：{s.get("cdl_profile")}')
        lines.append('')
    lines += ['四、女主角色卡 · 5阶段温度弧线', '—' * 40]
    ha = r.get('heroine_arc') or {}
    lines.append(f'姓名：{ha.get("name")}')
    lines.append(f'第一次相遇：{ha.get("first_meet_scene")}')
    for ph in (ha.get('temperature_phases') or []):
        lines.append(
            f'  · 阶段{ph.get("phase")}｜{ph.get("label")}｜温度{ph.get("temperature")}°｜{ph.get("core_event")}'
        )
    lines += ['', '五、金手指代价/反噬设计', '—' * 40]
    gfc = r.get('golden_finger_cost_design') or {}
    lines.append('使用代价清单：')
    for i, c in enumerate(gfc.get('usage_costs') or [], 1):
        lines.append(f'  {i}. {c}')
    lines.append('反噬剧情示例：')
    for i, c in enumerate(gfc.get('backfire_examples') or [], 1):
        lines.append(f'  {i}. {c}')
    lines.append(f'🚫 硬约束：{gfc.get("hard_constraint")}')
    lines += ['', '六、系统人格化（全书一致模板）', '—' * 40]
    sp = r.get('system_personality') or {}
    lines.append(f'模板：{sp.get("template_name")}')
    lines.append('出场口癖对白：')
    for q in (sp.get('opening_quotes') or []):
        lines.append(f'  “{q}”')
    lines.append('一致性铁律：')
    for rl in (sp.get('consistency_rules') or []):
        lines.append(f'  · {rl}')
    lines.append('\n✋ 上述所有内容若确认，前端可一键转为落地卡片（世界观/人物/大纲/规则维度各一张）入库。')
    return '\n'.join(lines)


# ============================================================================
# 小工具：提取 LLM 输出中的 JSON 代码块
# ============================================================================
def _extract_json_block(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', s)
    if fence:
        candidate = fence.group(1).strip()
    else:
        # 找最外层 { ... }
        a, b = s.find('{'), s.rfind('}')
        if a != -1 and b != -1 and b > a:
            candidate = s[a:b + 1]
        else:
            return None
    try:
        return json.loads(candidate)
    except Exception:
        # 宽松兜底：把 candidate 里的非 JSON 结尾字符清理后再试一次
        cleaned = re.sub(r'[\x00-\x1f\x7f]', '', candidate)
        try:
            return json.loads(cleaned)
        except Exception:
            return None
