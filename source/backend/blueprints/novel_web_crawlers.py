# -*- coding: utf-8 -*-
"""免费小说站正文抓取（顶点小说 / 笔趣阁 lewen 模板系）。

用途：榜单「前 30 章深拆」时，番茄直抓受风控/付费墙或起点等平台不支持直抓的
兜底路线——按【书名】（可选【作者名】）在免费小说站搜索 → 选最佳匹配 →
抓书籍目录前 N 章正文，返回结构与 novel_rank_crawlers.fetch_rank_book_content
保持一致，前端无感复用。

2026-10 实测（沙盒出口）：
  - 顶点小说网 https://www.23wx.la ：搜索 POST /search.html 字段 s（UTF-8 表单
    即可，页面 meta 虽声明 gbk 实际回 UTF-8）；书目页 /book/<bid>/；章节正文做了
    base64 内嵌混淆（mz('...') / wzrb.amz('...')，解码后是 <p> 段落），长章按
    _1/_2 分页；
  - 笔趣阁系镜像（xbiqugew.com 等）：书页/章节公开可读，但搜索强制登录，无法按
    书名检索，暂不纳入；镜像死亡频繁，站点全部走 NOVEL_MIRRORS 配置顺序尝试，
    后续有可用镜像只需追加配置。

合规：仅抓公开可读页面用于拆书分析（与既有番茄 SSR 抓取同性质），带浏览器 UA、
单请求超时、低并发 + 礼貌限速，失败只跳过不硬刚。
"""
from __future__ import annotations

import base64
import binascii
import html as _html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin

import requests

# 镜像按优先级排序：同一本书依次尝试，首个达到匹配阈值的镜像胜出。
# lewen 模板镜像（顶点系）页面结构一致，换新域名只需在此追加。
NOVEL_MIRRORS: list[dict[str, str]] = [
    {'name': '顶点小说网', 'base': 'https://www.23wx.la'},
]

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
_TIMEOUT = 12
_MAX_SUBPAGES = 15      # 单章分页上限（异常链接环保护）
_MIN_CHAPTER_CHARS = 150


# ---------------------------------------------------------------------------
# 底层 HTTP
# ---------------------------------------------------------------------------
def _decode_bytes(raw: bytes) -> str:
    """页面编码混乱（meta 写 gbk 实际 utf-8）：先 utf-8 严格解，失败再 gbk。"""
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('gbk', errors='replace')


def _http_get(url: str, *, referer: str | None = None, retries: int = 2,
              timeout: int = _TIMEOUT) -> str:
    headers = {
        'User-Agent': _UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.content:
                return _decode_bytes(r.content)
            last_exc = RuntimeError(f'HTTP {r.status_code}')
        except requests.RequestException as e:
            last_exc = e
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(str(last_exc) or '请求失败')


def _http_post_form(url: str, fields: dict[str, str], *, gbk: bool = False) -> str:
    headers = {
        'User-Agent': _UA,
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cache-Control': 'no-cache',
    }
    if gbk:
        # 老模板搜索按 GBK 收 searchkey：手工百分号编码，避免 requests 默认 UTF-8
        from urllib.parse import quote
        body = '&'.join(f'{k}={quote(str(v).encode("gbk"))}' for k, v in fields.items())
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        r = requests.post(url, data=body.encode('ascii'), headers=headers,
                          timeout=_TIMEOUT, allow_redirects=True)
    else:
        r = requests.post(url, data=fields, headers=headers,
                          timeout=_TIMEOUT, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code}')
    return _decode_bytes(r.content)


# ---------------------------------------------------------------------------
# 书名匹配
# ---------------------------------------------------------------------------
def _norm_title(t: str | None) -> str:
    """归一化书名用于比对：去《》空格与标点，繁体不做转换（镜像站多为简体）。"""
    if not t:
        return ''
    t = re.sub(r'[《》〈〉\s　·•・,，。.!！?？:：;；、\-—_（）()\[\]【】\'\"“”‘’]+', '', str(t))
    return t.lower().strip()


def _score_title(want: str, got: str) -> int:
    if not want or not got:
        return 0
    if want == got:
        return 100
    if want in got or got in want:
        return 70
    # 去"最新章节/全文免费阅读"等站点尾巴后再比一次
    got2 = re.sub(r'(最新章节|全文.*|免费阅读.*)$', '', got)
    if want == got2:
        return 100
    return 0


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
_BOOK_LINK_RE = re.compile(r'<a[^>]+href="/book/(?P<bid>\d+)/?"[^>]*>(?P<title>.*?)</a>',
                           re.S | re.I)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S | re.I)
