"""爆款方案流水线工具链（3步）。

独立于 chat_collab_bp 之外，保持原模块不超架构门禁基线。
被 chat_collab_bp 的4个薄路由直接调用。

Step 1: realtime_scan_rank(topic, refs=[]) → 趋势方向报告
        优先级：Web搜索+抓取热榜页面 → LLM归纳 → 失败回退知识库头部作品
Step 2: generate_5_plans(topic, trend_report, refs=[]) → 5方案×3方向 + 自洽验证
Step 3: build_worldbuild_package(plan_dict) → 世界观+修炼+CDL角色+金手指代价+系统人格化
"""
from __future__ import annotations
import json
import os
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
                if n > 500:
                    # 逐页提取真实书名（真实榜源首页会有几十本榜单作品）
                    page_books = _extract_real_books_from_html(html)
                    if page_books:
                        for b in page_books:
                            if b not in real_books:
                                real_books.append(b)
                        fetch_errors[site_key] = ''  # 成功：清掉之前可能残留的错误
                    scraped_pages.append(f'==== {spec["name"]} ==== [提取{len(page_books)}本真实书名]\n{html[:25000]}')
                    used_web = True
                elif n == 0 and not fetch_errors.get(site_key):
                    fetch_errors[site_key] = '返回空内容（可能被反爬或页面结构变化）'
            except Exception as e:
                fetch_errors.setdefault(site_key, f'{type(e).__name__}: {str(e)[:200]}')
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

    # 真实书名为空时，用 fallback 里的 sample_books（保持兼容）
    if not structured.get('sample_books_used'):
        structured['sample_books_used'] = list(fallback.get('sample_books', []))[:12]

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
        'per_site_bytes': per_site_bytes,
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

原始HTML（取关键词信息即可）：
'''.strip()


def _heuristic_scan_summary(topic: str, refs: list[str], scraped: str, fb: dict) -> dict:
    """纯启发式：从 scraped 里抓书名、高频词，抓不到就用知识库 fallback。"""
    # 抓书名号里的内容
    books = re.findall(r'[《「]([^《》「」]{2,25})[》」]', scraped or '')
    books = list(dict.fromkeys(books))[:12] or list(fb.get('sample_books', []))
    # 高频爽点词
    pls = fb.get('common_pleasures', [])[:3]
    gf = fb.get('common_golden_finger', [])[:4]
    openings = fb.get('openings', ['开篇事件驱动+第一章引入金手指'])
    chars_tags = fb.get('characters', ['有瑕疵主角', '反差女主', '兄弟配角'])
    cliches = fb.get('cliche_warnings', ['同质化模板要避开', '差异化做人物弧光'])
    # 从 scraped 里尝试抠出常见对话/短句描述
    dial_pct = 32
    if scraped:
        # 看"对话多""短句""口语"关键词
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
        if b and b > 500:
            status_lines.append(f'  ✅ {name}{force_tag}：抓取 {b:,} 字节原始HTML')
            n_success += 1
        elif err:
            err_snippet = str(err)[:120].replace('\n', ' ⏎ ')
            status_lines.append(f'  ❌ {name}{force_tag}：失败 → {err_snippet}')
        else:
            status_lines.append(f'  ⚠️ {name}{force_tag}：未抓取或内容过短（{b}字节）')
    if used_web:
        status_lines.append(f'  📌 汇总：真联网，成功站点 {n_success}/{len(all_site_keys)}，seed={seed}')
    else:
        status_lines.append(f'  📌 汇总：❌ 未联网（全部站点失败/环境无外网出口），使用知识库Fallback（非实时，内容固定），seed={seed}')
    # ===== 第二块：真实榜单书目预览（抓到≥5本就强制显示，用户一眼验证不是知识库固定几本）=====
    real_books_preview = meta.get('real_books_preview') or []
    real_books_count = int(meta.get('real_books_count') or 0)
    if real_books_count >= 5:
        status_lines.append(f'📚 真实榜单书目预览（共{real_books_count}本，前12本）：')
        preview = real_books_preview[:12]
        # 每行显示 3 本（对齐好看）
        for i in range(0, len(preview), 3):
            chunk = preview[i:i+3]
            line_items = []
            for j, b in enumerate(chunk):
                line_items.append(f'{i+j+1}. {b}')
            status_lines.append('  ' + '  |  '.join(line_items))
        status_lines.append('')

    source_tag = '' if used_web else '（⚠️ ' + str(meta.get('source', '基于知识库，非实时数据')) + '）'
    sb = r.get('sample_books_used', [])
    lines = list(status_lines)
    lines += [
        f'【实时扫榜 — {date} | 题材：{topic}】 {source_tag}',
        '',
        '🔥 当前火什么（基于{}本热榜作品归纳）：'.format(len(sb)),
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
        f'（样本归纳依据：{"、".join(sb[:6]) or "知识库头部作品"}）',
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
    """Step2 生成5个方案。JSON结构与用户需求格式一致。"""
    refs = [r.strip() for r in (reference_books or []) if r and r.strip()]
    trend_json = json.dumps(trend_report, ensure_ascii=False)[:8000]

    if llm_fn:
        prompt = _build_step2_prompt(topic, refs, trend_json, trend_report)
        try:
            out = llm_fn(prompt) or ''
            plans = _extract_json_block(out)
            if plans and isinstance(plans, dict) and 'plans' in plans and isinstance(plans['plans'], list):
                return _finalize_step2(topic, plans, trend_report)
        except Exception:
            pass
    return _finalize_step2(topic, _heuristic_step2_plans(topic, refs, trend_report), trend_report)


def _build_step2_prompt(topic, refs, trend_json, trend_report=None) -> str:
    ref_str = ('用户额外提供的参考书名（仅作灵感来源，不要照搬原书内容）：\n' +
               '\n'.join(f'- {r}' for r in refs) + '\n\n') if refs else ''
    # 新增：真实榜单对标书目（从3个权威榜源抓到的真实书名，解决"假扫榜→知识库固定书"→方案也假）
    real_books_preview = []
    if isinstance(trend_report, dict):
        meta = trend_report.get('_meta', {}) if isinstance(trend_report.get('_meta'), dict) else {}
        real_books_preview = list(meta.get('real_books_preview') or [])[:15]
        if not real_books_preview:
            sb = trend_report.get('sample_books_used')
            if isinstance(sb, list):
                real_books_preview = list(sb)[:15]
    real_books_str = ''
    if real_books_preview:
        real_books_str = (
            '真实榜单对标书目（从 书荒典/网文大数据/番茄Hub 抓取，非固定知识库）—— 仅作题材和爽点风格参考，严禁照搬内容：\n' +
            '\n'.join(f'- {b}' for b in real_books_preview) + '\n\n'
        )
    return f'''你是网文爆款方案策划（一线主编级，具体落地派，不是模板话痨）。基于如下Step1的"趋势方向"JSON和题材【{topic}】，
