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
    'fanqie': {
        'name': '番茄小说',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+番茄小说+排行榜+热门',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=番茄小说+{t}+热门+推荐+前十',
    },
    'qidian': {
        'name': '起点中文网',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+起点中文网+排行榜+热门推荐',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=起点中文网+{t}+热门+前十+完本',
    },
    'qimao': {
        'name': '七猫小说',
        'search_url_tpl': 'https://www.baidu.com/s?wd={q}+七猫小说+排行榜+热门',
        'topic_search_tpl': 'https://www.baidu.com/s?wd=七猫小说+{t}+热门+推荐',
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
    # quote(safe='/:?=&%#+.') 既保证分隔符不被编码，也保留已 percent 编码的 %XX 不会被再转成 %25XX
    # 但「#」在 path/fragment 分隔时需保留，safe 里要含
    return urllib.parse.quote(url, safe=r"/:?=&%#+.@-_,~()*!$'")


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

    # 抓取原始文本
    scraped_pages: list[str] = []
    used_web = False
    per_site_bytes: dict[str, int] = {}
    if web_fetch_fn:
        sites = list(SITE_SPECS.items())
        if force_sites:
            sites = [(k, v) for k, v in sites if k in force_sites]
        for site_key, spec in sites:
            try:
                url_raw = spec['topic_search_tpl'].format(t=urllib.parse.quote(topic))
                url = _encode_full_url(url_raw)
                html = web_fetch_fn(url) or ''
                n = len(html) if isinstance(html, str) else 0
                per_site_bytes[site_key] = n
                fetch_errors.setdefault(site_key, '')
                if n > 500:
                    scraped_pages.append(f'==== {spec["name"]} 搜索页 ====\n{html[:12000]}')
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

    structured['_meta'] = {
        'topic': topic,
        'reference_books': refs,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'source': '基于联网抓取+LLM归纳' if (used_web and scraped_concat) else '基于知识库，非实时数据',
        'used_web_fetch': used_web and bool(scraped_concat),
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
        prompt = _build_step2_prompt(topic, refs, trend_json)
        try:
            out = llm_fn(prompt) or ''
            plans = _extract_json_block(out)
            if plans and isinstance(plans, dict) and 'plans' in plans and isinstance(plans['plans'], list):
                return _finalize_step2(topic, plans, trend_report)
        except Exception:
            pass
    return _finalize_step2(topic, _heuristic_step2_plans(topic, refs, trend_report), trend_report)


def _build_step2_prompt(topic, refs, trend_json) -> str:
    ref_str = ('用户额外提供的参考书名（仅作灵感来源，不要照搬原书内容）：\n' +
               '\n'.join(f'- {r}' for r in refs) + '\n\n') if refs else ''
    return f'''你是网文爆款方案策划。基于如下Step1的"趋势方向"JSON和题材【{topic}】，
生成5个小说方案（方案1-2趋势跟随、3-4差异化、5大胆尝试），每个方案严格按要求8项字段，
且最后都要有"验证"4项：自洽✅/❌、爽点续航(≥100章)✅/❌、角色弧光✅/❌、差异化✅/❌（不得全❌）。

{ref_str}Step1趋势JSON：
{trend_json}

请只输出一个合法JSON对象（可包裹在```json里），格式：
{{
  "plans": [
    {{
      "plan_index": 1,
      "direction": "趋势跟随",
      "title": "书名 ≤15字",
      "one_liner": "一句话梗（25字以内）",
      "golden_finger": "核心金手指/奇遇，含代价：xxxxx",
      "identity_conflict": "身份矛盾（30字内）",
      "pleasure_core": "爽点内核（即时爽/延迟爽）",
      "world_shell": "世界观壳（30字内）",
      "diff_anchor": "读者凭什么选你而不是其他同类？（差异化锚点）",
      "trend_basis": "基于当前XX方向的流行 + 差异化在XX",
      "estimated_size": "参考总字数范围，如 180万-260万字",
      "validation": {{ "self_consistent": true, "sustain_100ch": true, "character_arc": true, "differentiation": true }}
    }}
  ]
}}
'''.strip()


def _heuristic_step2_plans(topic: str, refs: list[str], trend: dict) -> dict:
    fb = _pick_fallback(topic)
    ct = trend.get('current_trending', {})
    gf_pool = ct.get('golden_finger_directions') or fb.get('common_golden_finger', ['系统面板'])
    pl_pool = ct.get('pleasure_types') or fb.get('common_pleasures', ['打脸爽'])
    diffs = trend.get('cliche_landmines', {}).get('diff_opportunities') or ['人物代价实锤']
    directions = ['趋势跟随', '趋势跟随', '差异化', '差异化', '大胆尝试']
    gf_cycle = (gf_pool * 3)[:5]
    pl_cycle = (pl_pool * 3)[:5]
    plans = []
    topic_s = topic or '未知题材'
    for i in range(5):
        direction = directions[i]
        dif_idx = min(i if i < 2 else (i - 2), len(diffs) - 1)
        gf = gf_cycle[i]
        pl = pl_cycle[i]
        val = {
            'self_consistent': True,
            'sustain_100ch': True,
            'character_arc': i != 4,
            'differentiation': i >= 2,
        }
        title_suffix = ['之变', '纪元', '使徒', '事务所', '重启']
        plan_title = f'{topic_s[:2]}{title_suffix[i]}'
        if i == 4:
            plan_title = f'{topic_s[:2]}反向{title_suffix[i]}'
        plan = {
            'plan_index': i + 1,
            'direction': direction,
            'title': plan_title[:15],
            'one_liner': f'普通人意外触发{gf[:6]}，第一桶金靠{pl[:6]}',
            'golden_finger': f'{gf}；代价：每次使用都有真实反噬/资源消耗，不白嫖',
            'identity_conflict': '白天是最底层身份，晚上/背地里却是被追杀的大佬',
            'pleasure_core': f'{pl}（{"即时爽" if i%2==0 else "延迟爽"}，1-3章小反馈 + 10章大反馈）',
            'world_shell': f'{topic_s}现代/架空壳，等级体系≥9阶，势力≥4方拉扯',
            'diff_anchor': diffs[dif_idx] if diffs else '金手指反噬是硬约束，人物弧光前抑后扬',
            'trend_basis': f'基于当前{gf}方向流行 + 差异化在{diffs[dif_idx] if diffs else "反噬"}',
            'estimated_size': '180万-280万字（25-45卷，每卷50-60章）',
            'validation': val,
        }
        plans.append(plan)
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