_TAG_RE = re.compile(r'<[^>]+>')


def _parse_search_rows(page: str) -> list[dict[str, str]]:
    """解析 lewen 模板搜索结果表：每行 分类 / 书名链接 / 最新章节链接 / 作者 / 日期。"""
    out: list[dict[str, str]] = []
    for row in _ROW_RE.findall(page):
        m = _BOOK_LINK_RE.search(row)
        if not m:
            continue
        bid = m.group('bid')
        title = _html.unescape(_TAG_RE.sub('', m.group('title'))).strip()
        # 作者：含章节链接（/book/<bid>/<cid>.html）的那个 td 之后的第一个纯文本 td
        author = ''
        tds = _TD_RE.findall(row)
        for i, td in enumerate(tds):
            if re.search(rf'/book/{bid}/\d+\.html', td) and i + 1 < len(tds):
                cand = _html.unescape(_TAG_RE.sub('', tds[i + 1])).strip()
                cand = cand.replace('&nbsp;', '').strip()
                # 排除日期形态（02-23 / 2026-10-01）
                if cand and not re.match(r'^[\d\-/: ]+$', cand):
                    author = cand
                break
        if not author:
            # 兜底：行内最后一个非空纯文本短 td
            plains = [_html.unescape(_TAG_RE.sub('', x)).replace('&nbsp;', '').strip()
                      for x in tds if not re.search(r'<a\s', x)]
            plains = [x for x in plains if x and not re.match(r'^[\d\-/: ]+$', x)]
            if plains:
                author = plains[-1]
        if title:
            out.append({'bookId': bid, 'title': title, 'author': author})
    # 同 bid 去重保留分最高行（首页一般无重复，保险）
    seen: dict[str, dict[str, str]] = {}
    for it in out:
        seen.setdefault(it['bookId'], it)
    return list(seen.values())


def search_web_book(title: str, author: str | None = None,
                    mirrors: list[dict[str, str]] | None = None) -> dict[str, Any] | None:
    """按书名(+作者)在镜像列表依次搜索，返回全局最佳匹配。

    返回 {mirror,name,base,bookId,bookUrl,title,author,score,matchLevel}；
    全部镜像无命中返回 None。
    """
    want_t = _norm_title(title)
    want_a = _norm_title(author)
    best: dict[str, Any] | None = None
    for mir in (mirrors or NOVEL_MIRRORS):
        base = mir['base'].rstrip('/')
        page = ''
        for use_gbk in (False, True):  # 先 UTF-8，零结果再按老模板 GBK 重试
            try:
                page = _http_post_form(f'{base}/search.html', {'s': title or ''}, gbk=use_gbk)
            except Exception:
                page = ''
            rows = _parse_search_rows(page) if page else []
            if rows:
                break
            time.sleep(0.3)
        for it in rows:
            score = _score_title(want_t, _norm_title(it['title']))
            if not score:
                continue
            if want_a:
                got_a = _norm_title(it.get('author'))
                if got_a and want_a == got_a:
                    score += 30
                elif got_a and (want_a in got_a or got_a in want_a):
                    score += 15
            cand = {
                **it,
                'mirror': mir, 'name': mir['name'], 'base': base,
                'bookUrl': f'{base}/book/{it["bookId"]}/',
                'score': score,
                'matchLevel': 'exact' if _norm_title(it['title']) == want_t else 'likely',
            }
            if best is None or score > best['score']:
                best = cand
        if best and best['score'] >= 130:  # 书名精确 + 作者吻合，无需再试其他镜像
            break
    return best


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
_CHAPTER_LINK_RE = re.compile(r'<a[^>]+href="/book/(?P<bid>\d+)/(?P<cid>\d+)\.html"[^>]*>'
                              r'(?P<title>[^<]{1,60})</a>', re.I)


def _parse_chapter_links(page: str, bid: str, base: str = '') -> list[dict[str, str]]:
    """从一页 HTML 提取本书章节单元（保序、本页去重）；base 非空时直接产出绝对 URL。"""
    out: list[dict[str, str]] = []
    local_seen: set[str] = set()
    for m in _CHAPTER_LINK_RE.finditer(page):
        if m.group('bid') != bid or m.group('cid') in local_seen:
            continue
        local_seen.add(m.group('cid'))
        t = _html.unescape(m.group('title')).strip()
        path = f'/book/{bid}/{m.group("cid")}.html'
        out.append({'title': t, 'cid': m.group('cid'),
                    'url': (base + path) if base else path})
    return out