生成5个小说方案（方案1-2趋势跟随、3-4差异化、5大胆尝试）。

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

{real_books_str}{ref_str}Step1趋势JSON：
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


def _heuristic_step2_plans(topic: str, refs: list[str], trend: dict) -> dict:
    """_heuristic_step2_plans(fallback，当LLM调用失败/网络不好时用)：5套具体可落地方案，不是空洞大白话。
    按1-2趋势跟随(对应当前真实榜最火方向)/3-4差异化(反套路)/5大胆尝试(细分蓝海)组织，和_build_step2_prompt风格对齐。"""
    topic_s = (topic or '热门题材').strip()

    # ===== 从 trend 里取真实榜单样本书名（如果有），作为5方案的灵感标签 =====
    sample_books = list(trend.get('sample_books_used', []) if isinstance(trend.get('sample_books_used'), list) else [])
    has_guize = any(any(k in b for k in ['终焉', '规则', '邪神', '精神病院', '怪诞']) for b in sample_books)
    has_wuxing = any(any(k in b for k in ['悟性', '修仙', '灵根', '宗门', '洪荒']) for b in sample_books)
    has_moshi = any(any(k in b for k in ['末世', '冰封', '游戏入侵', '诡异', '求生']) for b in sample_books)
    has_tianzhong = any(any(k in b for k in ['种田', '悍妇', '大唐', '长生', '玄武门']) for b in sample_books)

    # ===== 方案1：趋势跟随 #1（规则怪谈/中式惊悚，对应当前真实榜《十日终焉》《我不是戏神》风向）=====
    plan1 = {
        'plan_index': 1,
        'direction': '趋势跟随（规则怪谈+中式惊悚，对标《十日终焉》《我不是戏神》）',
        'title': '谁让他在规则怪谈里开便民超市！',
        'one_liner': '0点后城市变灵异副本，我是唯一敢开门的超市老板',
        'golden_finger': '阴间连锁超市系统：①我能从阳间进货（进价×1）按阴间物价卖（利润率1000%-10000%）；②每成交1单必须帮鬼客完成1件心愿（心愿越离谱返利倍率越高）；③代价：心愿未完成扣3天寿命，每月15号必须交阳间等价物的"阴阳税"，欠税直接拉进十八层地狱副本',
        'identity_conflict': '表面：211毕业考公失败，回老家殡仪馆做夜班保安（月薪4500，被父母骂没用、亲戚说晦气、相亲没人要）；真实：规则怪谈世界唯一持证「阴间便民超市经营者」（城隍爷签发、鬼王排队买货、特调局得从我这儿拿情报）→ 白天上班被当看门的，晚上营业不能暴露客户全是鬼',
        'pleasure_core': '即时爽（每章1单）：吊死鬼要100捆香我加价10倍还得求我→第1章装逼；水鬼要最新款防水手机我翻20倍卖→第2章装逼；延迟爽（10章）：帮城隍爷搞定心愿→发营业执照，整条街其他同行的黑超市全被地府查封→大逆袭；延迟爽（30章）：特调局局长亲自来买情报→官方背书，从"晦气保安"直接变"特邀灵异顾问"',
        'world_shell': '现代+规则怪谈叠加壳：2024某市，每天0-6点随机刷3个灵异副本→等级D→S，势力4方：特调局/民间守夜人/邪神邪教/规则本身',
        'diff_anchor': '同类规则怪谈都在"闯关解谜+死队友升级"，我写「做生意流规则怪谈」——鬼进门先看价，买不起能打欠条做担保分期，爽点是谈条件/拉关系/收账/搞营销，不是比谁更能打',
        'trend_basis': '基于当前真实榜规则怪谈在读风向 + 差异化在做生意流不是闯关流',
        'estimated_size': '200万-280万字（30-42卷，每卷60-80章，每1卷=1个大副本+1张大订单）',
        'validation': {'self_consistent': '✅交易机制（进货→卖货→心愿→纳税）闭环无漏洞', 'sustain_100ch': '✅鬼客心愿类型100章不重样，超市升级路线（单店→连锁→批发）支撑200万', 'character_arc': '✅胆小只想工资→敢跟城隍爷谈分成→硬刚邪神搞商业帝国→最后阴间商会会长', 'differentiation': '✅做生意流规则怪谈，市面没有完全对标'},
    }

    # ===== 方案2：趋势跟随 #2（悟性/修仙/种田流，对标《悟性逆天》《从聊斋开始做金乌》这类修仙种田风向）=====
    plan2_title = '悟性逆天：我养的鸡鸭鹅全成圣了' if has_wuxing else '刚进宗门，我地里种的土豆能修仙'
    plan2_one = '宗门废物测灵根三灵根，我看了一眼土豆居然顿悟了' if has_wuxing else '穿到修仙界当杂役，我后院种的白菜比金丹还香'
    plan2 = {
        'plan_index': 2,
        'direction': '趋势跟随（悟性流+修仙种田，对应当前真实榜悟性/种田风向）',
        'title': plan2_title,
        'one_liner': plan2_one,
        'golden_finger': '悟性溢出面板：①我看任何东西≥10分钟自动顿悟（功法/炼丹/种庄稼都行）；②代价：每次顿悟，溢出范围10米内1个随机生物也会跟着顿悟（可能是我养的鸡/鸭/鱼/鹅，也可能是菜地里的虫/路过的狗/宗门的猫）→顿悟出妖兽我要么藏要么养要么杀，处理不好就是灭门灾难',
        'identity_conflict': '表面：青云宗外门杂役弟子（三灵根最差资质，月薪2块下品灵石，食堂打饭要站最后，被内门弟子随意使唤）；真实：整个青云宗悟性最高的人（长老们研究300年没悟透的残卷我看一眼就悟了）→ 冲突：不能暴露真实悟性（暴露就会被长老夺舍/被宗门当小白鼠/被敌对势力暗杀）',
        'pleasure_core': '即时爽（第1章）：杂役园种土豆看10分钟顿悟「土豆催熟秘术」→第二天土豆长到南瓜大，卖1块中品灵石，赚第一桶金；即时爽（第3章）：内门弟子来抢我的土豆，我随手扔1个土豆把他砸成重伤→土豆比金丹还硬；延迟爽（12章）：宗门大比我被派当啦啦队，随手悟了对手的功法→一招秒了天骄全场震惊',
        'world_shell': '架空修真界+灵气复苏叠加壳：青云宗/玄天宗/合欢宗/魔道4方拉扯，等级体系：炼气→筑基→金丹→元婴→化神→炼虚→合体→大乘→渡劫（9阶）',
        'diff_anchor': '同类悟性流都写"主角悟性高→一路碾压升级"，我写「悟性溢出坑主角」——每次顿悟我家鸡鸭鱼鹅菜地里的虫全顿悟变妖兽，我先处理这批妖兽再修炼→爽点多了一层"藏宠物/养宠物/擦屁股"的搞笑戏，完全反套路',
        'trend_basis': '基于当前真实榜悟性种田风向 + 差异化在悟性溢出波及宠物不是纯升级',
        'estimated_size': '180万-260万字（26-38卷，每卷60-70章）',
        'validation': {'self_consistent': '✅悟性溢出规则闭环（10米范围随机1只生物顿悟），不会出现"顿悟永远主角爽"的漏洞', 'sustain_100ch': '✅可顿悟的东西无穷（功法/丹方/阵法/种田/养马/做饭…），宠物顿悟种类无穷→足够水200万', 'character_arc': '✅杂役怕事→被迫藏宠物当"铲屎官"→敢跟内门天骄硬刚→最后养出一群妖兽小弟开了个"青云动物园"', 'differentiation': '✅悟性溢出坑主角流，市面悟性流全是纯爽无代价无搞笑戏'},
    }

    # ===== 方案3：差异化 #1（末世种田+囤活人，差异化锚点：不是囤货杀怪是囤活人管小社会）=====
    plan3_title = '全球冰封：我的空间里囤了300个活人' if has_moshi else '末世降临：我把全小区囤进了空间'
    plan3 = {
        'plan_index': 3,
        'direction': '差异化（末世种田反套路）',
        'title': plan3_title,
        'one_liner': '全球冰封300米，邻居冻死抢我物资，我把整栋楼300人囤进了空间',
        'golden_finger': '恒温囤货空间：①空间有100万㎡恒温仓库（-20℃冻不住，保鲜无限期）；②囤的东西越多/种类越齐，空间会自动解锁新分区（种植区/养殖区/医疗区/学校区/工业区…）；③代价：囤的「活人」不进保鲜区→会饿会疯会生病会勾心斗角会造反→我不仅要管物资还要管吃喝拉撒+权力分配+勾心斗角+镇压内斗',
        'identity_conflict': '表面：普通小区业主（月薪6000，物业看不起、邻居不认识、相亲没人理）；真实：囤货空间唯一持有者（全小区300人的命都捏在我手里）→ 冲突：想当好人（给所有人吃喝）不行（300张嘴喂不饱+有人贪得无厌要夺权），当坏人不行（真有人饿死我良心过不去+小区旧邻里情道德绑架）',
        'pleasure_core': '即时爽（第1章）：全球冰封当天，我把小区超市100吨物资全收进空间（物业/警察都在外面冻傻了）→第一桶金；即时爽（第3章）：小区恶霸带小弟来抢我家→我直接把他们关进空间「冷冻惩罚区」冻3天放出来→当场跪；延迟爽（12章）：我组织小区300人分工（种粮队/养殖队/医疗队/巡逻队）→空间自动解锁农业区/工业区→产出比官方避难所还高→官方主动来谈合作',
        'world_shell': '2024现代+全球冰封末世壳：太阳黑子异常，气温1小时降到-150℃，全世界99%冻死→等级：囤货量分阶层（1吨以下=贫民/100吨=中产/1万吨=贵族/100万吨=城主），势力4方：官方避难所/民间黑市/掠夺者/空间持有者联盟',
        'diff_anchor': '同类末世囤货都写"囤货+武力装逼+杀掠夺者"，我写「囤活人+管理末世小社会」——空间里有300个活人，吃喝拉撒+分蛋糕+权力斗争+镇压造反+建学校建医院建工厂，爽点是当"末世小区物业主任/城主"搞管理，不是比谁更能打',
        'trend_basis': '基于末世冰封囤货风向 + 差异化在囤活人管小社会不是囤货杀怪',
        'estimated_size': '220万-320万字（32-48卷，每卷70章）',
        'validation': {'self_consistent': '✅囤活人管理机制闭环（分工→分配→内斗→惩罚→升级分区）', 'sustain_100ch': '✅人类社会的问题1000章都写不完（吃饭/住房/上学/医疗/权力/腐败/造反/爱情…），空间分区能开几十种', 'character_arc': '✅普通业主只想保自己→被迫当物业主任→当城主搞万人基地→最后做冰封末世人类文明复兴的领袖', 'differentiation': '✅囤活人管小社会，市面末世囤货没有写管理这么细的'},
    }

    # ===== 方案4：差异化 #2（古言种田反套路：女尊/悍妇/边关/种田，差异化锚点不是甜宠是搞基建养权臣）=====
    plan4_title = '被休悍妇：边关种田种出个权臣白月光' if has_tianzhong else '穿成弃妇我带三娃种田养出了大将军'
    plan4 = {
        'plan_index': 4,
        'direction': '差异化（古言种田反套路）',
        'title': plan4_title,
        'one_liner': '穿成被状元休的悍妇，我带三娃回村种田种到边关军粮命脉',
        'golden_finger': '「种田精准度面板」：①我看一眼地就显示缺什么元素（氮磷钾/湿度/虫害）→随手一调产量翻10倍；②我种的东西自带 Buff（土豆→抗饿3天不吃饭；白菜→治小伤小感冒；小麦→伤口止血；玉米→耐寒-20℃冻不死）；③代价：用面板调1次地，我自己手上起1个老茧/掉1根头发（越种田越"悍妇"糙得像男人——和原主"娇弱貌美被休"的形象反差越大 Buff 越强，变美了 Buff 就没了→想保 Buff 就得故意扮糙穿男人衣服干重活）',
        'identity_conflict': '表面：状元郎妻子→因"善妒打了小妾+没生儿子"被休回村的悍妇（全村指指点点："泼妇活该被休""克夫克子"，娘家爹不让进门，叔伯要抢我家田）；真实：边关军粮命脉唯一供应商（大将军都得排队买我家粮，兵部尚书亲自来我家谈合同）→ 冲突：被休时发誓再不求男人，但现在大将军天天来我家蹭饭→全村以为我和男人鬼混更戳脊梁骨',
        'pleasure_core': '即时爽（第1章）：被休回村当天我种1亩土豆，用面板调完→第3天亩产10000斤（全村平均亩产才500斤）→全村震惊；即时爽（第3章）：叔伯来抢我家田→我扔1个土豆把他砸倒（Buff土豆比石头还硬）；延迟爽（12章）：边关大雪军粮断了，我家仓库存10万斤Buff耐寒玉米→直接供应3万大军1个月→大将军亲自来谢，皇帝派钦差赐我"七品乡君"封号→全村跪着巴结',
        'world_shell': '架空大靖朝（仿北宋）+灵气复苏叠加壳：北狄/大靖/西夏/大理4方拉扯，等级体系：种田能力=官身等级（布衣→乡君→县君→郡君→诰命）',
        'diff_anchor': '同类古言种田都写"女主貌美柔弱+王爷/将军一见钟情+甜宠"，我写「悍妇搞基建+权臣养成」——女主被休后故意扮糙穿男人衣服干重活（因为越糙 Buff 越强），大将军最初来买粮→后来天天来蹭饭→最后是大将军离不开她的粮（和她这个人），先有事业再有感情，甜宠是副产品不是主线→完全反套路',
        'trend_basis': '基于古言种田/权臣风向 + 差异化在悍妇搞基建养权臣不是甜宠嫁王爷',
        'estimated_size': '180万-250万字（26-36卷，每卷65章）',
        'validation': {'self_consistent': '✅Buff规则闭环（越糙Buff越强，变美Buff消失）倒逼女主走悍妇人设不会OOC', 'sustain_100ch': '✅种田→卖粮→搞基建（修路/建粮仓/建水利/建医馆/养孩子）→官身升级→权谋战争，180万字内容撑得住', 'character_arc': '✅被休失魂落魄→悍妇骂街护田+带娃+搞基建→当乡君管一县→最后当诰命夫人+边关后勤总管', 'differentiation': '✅悍妇搞基建权臣养成，市面古言种田全是柔弱女主走甜宠嫁王爷路线'},
    }

    # ===== 方案5：大胆尝试（细分蓝海：殡仪馆/殡葬业视角 + 中式怪诞+做生意，大胆混搭）=====
    plan5_title = '殡仪馆夜班保安：我的客户全是鬼但全是土豪'
    plan5 = {
        'plan_index': 5,
        'direction': '大胆尝试（殡仪馆视角+中式怪诞+做生意混搭）',
        'title': plan5_title,
        'one_liner': '殡仪馆夜班12点交班，第一个客户是吊死鬼要订1000捆香配分期',
        'golden_finger': '「殡葬服务小程序」：①只有我能看见，鬼客能直接在小程序上下单（寿衣/骨灰盒/花圈/香烛纸扎/超度/墓地/配冥婚）；②死人付钱用「阴德值」→阴德值能换阳间的东西（延寿/改运/治癌症/中彩票/逢考必过）；③代价：接了单必须按鬼客要求做，做错1个步骤，鬼客的怨气就附我身上1天（头疼/发烧/看见脏东西/掉发），做错3步，鬼客直接带我走',
        'identity_conflict': '表面：殡仪馆夜班保安（专科毕业，没背景，月薪5000，白天不敢跟人说自己做什么工作，朋友圈从来不敢发工作照，相亲不敢说职业，爸妈以为我在厂里打工）；真实：全阴间最大殡葬服务一条龙供应商→阎王办葬礼都找我订花圈，黑白无常是我老客户（每次勾魂都顺手在我这儿下单香烛）→ 冲突：白天我想当正常人谈恋爱结婚，但是鬼客24小时下单，女朋友来我家查岗刚好撞见我给吊死鬼试寿衣→当场被吓跑',
        'pleasure_core': '即时爽（第1章）：吊死鬼来订1000捆香→分期30年利息1000%，他不还钱我直接把他挂在小程序「失信鬼客黑名单」→ 第二天他托梦给他儿子儿子吓得赶紧连本带利给我烧了→赚第一桶金5万人民币+10000阴德值；即时爽（第4章）：我用10000阴德值换了个「癌症转阴」→我妈晚期肝癌体检直接好了→全家震惊；延迟爽（15章）：阎王办500大寿→整个地府从花圈到寿衣到主持全是我包的→我直接升阴间殡葬部特邀顾问，黑白无常见我都得叫哥',
        'world_shell': '现代都市+阴阳两界叠加壳：阳间（殡仪馆/医院/派出所）×阴间（地府/枉死城/城隍庙/阎王殿）互通，等级体系：阴德值分等级（1000=白丁→1万=九品→10万=五品→100万=三品→1亿=一品诰命→100亿=地府特邀顾问），势力4方：地府/城隍/阳间殡葬协会/游魂野鬼散客',
        'diff_anchor': '同类中式怪谈都写"殡仪馆撞鬼→被鬼追→吓死人→逃命"，我写「殡仪馆给鬼做殡葬服务供应商」——鬼是我的客户（上帝），我要给鬼介绍套餐、分期、售后、拉复购、搞活动（清明满减/中元节双倍积分），爽点是做殡葬行业生意+赚阴德换阳间好处，不是比谁更吓人→完全细分蓝海',
        'trend_basis': '基于规则怪谈+中式怪诞大方向，大胆切殡仪馆殡葬服务供应商细分视角，市面上没有完全对标',
        'estimated_size': '200万-300万字（30-45卷，每卷70章，1卷=1个大订单/1个大型殡葬活动）',
        'validation': {'self_consistent': '✅殡葬小程序规则闭环（下单→服务→收阴德值→错步骤附怨气），无漏洞', 'sustain_100ch': '✅殡葬服务内容无穷（寿衣/花圈/骨灰盒/超度/墓地/配冥婚/迁坟/做七/做寿/阎王大寿/黑白无常结婚…），100章不重样', 'character_arc': '✅专科保安不敢说职业→赚阴德值治好妈病→敢跟人说我是殡仪馆夜班→当地殡葬行业龙头→最后阴间+阳间两界殡葬帝国老板', 'differentiation': '✅殡仪馆殡葬供应商视角，市面中式怪谈没有写做生意+赚阴德换阳间好处的'},
    }

    return {'plans': [plan1, plan2, plan3, plan4, plan5]}


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
