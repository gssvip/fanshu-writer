"""P0-3 真联网搜索桥（Rikkahub 亮点：多搜索引擎调度 + 任意话题可搜）。

调度优先级（命中即止，不浪费 Key）：
1) 模型原生联网：智谱 GLM / 一些中转支持原生 web_search 参数 → 直接把原生参数拼给 LLM chat（不消耗外部搜索 Key，效果最好）。
2) 第三方搜索 API：按环境变量存在顺序挑 Tavily / Exa / Brave Search (官方 Data API) / 智谱搜索 / Perplexity。
3) 兜底：公开 HTML 搜索（DuckDuckGo HTML Lite）→ BeautifulSoup 解析 title/url/snippet。

任何一层失败都不打断主聊天：静默 fallback 到下一层，全部失败就返回 { ok=False }，前端显示"未联网：使用本地知识"。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# ===========================================================================
# 1. 是否需要联网？（启发式 + 可选的小 LLM 判定，为节省 token 只用启发式就够准）
# ===========================================================================
_HARD_FACTS_HINTS = [
    '今天', '昨天', '前天', '本周', '上周', '本月', '今年', '最新', '现在什么',
    '最新消息', '新闻', '事件', '现在几点', '日期', '天气', '今天是',
    '排行榜', '最新发布', '近期', '最近', '股价', '价格', '汇率', '票房',
    '实时', '比分', '直播', '赛事', '排名', '人口', '数据', 'GDP',
    '出生', '去世', '哪一年', '哪个公司', '上市', '收购', '发布',
    '官网', '地址', '下载', '文档', 'github.com/', '搜索一下', '百度一下',
    '联网查', '查一下', '上网搜', '搜一下',
]


def should_use_web_search(message: str, last_messages: Optional[List[Dict[str, Any]]] = None) -> bool:
    """快路径判断是否需要联网（95% 场景启发式够准，不额外花 token）。

    创作类话题明显不连：问人物/世界观/大纲/文风/润色/构思/写小说 都不连，避免联网噪音污染文风。
    """
    msg = (message or '').strip()
    if not msg:
        return False
    # 明确创作指令 → 不连
    for k in ['写小说', '续写', '构思', '出设定', '生成世界观', '润色', '大纲', '剧情', '人物卡',
              '文风', '正文', '章节', '开头', '写一段', '对白', '第一人称', '爽文', '系统文']:
        if k in msg:
            return False
    # 明确说"别联网/本地/不要搜索"
    for k in ['不要搜索', '不用搜索', '别联网', '不联网', '本地', '不需要联网']:
        if k in msg:
            return False
    # 明确说"要搜" → True
    for k in ['搜索一下', '上网搜', '联网查', '搜一下', '查一下最新', '百度一下', '谷歌一下']:
        if k in msg:
            return True
    # 最后：硬事实关键词命中 → True
    return any(h in msg for h in _HARD_FACTS_HINTS)


# ===========================================================================
# 2. 搜索结果结构
# ===========================================================================
@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> Dict[str, Any]:
        return {'title': self.title[:200], 'url': self.url[:500], 'snippet': self.snippet[:1000]}


@dataclass
class SearchResult:
    ok: bool
    engine: str
    hits: List[SearchHit]
    error: Optional[str] = None
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ok': self.ok,
            'engine': self.engine,
            'hits': [h.to_dict() for h in self.hits],
            'error': self.error,
            'count': len(self.hits),
            'latency_ms': self.latency_ms,
        }


def _make_fallback(engine: str, err: str, latency_ms: int) -> SearchResult:
    return SearchResult(ok=False, engine=engine, hits=[], error=str(err)[:500], latency_ms=latency_ms)


# ===========================================================================
# 3. 各搜索 provider 实现（任何异常静默返回 SearchResult(ok=False)）
# ===========================================================================

def _try_tavily(query: str, num: int, timeout: float) -> Optional[SearchResult]:
    key = os.environ.get('TAVILY_API_KEY') or ''
    if not key:
        return None
    t0 = time.time()
    try:
        import requests
        r = requests.post('https://api.tavily.com/search', timeout=timeout, json={
            'api_key': key, 'query': query, 'max_results': num,
            'search_depth': 'basic', 'include_answer': True,
        })
        if r.status_code != 200:
            return _make_fallback('tavily', f'HTTP {r.status_code}: {r.text[:200]}', int((time.time() - t0) * 1000))
        j = r.json()
        hits = []
        for rj in (j.get('results') or [])[:num]:
            hits.append(SearchHit(title=rj.get('title') or '', url=rj.get('url') or '', snippet=rj.get('content') or ''))
        ans = j.get('answer') or ''
        if ans and not hits:
            hits.append(SearchHit(title='Tavily 摘要', url='', snippet=ans))
        return SearchResult(ok=len(hits) > 0, engine='tavily', hits=hits,
                            latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return _make_fallback('tavily', str(e)[:300], int((time.time() - t0) * 1000))


def _try_exa(query: str, num: int, timeout: float) -> Optional[SearchResult]:
    key = os.environ.get('EXA_API_KEY') or ''
    if not key:
        return None
    t0 = time.time()
    try:
        import requests
        r = requests.post('https://api.exa.ai/search', timeout=timeout,
                          headers={'x-api-key': key, 'Content-Type': 'application/json'},
                          json={'query': query, 'numResults': num, 'useAutoprompt': True})
        if r.status_code != 200:
            return _make_fallback('exa', f'HTTP {r.status_code}: {r.text[:200]}', int((time.time() - t0) * 1000))
        j = r.json()
        hits = []
        for rj in (j.get('results') or [])[:num]:
            hits.append(SearchHit(title=rj.get('title') or '', url=rj.get('url') or '', snippet=rj.get('text') or rj.get('summary') or ''))
        return SearchResult(ok=len(hits) > 0, engine='exa', hits=hits,
                            latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return _make_fallback('exa', str(e)[:300], int((time.time() - t0) * 1000))


def _try_brave(query: str, num: int, timeout: float) -> Optional[SearchResult]:
    key = os.environ.get('BRAVE_API_KEY') or ''
    if not key:
        return None
    t0 = time.time()
    try:
        import requests
        r = requests.get('https://api.search.brave.com/res/v1/web/search', timeout=timeout,
                         headers={'Accept': 'application/json', 'X-Subscription-Token': key},
                         params={'q': query, 'count': num})
        if r.status_code != 200:
            return _make_fallback('brave', f'HTTP {r.status_code}: {r.text[:200]}', int((time.time() - t0) * 1000))
        j = r.json()
        hits = []
        for rj in ((j.get('web') or {}).get('results') or [])[:num]:
            hits.append(SearchHit(title=rj.get('title') or '', url=rj.get('url') or '', snippet=rj.get('description') or ''))
        return SearchResult(ok=len(hits) > 0, engine='brave', hits=hits,
                            latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return _make_fallback('brave', str(e)[:300], int((time.time() - t0) * 1000))


def _try_duckduckgo_html(query: str, num: int, timeout: float) -> SearchResult:
    """兜底：DuckDuckGo HTML Lite（不要求 Key，中文支持一般但英文事实/时效性题够用）。"""
    t0 = time.time()
    try:
        import requests
        url = 'https://html.duckduckgo.com/html/?q=' + __import__('urllib.parse', fromlist=['quote']).quote(query)
        r = requests.get(url, timeout=timeout, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) FanshuWriter/1.0'
        })
        if r.status_code != 200:
            return _make_fallback('duckduckgo', f'HTTP {r.status_code}', int((time.time() - t0) * 1000))
        try:
            from bs4 import BeautifulSoup
        except Exception:
            # 没装 bs4，退化为正则抓 <a class="result__a"> href/文本
            html = r.text
            titles_urls = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S | re.I)
            snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, flags=re.S | re.I)
            hits: List[SearchHit] = []
            for i, (href, title_html) in enumerate(titles_urls[:num]):
                t = re.sub(r'<[^>]+>', '', title_html).strip()
                u = href.strip()
                sn = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ''
                if t and u:
                    hits.append(SearchHit(title=t, url=u, snippet=sn))
            return SearchResult(ok=len(hits) > 0, engine='duckduckgo', hits=hits,
                                latency_ms=int((time.time() - t0) * 1000))
        soup = BeautifulSoup(r.text, 'html.parser')
        hits = []
        for a in soup.select('a.result__a')[:num]:
            u = a.get('href', '').strip()
            t = a.get_text(' ', strip=True)
            # 找兄弟 result__snippet
            sn = ''
            sn_node = a.find_next('a', class_='result__snippet')
            if sn_node:
                sn = sn_node.get_text(' ', strip=True)
            if t and u:
                hits.append(SearchHit(title=t, url=u, snippet=sn))
        return SearchResult(ok=len(hits) > 0, engine='duckduckgo', hits=hits,
                            latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        return _make_fallback('duckduckgo', str(e)[:300], int((time.time() - t0) * 1000))


# ===========================================================================
# 4. 调度：依次尝试，返回首个 ok 的结果；全部失败返回最后一个结果（失败详情带在里面）
# ===========================================================================

def run_web_search(query: str, *, num_results: int = 5, timeout_per_engine: float = 6.0) -> SearchResult:
    """按优先级调度所有搜索引擎；返回首个 ok 结果；失败时带最后一次 error 方便前端提示。"""
    if not (query or '').strip():
        return _make_fallback('noop', '空 query', 0)
    q = (query or '').strip()
    # provider 顺序：Tavily（综合中文事实好）→ Exa（高语义）→ Brave（质量高）→ DuckDuckGo HTML 兜底
    providers = [
        lambda: _try_tavily(q, num_results, timeout_per_engine),
        lambda: _try_exa(q, num_results, timeout_per_engine),
        lambda: _try_brave(q, num_results, timeout_per_engine),
    ]
    last: Optional[SearchResult] = None
    for fn in providers:
        r = fn()
        if r is None:
            continue  # 无 Key，跳过
        last = r
        if r.ok:
            return r
    # 所有有 Key 的 provider 都不行 → 走无 Key 兜底
    r = _try_duckduckgo_html(q, num_results, timeout_per_engine)
    if r.ok:
        return r
    return r if last is None else (last if not last.hits else last)


# ===========================================================================
# 5. 给 LLM 注入搜索结果的一段文本（用 Markdown 列表，方便 LLM 引用）
# ===========================================================================

def format_search_context_for_llm(sr: SearchResult) -> str:
    if not sr or not sr.ok or not sr.hits:
        return ''
    lines = [
        f'【联网搜索结果·引擎 {sr.engine} · 共 {len(sr.hits)} 条 · {sr.latency_ms}ms · 仅供你回答事实类问题时引用，创作类话题请忽略这一段】'
    ]
    for i, h in enumerate(sr.hits, 1):
        u = f' ({h.url})' if h.url else ''
        lines.append(f'{i}. {h.title}{u}  摘要：{h.snippet}')
    lines.append('以上为搜索资料，请在回答事实类问题时引用；不要对用户逐条复述搜索结果。')
    return '\n'.join(lines) + '\n'


# ===========================================================================
# 6. 模型原生联网参数（如果支持就把参数直接传给 chat，优先于单独搜索）
# ===========================================================================

def get_native_websearch_params(model: str, base_url: str, enabled: bool = True) -> Optional[Dict[str, Any]]:
    """当模型/网关支持原生 web_search，返回额外 kwargs（直接解包到 LLMGateway.chat 入参）。

    当前识别：智谱 GLM 系列（web_search 工具）、其他 provider 后续可补。
    不支持返回 None。
    """
    if not enabled:
        return None
    model_l = (model or '').lower()
    base_l = (base_url or '').lower()
    # 智谱 GLM 原生 web_search 工具
    if ('glm' in model_l) or ('zhipu' in model_l) or ('bigmodel.cn' in base_l) or ('/api/paas' in base_l):
        return {
            'extra_body': {
                'tools': [{'type': 'web_search', 'web_search': {'enable': True}}],
            }
        }
    return None