def fetch_web_chapter_list(book: dict[str, Any]) -> list[dict[str, str]]:
    """抓书籍完整章节目录。

    书目主页含前 100 个阅读单元，其余在 /book/<bid>/index_2.html、index_3…；
    顶点把超长章拆成多个单元（第X章一/二/三…），各单元独立 cid。
    每批 3 页并发、按页序合并，遇到无新增单元的页即认为目录到底（上限 12 页）。
    """
    base = book['base']
    bid = book['bookId']
    first = _http_get(book['bookUrl'], referer=base + '/')
    chapters = _parse_chapter_links(first, bid)
    seen = {c['cid'] for c in chapters}

    def _page(idx: int) -> str | None:
        try:
            return _http_get(f'{base}/book/{bid}/index_{idx}.html',
                             referer=book['bookUrl'], retries=1, timeout=10)
        except Exception:
            return None  # 网络错误与"到底页"区分：不触发提前终止

    idx = 2
    hit_end = False
    with ThreadPoolExecutor(max_workers=3) as pool:
        while idx <= 12 and not hit_end:
            batch = list(range(idx, min(idx + 3, 13)))
            pages = list(pool.map(_page, batch))
            progressed = False
            for html_page in pages:
                if html_page is None:
                    continue  # 该页抓取失败：不合并也不判定到底
                items = _parse_chapter_links(html_page, bid)
                new = [c for c in items if c['cid'] not in seen]
                if not new:
                    # 成功取到页但无新增 → 目录到底：只跳出循环，
                    # 不能直接 return（否则本批后续页被跳过、末尾 URL 归一化不可达）
                    hit_end = True
                    break
                for c in new:
                    seen.add(c['cid'])
                    c['url'] = base + c['url']
                    chapters.append(c)
                progressed = True
            if not progressed:
                break
            idx += 3
    for c in chapters:
        if c['url'].startswith('/'):
            c['url'] = base + c['url']
    return chapters


# ---------------------------------------------------------------------------
# 阅读单元 → 逻辑章归并
# ---------------------------------------------------------------------------
# 顶点会把超长的一章拆成「第X章 标题一 / 标题二 / …」多个独立 cid 阅读单元，
# 用户选"前 30 章"期望的是 30 个逻辑章。归并规则从严，避免误并：
#   1) 后缀只认：一~九单字 / 十~十九 / 阿拉伯数字 1~99（站方偶尔混用"4"）；
#   2) 必须与上一单元【同基标题】且序号严格 +1 连续；
#   3) 分组首单元后缀必须是 1（一），否则不开放合并。
# 反例："…唐三"+"一"虽以数字字结尾，但后续不会出现同基标题的"二/三/四"，
# 自然不会合并；首章"…唐三一/二/三/四"则能正确并为一章。
_PART_SUFFIX_RE = re.compile(r'(?:十[一二三四五六七八九]?|[一二三四五六七八九]|[0-9]{1,2})$')
_CN_DIGIT = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
             '六': 6, '七': 7, '八': 8, '九': 9}
# 30 逻辑章在分拆严重的书里约 110~130 个单元；设硬上限防异常书请求时长失控
_MAX_FETCH_UNITS = 140


def _part_number(suffix: str) -> int | None:
    if suffix.isdigit():
        n = int(suffix)
        return n if 1 <= n <= 99 else None
    if suffix == '十':
        return 10
    if len(suffix) == 2 and suffix[0] == '十':
        return 10 + _CN_DIGIT.get(suffix[1], 0)
    return _CN_DIGIT.get(suffix)


def group_logical_chapters(units: list[dict[str, str]]) -> list[dict[str, Any]]:
    """把站点阅读单元按逻辑章归并，保序。返回 [{title, units:[unit...]}]。"""
    groups: list[dict[str, Any]] = []
    for u in units:
        m = _PART_SUFFIX_RE.search(u['title'])
        num = _part_number(m.group()) if m else None
        base = u['title'][:m.start()].rstrip(' 　:：-—_') if m else ''
        if m and num is not None and groups:
            g = groups[-1]
            if g.get('base') and g['base'] == base and g['next'] == num:
                g['units'].append(u)
                g['next'] = num + 1
                continue
        # 首单元后缀为 1 才开放后续合并；否则作为不可合并的独立章
        groups.append({'title': u['title'], 'base': (base if num == 1 else ''),
                       'units': [u], 'next': (2 if num == 1 else None)})
        # 多单元章之后偶尔挂一个无后缀的同标题重复单元（站方目录瑕疵），丢弃
        if len(groups) >= 2 and num is None:
            prev = groups[-2]
            if len(prev['units']) > 1 and u['title'] == prev['base']:
                groups.pop()
    for g in groups:
        if len(g['units']) > 1:
            g['title'] = g['base']
    return groups


# ---------------------------------------------------------------------------
# 单章正文（base64 混淆 + _N 分页）
# ---------------------------------------------------------------------------
# payload 长度下限不能设大：短对白（"大哥。"）编码后只有 ~24 字符，
# 设 40 会丢段落。正文真伪改靠"解码后必须是 <p>/<br> 段落 + 含中文"校验。
_B64_WRITELN_RE = re.compile(
    r"document\.writeln\(\s*[\w$]+(?:\.[\w$]+)?\(\s*['\"]([A-Za-z0-9+/=\s]{8,})['\"]\s*\)\s*\)",
    re.I)
_HHEKGV_RE = re.compile(r"var\s+hhekgv\s*=\s*['\"]([^'\"]+)['\"]")
_LINK_NEXT_RE = re.compile(r'<a[^>]+id="linkNext"[^>]+href="([^"]+)"', re.I)
_CONTENT_DIV_RE = re.compile(r'<div[^>]*id="content"[^>]*>(.*?)</div>', re.S | re.I)
_AD_PATTERNS = (
    re.compile(r'顶点小说网|笔趣阁|本章未完|点击下一页|手机用户请浏览|www\.[a-z0-9.]+'),
    re.compile(r'^第?\s*\d+\s*页\s*$'),
)


def _decode_b64_chunks(page: str) -> list[str]:
    """提取页内正文 base64 段 → 解码 → 纯文本段落。

    lewen 模板的解码函数名是每页随机的（实测见过 mz / wzrb.amz / wrxlhkdf.jpokd），
    但调用形态固定：document.writeln(<随机名>('<base64>'))，且 payload 解码后是
    <p>正文</p>。因此以 document.writeln 调用为锚点，并对解码结果做正文校验，
    避免误收页面里其他 base64 串。
    """
    cands = _B64_WRITELN_RE.findall(page)
    paras: list[str] = []
    for raw in cands:
        try:
            decoded = base64.b64decode(raw.strip(), validate=False).decode('utf-8', errors='replace')
        except (binascii.Error, ValueError):
            continue
        # 正文校验：必须是 <p>/<br> 段落 HTML、含至少 1 个汉字、且不是脚本。
        # CJK 阈值不能设高：独立成段的短对白（"大哥。""等一下。"）只有 2~3 个汉字。
        plain = _TAG_RE.sub('', decoded)
        cjk = sum(1 for ch in plain if '一' <= ch <= '鿿')
        low = decoded.lower()
        if (cjk < 1 or not re.search(r'<(?:p|br)\b', decoded, re.I)
                or 'function' in low or '<script' in low or 'document.' in low):
            continue
        chunk = re.sub(r'<br\s*/?>', '\n', decoded, flags=re.I)
        chunk = _TAG_RE.sub('', chunk)
        chunk = _html.unescape(chunk)
        for line in chunk.split('\n'):
            line = line.strip()
            if len(line) < 2:
                continue
            if any(p.search(line) for p in _AD_PATTERNS):
                continue
            paras.append(line)
    return paras


def fetch_web_chapter(chapter_url: str, base: str) -> dict[str, str] | None:
    """抓单章（自动跟 _N 分页拼接）。返回 {title,content}，失败 None。"""
    main = _http_get(chapter_url, referer=base + '/')
    title = ''
    mh1 = re.search(r'<h1[^>]*>(.*?)</h1>', main, re.S | re.I)
    if mh1:
        title = _html.unescape(_TAG_RE.sub('', mh1.group(1))).strip()
    if not title:
        mt = re.search(r'<title>([^<_]+)[_<]', main)
        title = _html.unescape(mt.group(1)).strip() if mt else ''

    first = _HHEKGV_RE.search(main)
    sub_url = urljoin(chapter_url, first.group(1)) if first else ''
    paras: list[str] = []
    if not sub_url:
        # 其他模板：正文直接内嵌
        paras = _decode_b64_chunks(main)
        mdiv = _CONTENT_DIV_RE.search(main)
        if mdiv and not paras:
            txt = _html.unescape(_TAG_RE.sub('', mdiv.group(1)))
            paras = [x.strip() for x in txt.split('\n') if len(x.strip()) >= 2]
    page_no = 0
    cur = sub_url
    while cur and page_no < _MAX_SUBPAGES:
        page_no += 1
        page = _http_get(cur, referer=chapter_url)
        paras.extend(_decode_b64_chunks(page))
        mdiv = _CONTENT_DIV_RE.search(page)
        if mdiv and not _B64_WRITELN_RE.search(page):
            txt = _html.unescape(_TAG_RE.sub('', mdiv.group(1)))
            paras.extend(x.strip() for x in txt.split('\n') if len(x.strip()) >= 2)
        mnxt = _LINK_NEXT_RE.search(page)
        nxt = urljoin(cur, mnxt.group(1)) if mnxt else ''
        # 仅跟随 /book/<bid>/<cid>_<n+1>.html 形态；下一章（无下划线）即末页
        m_cur = re.search(r'_(\d+)\.html', cur)
        m_nxt = re.search(r'_(\d+)\.html', nxt)
        if m_cur and m_nxt and int(m_nxt.group(1)) == int(m_cur.group(1)) + 1:
            cur = nxt
            time.sleep(0.25)  # 同章分页礼貌间隔
        else:
            cur = ''
    content = '\n\n'.join(paras).strip()
    if len(re.sub(r'\s', '', content)) < _MIN_CHAPTER_CHARS:
        return None
    return {'title': title, 'content': content}


# ---------------------------------------------------------------------------
# 编排：对外主入口
# ---------------------------------------------------------------------------
def fetch_web_novel_content(title: str, *, author: str | None = None,
                            max_chapters: int = 30,
                            max_chars: int = 120000) -> dict[str, Any]:
    """按书名/作者抓免费小说站前 N 章正文。

    成功返回与 fetch_rank_book_content 同构的 dict（platform='web_novel'）；
    搜不到书 / 目录为空 / 正文全失败抛 RuntimeError（路由层转友好提示）。
    """
    title = (title or '').strip()
    if not title:
        raise RuntimeError('缺少书名，无法在免费小说站检索')
    max_chapters = max(1, min(int(max_chapters or 30), 30))

    book = search_web_book(title, author)
    if not book:
        raise RuntimeError(f'免费小说站未搜到《{title}》，请核对书名/作者后重试或直接粘贴正文')

    try:
        chapters = fetch_web_chapter_list(book)
    except Exception as e:
        raise RuntimeError(f'目录页获取失败（{book["name"]}）：{str(e)[:100]}')
    if not chapters:
        raise RuntimeError(f'《{book["title"]}》目录解析为空，源站可能改版，请改用粘贴')

    targets = chapters[:max_chapters]
    fetched: list[dict[str, str] | None] = [None] * len(targets)

    def _grab(pos: int, ch: dict[str, str]) -> tuple[int, dict[str, str] | None]:
        try:
            return pos, fetch_web_chapter(ch['url'], book['base'])
        except Exception:
            return pos, None

    def _run_pool(positions: list[int]) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = []
            for pos in positions:
                futs.append(pool.submit(_grab, pos, targets[pos]))
                time.sleep(0.25)  # 礼貌错峰：镜像站服务器偏弱，瞬时高并发易限流
            for f in futs:
                pos, got = f.result()
                fetched[pos] = got

    _run_pool(list(range(len(targets))))
    # 失败补抓一轮（瞬时限流/代理抖动兜底）：退避后按原并发再来一次
    failed = [i for i, x in enumerate(fetched) if x is None]
    if failed:
        time.sleep(1.2)
        _run_pool(failed)

    # 前 3 章仍全失败 → 源站大概率整体不可用，报错而不是返回空壳
    head_fail = sum(1 for x in fetched[:3] if not x)
    if head_fail >= 3:
        raise RuntimeError(f'{book["name"]} 正文连续抓取失败（反爬/改版），请稍后重试或直接粘贴正文')

    parts: list[str] = []
    meta: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for pos, got in enumerate(fetched):
        if not got:
            continue
        ch_title = got['title'] or targets[pos]['title'] or f'章节{len(meta) + 1}'
        piece = f'{ch_title}\n\n{got["content"]}'
        if total + len(piece) >= max_chars:
            piece = piece[:max(0, max_chars - total)]
            truncated = True
        if piece.strip():
            parts.append(piece.strip())
            total += len(piece)
            meta.append({'title': ch_title,
                         'itemId': targets[pos]['url'],
                         'chars': len(got['content'])})
        if truncated:
            break
    if not parts:
        raise RuntimeError('章节正文解析为空，源站可能改版，请改用粘贴')

    return {
        'ok': True,
        'platform': 'web_novel',
        'sourceName': book['name'],
        'bookId': f'web:{book["name"]}:{book["bookId"]}',
        'matchedTitle': book['title'],
        'matchedAuthor': book.get('author') or '',
        'matchLevel': book['matchLevel'],
        'chapterCount': len(chapters),
        'fetchedChapters': meta,
        'content': '\n\n'.join(parts),
        'truncated': truncated,
        'decodeRate': 1.0,
        'locked': False,
        'lockedChapterNo': 0,
    }
