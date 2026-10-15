"""榜单风向 Blueprint（完整移植自 easy-writing: NovelRank 模块）。

包含：
1. 4 个对外 API：
   - GET  /api/rank/platforms                     平台列表
   - GET  /api/rank/filters?platform=...          榜单类型 + 男女频 + 分类选项
   - GET  /api/rank/list?sourceId=...&...         榜单书籍列表（实时抓 + 1h 内存缓存 + 熔断）
   - POST /api/rank/crawl?sourceId=...            强制刷新当前榜单
2. 智驾×榜单风向：/api/rank/scan-for-concept（构思扫榜 → LLM 市场情报聚合）

种子数据与抓取适配器已拆到 blueprints/novel_rank_crawlers.py（本文件只保留路由层）。
使用方：Fanshu 工具页「📈 榜单风向」Tab。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, request

# 触发 LLM 的 rank 路由必须登录：/api/rank/ 在全局鉴权白名单内（榜单浏览是公共
# 数据），白名单路径不会被全局守卫注入 request.current_user_id；若不单独加
# login_required，get_llm_config() 会回落到共享/系统兜底 key（user_id IS NULL），
# 表现为"别的功能用用户自己的模型正常，定位轻拆却 429 余额不足"。
from auth_utils import login_required

from blueprints.novel_rank_crawlers import (
    RANK_CATEGORIES,
    RANK_SITES,
    RANK_SOURCES,
    RANK_TYPE_LABELS,
    _QIDIAN_RANK_LABEL,
    _clean,
    _find_category,
    _find_source,
    crawl_qidian_api,
    crawl_rank_source,
    fetch_rank_book_content,
    resolve_sources,
)

# ---------------------------------------------------------------------------
# Blueprint 实例
# ---------------------------------------------------------------------------
novel_rank_bp = Blueprint('novel_rank', __name__)



# ---------------------------------------------------------------------------
# 5. Flask 路由
# ---------------------------------------------------------------------------
@novel_rank_bp.route('/api/rank/platforms', methods=['GET'])
def api_rank_platforms():
    platforms = [{'code': s['code'], 'name': s['name'], 'baseUrl': s.get('baseUrl'),
                  'remark': s.get('remark')} for s in RANK_SITES if s.get('enabled') == 1]
    return jsonify({'platforms': platforms})


@novel_rank_bp.route('/api/rank/filters', methods=['GET'])
def api_rank_filters():
    platform = request.args.get('platform', 'fanqie').strip()
    # 平台下线校验
    site_row = next((s for s in RANK_SITES if s['code'] == platform), None)
    if site_row is None or site_row.get('enabled') != 1:
        return jsonify({'platform': platform, 'rankTypes': [], 'genders': [], 'categories': [], 'subcategories': []})

    # ---- 起点：走移动端 JSON 接口，榜单类型 / 男女频 / 大类 / 主题子类 全部可选 ----
    # 按需求 7：起点只保留 月票榜 + 新书榜（原「新人作者新书榜」→ 统一叫 新书榜）
    if platform == 'qidian':
        rank_types: list[dict[str, str]] = [
            {'value': 'monthTicket', 'label': '月票榜'},
            {'value': 'newauthor',   'label': '新书榜'},
        ]
        categories: list[dict[str, Any]] = [
            {'id': 'all', 'code': '__all__', 'name': '全部', 'scope': 'all'}
        ]
        subcategories: list[dict[str, Any]] = []
        parent_by_id: dict[int, Any] = {}
        for c in RANK_CATEGORIES:
            if c['siteCode'] != 'qidian' or c.get('enabled') != 1:
                continue
            if c.get('parentLegacyId') is None:
                parent_by_id[c['legacyId']] = c
        for c in RANK_CATEGORIES:
            if c['siteCode'] != 'qidian' or c.get('enabled') != 1:
                continue
            if c.get('parentLegacyId') is None:
                categories.append({
                    'id': f"cat:{c['legacyId']}",
                    'categoryId': c['legacyId'],
                    'code': c['code'],
                    'name': c['name'],
                    'gender': c.get('gender'),
                    'scope': 'category',
                    'level': 1,
                })
            else:
                p = parent_by_id.get(c['parentLegacyId'])
                subcategories.append({
                    'id': f"subcat:{c['legacyId']}",
                    'categoryId': c['legacyId'],
                    'code': c['code'],
                    'name': c['name'],
                    'parentCode': p['code'] if p else None,
                    'gender': c.get('gender'),
                    'scope': 'category',
                    'level': 2,
                })
        return jsonify({
            'platform': platform,
            'rankTypes': rank_types,
            'genders': ['male', 'female'],
            'categories': categories,
            'subcategories': subcategories,
        })

    # ---- 番茄：共举源配置（阅读/新书）----
    # 榜单类型：从当前平台启用的源里求并集
    rank_types: list[dict[str, str]] = []
    seen: set[str] = set()
    for s in RANK_SOURCES:
        if s['siteCode'] != platform or s.get('enabled') != 1:
            continue
        rt = s['rankType']
        if rt in seen:
            continue
        seen.add(rt)
        rank_types.append({'value': rt, 'label': RANK_TYPE_LABELS.get(rt, rt)})
    # 男女频：只有该平台同时含男女频数据时才显示切换（否则读默认值）
    genders: set[str] = set()
    for s in RANK_SOURCES:
        if s['siteCode'] != platform or s.get('enabled') != 1:
            continue
        cat = _find_category(s.get('categoryLegacyId')) if s.get('categoryLegacyId') else None
        g = (s.get('meta') or {}).get('gender') or (cat['gender'] if cat else None)
        if g:
            genders.add(g)
    genders_list = sorted(genders)
    # 分类（父级），按平台与 gender（若传入则过滤）
    gender_q = request.args.get('gender')
    rank_type_q = request.args.get('rankType')
    category_list: list[dict[str, Any]] = [
        {'id': 'all', 'code': '__all__', 'name': '全部', 'scope': 'all'}
    ]
    for s in RANK_SOURCES:
        if s['siteCode'] != platform or s.get('enabled') != 1:
            continue
        if rank_type_q and s['rankType'] != rank_type_q:
            continue
        meta = s.get('meta') or {}
        if meta.get('scope') == 'all' and not s.get('categoryLegacyId'):
            # 平台总榜单独放在分类里
            if not any(c.get('id') == f"src:{s['legacyId']}" for c in category_list):
                category_list.append({
                    'id': f"src:{s['legacyId']}",
                    'sourceId': s['legacyId'],
                    'code': f"scope-all-{s['legacyId']}",
                    'name': (RANK_TYPE_LABELS.get(s['rankType']) or s['rankType']) + '（总榜）',
                    'scope': 'all',
                })
            continue
        cat = _find_category(s.get('categoryLegacyId'))
        if not cat or cat.get('parentLegacyId') is not None:
            continue
        if gender_q and cat.get('gender') != gender_q:
            continue
        # 去重
        if any(c.get('code') == cat['code'] for c in category_list):
            continue
        category_list.append({
            'id': f"cat:{cat['legacyId']}",
            'categoryId': cat['legacyId'],
            'code': cat['code'],
            'name': cat['name'],
            'gender': cat.get('gender'),
            'scope': 'category',
        })
    return jsonify({
        'platform': platform,
        'rankTypes': rank_types,
        'genders': genders_list,
        'categories': category_list,
        'subcategories': [],
    })


@novel_rank_bp.route('/api/rank/list', methods=['GET'])
def api_rank_list():
    """
    拉取某一榜单或聚合视图的书籍列表。
    优先级：
      1. sourceId 指定 -> 直接抓此榜单
      2. platform + (rankType/gender/categoryCode) 组合 -> 解析出首个匹配榜单源
      3. platform 默认 -> 默认读 番茄阅读榜
    """
    source_id_raw = request.args.get('sourceId')
    platform = request.args.get('platform', 'fanqie').strip()
    # 平台下线校验：七猫等未启用站点不再对外提供实时榜单
    site_row = next((s for s in RANK_SITES if s['code'] == platform), None)
    if site_row is None or site_row.get('enabled') != 1:
        return jsonify({
            'sourceId': source_id_raw,
            'items': [],
            'itemCount': 0,
            'page': 1,
            'pageSize': 50,
            'total': 0,
            'fetchError': f'平台「{platform}」已下线',
        })
    rank_type = request.args.get('rankType')
    gender = request.args.get('gender')
    category_code = request.args.get('categoryCode')
    keyword = (request.args.get('keyword') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except Exception:
        page = 1
    # 默认单榜 20 本（与参考站一致），若前端没传 pageSize 就按 20 切；上限仍 200
    try:
        page_size = min(200, max(5, int(request.args.get('pageSize', 20))))
    except Exception:
        page_size = 20

    force = request.args.get('force', '0') == '1'

    # ---- 起点：直接走移动端 JSON 接口（按 榜单类型×男女频×大类×主题子类 组合）----
    if platform == 'qidian':
        rank_type = rank_type or 'hotsale'
        gender = gender or 'male'
        category_code = None if category_code in ('__all__', None, '') else category_code
        try:
            data = crawl_qidian_api(rank_type, gender=gender, category_code=category_code,
                                    max_pages=2, force=force)
            items = list(data.get('items') or [])
            fetch_error = None
        except Exception as exc:
            items, fetch_error = [], str(exc)
        if keyword:
            kw = keyword.lower()
            items = [
                it for it in items
                if kw in (it.get('bookTitle') or '').lower()
                or kw in (it.get('authorName') or '').lower()
                or kw in (it.get('categoryName') or '').lower()
            ]
        total = len(items)
        start = (page - 1) * page_size
        return jsonify({
            'sourceId': None,
            'siteCode': 'qidian',
            'rankType': rank_type,
            'rankTitle': _QIDIAN_RANK_LABEL.get(rank_type, rank_type),
            'pageTitle': category_code or None,
            'cutoffText': None,
            'fetchAt': int(time.time()),
            'sourceKind': 'live',
            'fetchError': fetch_error,
            'fetch_ok': fetch_error is None,
            'page': page,
            'pageSize': page_size,
            'total': total,
            'itemCount': total,
            'items': items[start:start + page_size],
        })

    source_id = None
    if source_id_raw:
        try:
            source_id = int(source_id_raw)
        except Exception:
            source_id = None

    if source_id is None:
        # 找第一个匹配的榜单源
        candidates = resolve_sources(platform, rank_type, gender, category_code)
        if not candidates:
            # 实在没有，返回空
            return jsonify({
                'sourceId': None,
                'items': [],
                'itemCount': 0,
                'page': page,
                'pageSize': page_size,
                'total': 0,
                'fetchError': '当前筛选条件没有匹配的榜单源',
            })
        # 如果是 categoryCode 筛选，可能一个 rankType 下有多个分类源。分类=全部时，取第一个 scope=all 的源，否则取第一个 category 源
        if category_code == '__all__' or not category_code:
            # 偏好平台总榜；没有则取首个候选
            src = next((c for c in candidates if (c.get('meta') or {}).get('scope') == 'all'), candidates[0])
        else:
            src = candidates[0]
        source_id = int(src['legacyId'])

    force = request.args.get('force', '0') == '1'
    # 参考站：单榜固定 20 本；先一次拉 100 条入缓存以便关键词搜索时能切到匹配项，实际分页仍按 page_size=20 返回
    data = crawl_rank_source(source_id, force=force, limit=max(100, page_size))
    items = list(data.get('items') or [])

    # 关键词搜索（书名 / 作者 / 分类）
    if keyword:
        kw = keyword.lower()
        items = [
            it for it in items
            if kw in (it.get('bookTitle') or '').lower()
            or kw in (it.get('authorName') or '').lower()
            or kw in (it.get('categoryName') or '').lower()
        ]
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    items_page = items[start:end]

    return jsonify({
        'sourceId': data.get('sourceId'),
        'siteCode': data.get('siteCode'),
        'rankType': data.get('rankType'),
        'rankTitle': data.get('rankTitle'),
        'pageTitle': data.get('pageTitle'),
        'cutoffText': data.get('cutoffText'),
        'fetchAt': data.get('fetchAt'),
        'sourceKind': data.get('sourceKind', 'live'),
        'fetchError': data.get('fetchError'),
        'circuitOpen': data.get('circuitOpen', False),
        'circuitRetryAfter': data.get('circuitRetryAfter'),
        'droppedCount': data.get('droppedCount', 0),
        'structureProblems': data.get('structureProblems') or [],
        'health': data.get('health'),
        'page': page,
        'pageSize': page_size,
        'total': total,
        'itemCount': total,
        'items': items_page,
    })


@novel_rank_bp.route('/api/rank/crawl', methods=['POST'])
def api_rank_crawl():
    """强制忽略缓存刷新某榜单。"""
    body = request.get_json(silent=True) or {}
    sid = body.get('sourceId') or request.args.get('sourceId')
    if not sid:
        return jsonify({'error': '缺少 sourceId'}), 400
    try:
        data = crawl_rank_source(int(sid), force=True, limit=200)
        return jsonify({'ok': True, 'itemCount': len(data.get('items') or []), 'fetchAt': data.get('fetchAt')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# =============================================================================
# 智驾×榜单风向：/api/rank/scan-for-concept
#   - 默认扫「番茄 · 新书榜」；用户显式传 platform=qidian 时扫「起点 · 新书榜」
#   - 规则：用户输入构想 → 先做分类关键词匹配 → 命中分类的新书榜 Top8 抓书 → LLM 抽（开篇钩子/热元素/毒点/书名公式）→ 存 session.meta_json.rank_scan
#   - 供 chat_collab_bp.build_chat_system_prompt / smartSuggest / Generate / Roundtable 统一注入
# =============================================================================

# ---- 分类→关键词映射表：用于"构想文本→分类"快速粗匹配（比纯LLM快、省token）----
_CATEGORY_KEYWORD_MAP: dict[int, list[str]] = {
    # —— 番茄 男频（父分类 legacyId）新书榜源：父分类 legacyId 86~104 对应男频番茄新书榜 ——
    86: ['奇幻', '魔幻', '西方魔法', '剑与魔法', 'dnd', '巫师', '龙族'],
    87: ['仙侠', '修真', '修仙', '金丹', '元婴', '剑道', '炼气', '宗门'],
    88: ['科幻', '末世', '赛博', '机甲', '星际', '废土', '宇宙', '基因锁', '进化', '末日'],
    89: ['都市日常', '重生', '创业', '生活', '摆摊', '开店', '奶爸', '神豪', '四合院'],
    90: ['都市修真', '都市修仙', '都市异能', '赘婿修仙', '下山'],
    91: ['高武', '都市高武', '武徒', '灵气复苏', '武道', '武校', '觉醒', '血脉'],
    92: ['历史古代', '历史', '大明', '大唐', '大明王朝', '皇朝', '春秋战国', '秦汉', '三国', '水浒', '红楼'],
    93: ['战神', '赘婿', '兵王', '战神归来', '龙王', '上门'],
    94: ['种田', '乡村', '农家乐', '山清水秀', '渔村', '田园', '空间种田'],
    95: ['玄幻', '传统玄幻', '斗气', '斗破', '大帝', '天帝', '万古', '神朝'],
    96: ['历史脑洞', '穿明', '穿唐', '反套路历史', '历史直播', '盘点历史'],
    97: ['悬疑脑洞', '规则怪谈', '规则', '惊悚', '副本', '无限流', '诡异', '惊悚游戏'],
    98: ['都市脑洞', '系统', '签到', '曝光', '直播', '算命', '天眼', '都市异能'],
    99: ['玄幻脑洞', '玄幻系统', '多子多福', '老祖宗', '宗门流', '横推', '召唤猛将'],
    100: ['悬疑灵异', '灵异', '驱邪', '盗墓', '鬼', '阴', '茅山', '道士'],
    101: ['抗战', '谍战', '抗日', '谍报', '间谍', '军旅', '打仗'],
    102: ['游戏', '体育', '电竞', '网游', '足球', '篮球', 'moba', '攻略'],
    103: ['动漫', '二次元', '综漫', '同人', '海贼', '火影', '柯南'],
    104: ['男频衍生', '港综', '美漫', '漫威', '影视同人', '综艺'],
    # —— 番茄 女频 新书榜 父分类 legacyId 161~178 ——
    161: ['古言', '古风世情', '宅斗', '世家', '王妃', '皇后', '嫡女', '庶女'],
    162: ['女频科幻', '女频末世', '星际女强', '末世女强'],
    163: ['女频游戏', '女频体育', '电竞女主'],
    164: ['女频衍生', '同人文', '影视同人', '韩娱', '清穿', '综穿'],
    165: ['玄幻言情', '女强', '女帝', '女玄', '战神王妃', '驭兽'],
    166: ['种田', '农家', '空间', '美食', '穿越种田', '经商'],
    167: ['年代', '七零', '八零', '九零', '军婚', '知青', '年代文'],
    168: ['现言脑洞', '穿书', '爽文', '系统', '重生复仇', '真假千金', '豪门', '契约婚姻'],
    169: ['宫斗', '宅斗', '太后', '皇后', '争宠'],
    170: ['女频悬疑', '女频规则怪谈', '惊悚女主', '探案女主'],
    171: ['古言脑洞', '穿古', '反套路古言', '女扮男装', '科举女主'],
    172: ['快穿', '系统快穿', '攻略', '宿主', '位面'],
    173: ['甜宠', '青春', '校园甜宠', '小奶狗', '甜文', '暗恋', '校园'],
    174: ['娱乐圈', '明星', '演艺', '顶流', '影帝', '恋综'],
    175: ['悬疑言情', '刑侦言情', '法医女主'],
    176: ['职场婚恋', '婚恋', '婚姻', '霸道总裁', '霸总', '上司', '先婚后爱'],
    177: ['豪门总裁', '总裁', '豪门', '总裁文', '霸道总裁', '替身', '娇妻'],
    178: ['民国言情', '民国', '少帅', '军阀'],
}

# 起点大盘新书榜（不分分类），命中任何男/女频关键词 → 走起点大盘 newauthor
_QIDIAN_NEW_BOOK_SOURCE_LEGACY_ID = 47  # legacyId=47 siteCode=qidian rankType=newauthor，大盘全品类新书榜


def _detect_gender_by_keywords(concept: str) -> str:
    """按关键词粗判男女频；无法判则男频（默认番茄新书榜男频19分类覆盖广）。"""
    if not concept:
        return 'male'
    female_hit = sum(1 for kw in ['古言', '快穿', '甜宠', '宫斗', '宅斗', '年代', '民国言情', '娱乐圈', '豪门总裁',
                                   '庶女', '王妃', '女强', '女帝', '青梅', '竹马', '恋综', '军婚', '知青',
                                   '穿书', '重生复仇', '真假千金', '先婚后爱', '小奶狗', '暗恋', '校园甜']
                   if kw in concept)
    male_hit = sum(1 for kw in ['玄幻', '高武', '修仙', '修真', '仙侠', '末世', '战神', '赘婿', '洪荒', '诸天',
                                 '武道', '宗门', '灵气复苏', '电竞', '网游', '历史', '三国', '抗日', '谍战',
                                 '系统', '签到', '老祖宗', '多子多福', '横推', '诡异', '规则怪谈', '盗墓']
                   if kw in concept)
    if female_hit and not male_hit:
        return 'female'
    if male_hit and not female_hit:
        return 'male'
    # 字数 >80 且出现「主角是女/她/小姐/公主/女主/姐姐/妹妹」密集 → female
    she_count = len(re.findall(r'她|女主|小姐|公主|王妃|皇后|庶女|嫡女|姐姐|妹妹|女生|女大学生', concept))
    return 'female' if she_count >= 2 else 'male'


def _rank_category_match_score(cat_legacy_id: int, concept: str, gender: str) -> int:
    """给某分类算匹配得分；返回整数，越大越匹配。"""
    kws = _CATEGORY_KEYWORD_MAP.get(cat_legacy_id) or []
    if not kws:
        return 0
    score = 0
    c = concept or ''
    for kw in kws:
        if kw and kw in c:
            score += 3 if len(kw) >= 3 else 2
    # 分类本身名字也做一次直接包含匹配
    cat = _find_category(cat_legacy_id)
    if cat and cat.get('name') and cat['name'] in c:
        score += 5
    # 男女频惩罚
    if cat and cat.get('gender') and gender and cat['gender'] != gender:
        score -= 10
    return score


def _match_rank_new_book_sources(concept: str, platform: str,
                                 gender: str | None = None,
                                 max_sources: int = 3) -> list[dict[str, Any]]:
    """
    仅选【新书榜】rankType，按概念匹配度返回 Top N 榜单源。
    - platform=fanqie 默认：用分类关键词挑选匹配度最高的 ≤3 个分类新书榜
    - platform=qidian：只有大盘新书榜 legacyId=47（不分分类）
    """
    if platform == 'qidian':
        src = _find_source(_QIDIAN_NEW_BOOK_SOURCE_LEGACY_ID)
        return [src] if src else []
    # 番茄：选 categoryLegacyId 不为空 且 rankType='new' 的源
    concept = concept or ''
    gender = gender or _detect_gender_by_keywords(concept)
    scored: list[tuple[int, dict]] = []
    for s in RANK_SOURCES:
        if s.get('enabled') != 1:
            continue
        if s.get('siteCode') != 'fanqie':
            continue
        if s.get('rankType') != 'new':
            continue
        if not s.get('categoryLegacyId'):
            continue
        sc = _rank_category_match_score(int(s['categoryLegacyId']), concept, gender)
        if sc > 0:
            scored.append((sc, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = [s for _, s in scored[:max_sources]]
    # 兜底：没命中任何分类关键词 → 按 gender 选 3 个默认（都市高武/玄幻脑洞/科幻末世 男；现言脑洞/甜宠/年代 女）
    if not chosen:
        fallback_ids = {
            'male': [91, 99, 88],   # 男频：都市高武/玄幻脑洞/科幻末世
            'female': [168, 173, 167],  # 女频：现言脑洞/青春甜宠/年代
        }.get(gender, [91, 99, 88])
        for cid in fallback_ids:
            for s in RANK_SOURCES:
                if s.get('enabled') != 1 or s.get('siteCode') != 'fanqie' or s.get('rankType') != 'new':
                    continue
                if s.get('categoryLegacyId') == cid:
                    chosen.append(s)
                    break
    # 去重
    out: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for s in chosen:
        sid = int(s['legacyId'])
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        out.append(s)
    return out


def _call_small_llm_json(messages: list[dict], max_tokens: int = 512) -> tuple[dict, str]:
    """调用轻量 LLM 返回 JSON。

    返回 (data, error)：成功 data 为解析后的 dict、error 为空串；失败 data 为 {}、
    error 为可直接展示给用户的真实原因。
    注意：LLMGateway.chat() 返回的是 ModelResult（字段 .content/.ok/.error），
    不是 dict——历史代码误用 out.get('text')，导致 str(ModelResult) 的 repr 被
    贪婪正则匹配到 raw={...} 花括号、JSON 永远解析失败，定位轻拆必挂。
    """
    try:
        from llm_gateway import LLMGateway, get_llm_config  # 延迟导入，避免循环依赖
        base_url, api_key, model = get_llm_config()
        if not api_key:
            return {}, '尚未配置 AI（或 API Key 为空），请先在「我的 → AI 配置」中选择模型'
        gw = LLMGateway(base_url, api_key, model)
        out = gw.chat(messages, temperature=0.4, max_tokens=max_tokens,
                      response_format={'type': 'json_object'})
        # 部分服务商不支持 response_format 参数（400/422）→ 去掉该参数重试一次，
        # 与 ai_analyze_bp 的兜底策略保持一致，避免“别的功能能用、轻拆必挂”。
        if not getattr(out, 'ok', False):
            err_msg = (getattr(out, 'error', '') or '')
            if ('response_format' in err_msg or 'json_object' in err_msg
                    or 'unrecognized' in err_msg.lower() or 'unsupported' in err_msg.lower()):
                out = gw.chat(messages, temperature=0.4, max_tokens=max_tokens)
        if not getattr(out, 'ok', False):
            return {}, (getattr(out, 'error', '') or '模型调用失败，请检查 AI 配置或稍后重试')
        txt = (out.content or '').strip()
        if not txt:
            return {}, '模型返回了空内容，请换一个模型或稍后重试'
        # 容错：剥除 ```json ... ``` 包裹
        fence = re.match(r'^```(?:json)?\s*([\s\S]*?)\s*```$', txt)
        if fence:
            txt = fence.group(1).strip()
        # 容错：正文前后可能有解释文字，截取首个 { 到末个 }
        start, end = txt.find('{'), txt.rfind('}')
        if start != -1 and end > start:
            txt = txt[start:end + 1]
        try:
            data = json.loads(txt)
            if isinstance(data, dict):
                return data, ''
        except Exception:
            pass
        return {}, '模型返回内容不是合法 JSON，请换用更强的模型后重试'
    except Exception as e:
        return {}, f'AI 调用异常：{str(e)[:200]}'


def _aggregate_market_llm(concept: str, merged_items: list[dict], matched_labels: list[str]) -> dict:
    """基于 TopN 书元数据，让 LLM 抽四大市场情报列表；失败返回规则兜底。"""
    if not merged_items:
        return {
            'opening_patterns': [],
            'popular_elements': [],
            'landmine_elements': [],
            'title_formulas': [],
            'golden_finger_types': [],
            'intro_formulas': [],
            'setting_selling_points': [],
            'golden_three_patterns': [],
        }
    user_prompt = f"【用户构想】\n{concept[:800]}\n\n"
    user_prompt += "【匹配的新书榜 TOP 10】\n"
    for idx, it in enumerate(merged_items[:10], 1):
        title = _clean(it.get('bookTitle') or it.get('title'))
        intro = _clean(it.get('intro') or it.get('bookIntro') or '')[:400]
        tags = []
        raw_tags = it.get('tags') or it.get('categoryName') or ''
        if isinstance(raw_tags, list):
            tags = [str(x) for x in raw_tags if x]
        elif isinstance(raw_tags, str):
            tags = [x for x in re.split(r'[,，、/\- ]', raw_tags) if x]
        line = f"{idx}. 《{title}》"
        if tags:
            line += f" 标签：{'/'.join(tags[:5])}"
        if intro:
            line += f"\n    简介：{intro}"
        user_prompt += line + "\n"
    user_prompt += (
        "\n【任务】认真研读上方新书榜 TOP 10（已命中分类：" + '、'.join(matched_labels) + "）"
        " 的书名、简介、标签，像爆款拆书编辑一样深挖，总结面向网文作者的市场情报。"
        " 仅输出一个 JSON 对象，不要任何解释文字，不要 ```json 包裹。"
        " JSON 结构（固定 8 个数组，每项是一句中文，每条 20~80 字，每个数组 4~8 条）：\n"
        "{\n"
        "  \"opening_patterns\": [\"开篇钩子套路（第1章怎么抓住读者）\"...],\n"
        "  \"popular_elements\": [\"读者买单要素/热门爽点元素\"...],\n"
        "  \"landmine_elements\": [\"读者弃文毒点/避雷要素\"...],\n"
        "  \"title_formulas\": [\"书名公式范例（用占位符+点名学自哪本）\"...],\n"
        "  \"golden_finger_types\": [\"上榜书金手指类型拆解（类型+爽点结构+适用题材，点名哪本书）\"...],\n"
        "  \"intro_formulas\": [\"上榜书简介写法套路（开篇冲突句式/金手指亮法/悬念留法）\"...],\n"
        "  \"setting_selling_points\": [\"上榜书核心设定卖点（世界观/力量体系/身份反差的差异化打法）\"...],\n"
        "  \"golden_three_patterns\": [\"黄金三章结构套路（从简介反推：第1章困境/第2章金手指/第3章爽点+钩子）\"...]\n"
        "}\n"
    )
    messages = [
        {'role': 'system', 'content': '你是番茄/起点爆款拆书数据分析编辑：只依据给出的榜单数据做归纳，每条结论尽量点名具体榜上书；说话精炼、全用中文、不输出废话、只给可落地的结论。'},
        {'role': 'user', 'content': user_prompt},
    ]
    resp, _err = _call_small_llm_json(messages, max_tokens=1800)
    # 规则级兜底 + 长度裁剪（新深度字段失败时静默为空，不阻塞主情报）
    def _arr(key: str, fallback: list[str]) -> list[str]:
        v = resp.get(key) or fallback
        if not isinstance(v, list):
            v = fallback
        cleaned = []
        for x in v:
            s = _clean(str(x))
            if 4 <= len(s) <= 160:
                cleaned.append(s)
            if len(cleaned) >= 8:
                break
        return cleaned or fallback
    def _arr_soft(key: str) -> list[str]:
        """新深度情报：LLM 没给就空列表，不注入规则兜底（避免假数据污染）。"""
        v = resp.get(key)
        if not isinstance(v, list):
            return []
        cleaned = []
        for x in v:
            s = _clean(str(x))
            if 4 <= len(s) <= 160:
                cleaned.append(s)
            if len(cleaned) >= 8:
                break
        return cleaned
    return {
        'opening_patterns': _arr('opening_patterns', ['开篇用旁白抛出世界观规则，随即切主角生死危机场面']),
        'popular_elements': _arr('popular_elements', ['能力分阶解锁+可视化进度']),
        'landmine_elements': _arr('landmine_elements', ['开篇堆砌设定>2段，无冲突']),
        'title_formulas': _arr('title_formulas', ['《前缀：核心卖点》']),
        'golden_finger_types': _arr_soft('golden_finger_types'),
        'intro_formulas': _arr_soft('intro_formulas'),
        'setting_selling_points': _arr_soft('setting_selling_points'),
        'golden_three_patterns': _arr_soft('golden_three_patterns'),
    }


# 扫榜缓存（5分钟）：避免同一用户/同一短时间内重复抓榜 + 重复LLM
_SCAN_CACHE_LOCK = threading.Lock()
_SCAN_CACHE: dict[str, tuple[float, dict]] = {}
_SCAN_CACHE_TTL = 300


def _core_rank_scan_for_concept(concept: str, platform: str = 'fanqie',
                                gender: str | None = None,
                                top_n: int = 3,
                                force: bool = False) -> dict:
    """【内部函数】根据构思匹配新书榜→抓榜→LLM聚合市场情报→返回完整payload。
    被 chat_collab_bp 的自然语言扫榜触发直接调用，跳过 HTTP 包装层，省时间 & 无缓存击穿。
    返回 dict：{ 'ok': True/False, 'error': str?, 'from_cache': bool, 'report': {...}? }
    注意：返回 payload 结构与 /api/rank/scan-for-concept HTTP 响应完全一致，
          前端 RankScanCard 可以直接渲染。
    """
    concept = _clean(concept or '')
    if len(concept) < 2:
        return {'ok': False, 'error': '构想太短，不足以匹配同类题材（至少 2 字）'}
    platform = platform or 'fanqie'
    if platform not in ('fanqie', 'qidian'):
        return {'ok': False, 'error': 'platform 只支持 fanqie（默认）或 qidian'}
    if gender not in ('male', 'female', None):
        gender = None
    top_n = max(1, min(5, int(top_n or 3)))

    cache_key = f'{platform}|{gender or "auto"}|{top_n}|{concept[:120]}'
    now = time.time()
    with _SCAN_CACHE_LOCK:
        cached = _SCAN_CACHE.get(cache_key)
        if (not force) and cached and now - cached[0] < _SCAN_CACHE_TTL:
            return {'ok': True, 'from_cache': True, 'report': cached[1]}

    gender = gender or _detect_gender_by_keywords(concept)

    # 1) 匹配榜单源（只选【新书榜】）
    sources = _match_rank_new_book_sources(concept, platform, gender, max_sources=top_n)
    if not sources:
        return {'ok': False, 'error': '没有匹配到可用的新书榜源'}

    # 2) 抓榜（复用 crawl_rank_source，带缓存 + 熔断）
    all_items: list[dict] = []
    matched_labels: list[str] = []
    source_infos: list[dict] = []
    for s in sources:
        try:
            payload = crawl_rank_source(int(s['legacyId']), force=force, limit=10)
        except Exception:
            continue
        cat = _find_category(s.get('categoryLegacyId')) if s.get('categoryLegacyId') else None
        site_name = next((x.get('name') or x.get('code') or x.get('code') for x in RANK_SITES if x.get('code') == s.get('siteCode')), '')
        s_gender = (s.get('meta') or {}).get('gender') or (cat.get('gender') if cat else None)
        label = (
            f"{site_name}"
            f"·{'男频' if s_gender == 'male' else '女频'}"
            f"·{cat.get('name') if cat else '大盘'}"
            f"·{s.get('title') or s.get('rankType')}"
        )
        matched_labels.append(label)
        items = list(payload.get('items') or [])[:10]
        source_infos.append({
            'sourceId': s.get('legacyId'),
            'platform': s.get('siteCode'),
            'categoryName': cat.get('name') if cat else '大盘',
            'rankType': s.get('rankType'),
            'rankTitle': s.get('title'),
            'itemCount': len(items),
            'fetchError': payload.get('fetchError'),
        })
        for it in items:
            cleaned = {
                'title': _clean(it.get('bookTitle') or it.get('title')),
                'author': _clean(it.get('authorName') or it.get('author')),
                'intro': _clean(it.get('intro') or it.get('bookIntro') or '')[:400],
                'tags': (it.get('tags') if isinstance(it.get('tags'), list) else
                         ([x for x in re.split(r'[,，、/\- ]', str(it.get('categoryName') or it.get('category') or '')) if x]
                          if (it.get('categoryName') or it.get('category')) else [])),
                'categoryName': _clean(it.get('categoryName') or it.get('category') or ''),
                'wordCount': int(it.get('wordCount') or it.get('word_count') or 0) or None,
                'metricName': _clean(it.get('metricName') or ''),
                'metricValue': int(it.get('metricValue') or it.get('metric_value') or 0) or None,
                'rank': int(it.get('rank') or 0) or None,
                'platform': s.get('siteCode'),
            }
            if cleaned['title']:
                all_items.append(cleaned)

    # 3) LLM 聚合
    if all_items:
        agg = _aggregate_market_llm(concept, all_items, matched_labels)
    else:
        agg = {'opening_patterns': [], 'popular_elements': [], 'landmine_elements': [], 'title_formulas': [],
               'golden_finger_types': [], 'intro_formulas': [], 'setting_selling_points': [], 'golden_three_patterns': []}

    # 4) 关键词抽取（规则，不用LLM）
    keyword_set: set[str] = set()
    for it in all_items[:10]:
        for tag in (it.get('tags') or []):
            if 1 <= len(tag) <= 16:
                keyword_set.add(tag)
    for l in (6, 4, 2):
        for i in range(0, max(0, len(concept) - l + 1)):
            sub = concept[i:i + l]
            if sub.isdigit():
                continue
            if sub in _CATEGORY_KEYWORD_MAP.get(0, []):
                continue
            if len(keyword_set) >= 16:
                break
            if any(kk in sub for kk in ('都市', '玄幻', '仙侠', '高武', '末世', '甜宠', '快穿',
                                        '战神', '赘婿', '种田', '年代', '悬疑', '灵异', '科幻')):
                keyword_set.add(sub)
        if len(keyword_set) >= 16:
            break
    detected_keywords = sorted(keyword_set)[:16]

    # 5) trend_marker 打标
    pop_count = len(agg.get('popular_elements') or [])
    trend_label = '新梗求变' if pop_count >= 6 else ('稳中求变' if pop_count >= 3 else '稳妥保底')
    tone_label = '新梗融合·差异化创新' if trend_label == '新梗求变' else (
        '新梗+情怀融合' if trend_label == '稳中求变' else '经典题材稳盘')

    report = {
        'scanned_at': datetime.now(timezone.utc).isoformat(),
        'platform_default': platform,
        'meta': {
            'matched_categories': matched_labels,
            'detected_gender': gender,
            'detected_keywords': detected_keywords,
        },
        'market_snapshot': {
            'trend_marker': {'label': trend_label, 'tone': tone_label},
            'scanned_sources': source_infos,
        },
        'top_books': all_items[:12],
        'opening_patterns': agg.get('opening_patterns', []),
        'popular_elements': agg.get('popular_elements', []),
        'landmine_elements': agg.get('landmine_elements', []),
        'title_formulas': agg.get('title_formulas', []),
        'golden_finger_types': agg.get('golden_finger_types', []),
        'intro_formulas': agg.get('intro_formulas', []),
        'setting_selling_points': agg.get('setting_selling_points', []),
        'golden_three_patterns': agg.get('golden_three_patterns', []),
        'rank_aggregate_label': (
            f"{'番茄' if platform == 'fanqie' else '起点'}·新书榜 × {len(matched_labels)}个"
            f"{'/'.join(matched_labels[:2]) + ('…' if len(matched_labels) > 2 else '')}"
        ),
    }

    # 缓存 5 分钟
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE[cache_key] = (time.time(), report)
        if len(_SCAN_CACHE) > 200:
            try:
                oldest = min(_SCAN_CACHE.items(), key=lambda kv: kv[1][0])
                _SCAN_CACHE.pop(oldest[0], None)
            except Exception:
                pass
    return {'ok': True, 'from_cache': False, 'report': report}


@novel_rank_bp.route('/api/rank/scan-for-concept', methods=['POST'])
@login_required
def api_rank_scan_for_concept():
    """智驾入口：根据构想，匹配同题材新书榜 → 抓取 TopN → LLM 抽市场情报 → 返回 RankScanReport。
    Request JSON：
      concept: str             必填
      platform?: 'fanqie'|'qidian' 默认 fanqie；用户指定起点时扫起点大盘新书榜
      gender?:  'male'|'female'  可选，不填则根据关键词粗判
      book_id?: str             可选（前端传了可用于后续落地缓存）
      session_id?: str          可选
      top_n_categories?: int    默认 3
      force?: bool              默认 false；true 时忽略缓存
    """
    body = request.get_json(silent=True) or {}
    concept = _clean(body.get('concept') or '')
    if len(concept) < 4:
        return jsonify({'error': '构想太短，不足以匹配同类题材（至少 4 字）'}), 400
    platform = (body.get('platform') or 'fanqie').strip() or 'fanqie'
    gender = body.get('gender') or None
    top_n = max(1, min(5, int(body.get('top_n_categories') or 3)))
    force = bool(body.get('force'))

    result = _core_rank_scan_for_concept(concept, platform, gender, top_n, force)
    if not result.get('ok'):
        return jsonify({'error': result.get('error') or '扫榜失败'}), 400
    return jsonify({'ok': True, **{k: v for k, v in result.items() if k != 'ok'}})


# =============================================================================
# 榜单风向 banner 兼容接口：/api/rankings
#   - 前端 ToolsPage「榜单风向」Tab 顶部的热词 banner（hot_tags / rising_keywords）
#   - 只返回静态度风向数据；书籍列表已由 /api/rank/list V2 接口承载
#   - 原 app.py 中的 legacy 抓取器（_crawl_fanqie/_crawl_qimao/_get_rank_books 等）
#     已被 novel_rank_crawlers.py 的套件完整取代，此处不再重复抓取（去重/降臃肿）
# =============================================================================
_RANKING_DATA = {
    'qidian': {
        'platform': '起点中文网', 'icon': '🏯', 'note': '男频传统市场风向',
        'trend_marker': {'label': '稳中求变', 'tone': '新梗融合 · 情怀翻新'},
        'hot_tags': ['都市异能', '玄幻女婿', '重生商战', '无限流', '轻悬疑'],
        'rising_keywords': ['国运', '规则怪谈', '位面穿梭', '投资博弈', '反派逆袭'],
        'hot_genres': ['都市', '玄幻', '游戏', '科幻', '历史'],
        'examples': [
            {'title': '都市：从投资开始解惑', 'tag': '都市 · 投资博弈', 'point': '金融博弈+草根翻身'},
            {'title': '我靠反派身份躺平', 'tag': '玄幻 · 反套路', 'point': '反套路人设红利'},
            {'title': '规则怪谈：我在规则里求生', 'tag': '悬疑 · 规则流', 'point': '规则怪谈持续高热'},
        ],
        'advice': '起点读者偏爱"强逻辑+长线布局"。建议把流行元素做"融合创新"而非跟风复制，聚焦在一个扎实的爽点上持续放大。'
    },
    'fanqie': {
        'platform': '番茄小说', 'icon': '🍅', 'note': '免费流量 · 快节奏爽点',
        'trend_marker': {'label': '轻快迭代', 'tone': '开局即高潮 · 短平快'},
        'hot_tags': ['赘婿', '战神', '神豪', '神医', '末世复苏'],
        'rising_keywords': ['奶爸', '千亿首富', '觉醒系统', '直播', '打脸'],
        'hot_genres': ['都市', '玄幻', '军婚', '豪门', '星际'],
        'examples': [
            {'title': '开局觉醒神级签到', 'tag': '系统 · 签到流', 'point': '开局金手指马上爽'},
            {'title': '神医奶爸：下山救美', 'tag': '都市 · 神医奶爸', 'point': '反差人设+日常爽点'},
            {'title': '末世：我能无限进化', 'tag': '末世 · 进化流', 'point': '末世题材复活热'},
        ],
        'advice': '番茄读者耐心有限，前3000字必须给出第一个爽点。标题直给、章节留钩子，用高频"打脸"节奏驱动追更。'
    },
    'qimao': {
        'platform': '七猫中文网', 'icon': '🐱', 'note': '下沉+免费 · 强情绪强冲突',
        'trend_marker': {'label': '情绪出口', 'tone': '复仇/逆袭/家人羁绊'},
        'hot_tags': ['豪门', '复仇', '满级大佬', '女频爽文', '马甲文'],
        'rising_keywords': ['认亲', '隐藏身份', '打脸绿茶', '一家顶配', '黑马逆袭'],
        'hot_genres': ['女频豪门', '都市', '穿越', '玄幻', '宫斗'],
        'examples': [
            {'title': '满级大佬穿成农家妇', 'tag': '女频 · 满级大佬', 'point': '满级人设+身份反差'},
            {'title': '豪门隐婚：首富妻子掉马甲', 'tag': '女频 · 马甲流', 'point': '马甲掉马情绪拉满'},
            {'title': '重生之复仇千金', 'tag': '女频 · 复仇', 'point': '复仇爽感+家人羁绊'},
        ],
        'advice': '七猫读者看重情绪引爆点，家庭/身份冲突最能引发共鸣和评论互动。开篇快速抛出强冲突，用"掉马甲/被低估"制造持续爽感。'
    },
}


@novel_rank_bp.route('/api/rankings', methods=['GET'])
def api_rankings_banner():
    """榜单风向 banner（旧结构兼容）：只返回静态风向数据，书籍列表走 /api/rank/list。"""
    platform = request.args.get('platform', 'fanqie')
    data = dict(_RANKING_DATA.get(platform))
    if not data:
        return jsonify({'error': '未知平台'}), 400
    data['books'] = []
    data['fetch_ok'] = False
    data['source'] = '内置精选'
    return jsonify(data)


# ---------------------------------------------------------------------------
# 榜单书 → 一键拆书
#  1. GET  /api/rank/book-content      抓取榜单书前 N 章正文（当前仅番茄，SSR+字体解码）
#  2. POST /api/rank/position-analyze  仅凭榜单元数据的「定位轻拆」（全平台，秒出）
# ---------------------------------------------------------------------------
@novel_rank_bp.route('/api/rank/book-content', methods=['GET'])
def api_rank_book_content():
    platform = (request.args.get('platform') or 'fanqie').strip().lower()
    book_id = (request.args.get('bookId') or '').strip()
    book_url = (request.args.get('bookUrl') or '').strip()
    try:
        max_chapters = int(request.args.get('chapters') or 3)
    except (TypeError, ValueError):
        max_chapters = 3
    max_chapters = max(1, min(max_chapters, 30))
    # 章均约 2.5~3.5 千字：按章数放大正文容量（3→2万 / 10→4万 / 30→12万），
    # 不再固定 2 万字导致 10 章以上被静默截断。
    max_chars = min(120000, max(20000, max_chapters * 4000))

    # 免费小说站兜底路线（顶点/笔趣阁系）：按书名(+作者)搜索抓前 N 章。
    # 非番茄平台深拆、或番茄直抓失败的前端 fallback 都走这里。
    if platform == 'web_novel':
        from blueprints.novel_web_crawlers import fetch_web_novel_content
        title = (request.args.get('title') or '').strip()
        author = (request.args.get('author') or '').strip()
        if not title:
            return jsonify({'ok': False, 'code': 'FETCH_FAILED',
                            'error': '缺少书名，无法在免费小说站检索'}), 200
        try:
            result = fetch_web_novel_content(
                title, author=author or None,
                max_chapters=max_chapters, max_chars=max_chars,
            )
        except RuntimeError as e:
            return jsonify({'ok': False, 'code': 'FETCH_FAILED', 'error': str(e)}), 200
        except Exception as e:
            return jsonify({'ok': False, 'code': 'FETCH_ERROR',
                            'error': f'抓取异常：{str(e)[:150]}'}), 200
        return jsonify(result)

    try:
        result = fetch_rank_book_content(
            platform, book_id=book_id, book_url=book_url,
            max_chapters=max_chapters, max_chars=max_chars,
        )
    except ValueError:
        platform_name = next((s['name'] for s in RANK_SITES if s['code'] == platform), platform)
        return jsonify({
            'ok': False,
            'code': 'UNSUPPORTED_PLATFORM',
            'error': f'{platform_name} 的正文受平台版权/反爬限制暂不支持自动抓取，请复制正文到输入框后分析',
        }), 200  # 业务级可预期结果，用 200 + ok=false，前端走「粘贴兜底」而不是抛红
    except RuntimeError as e:
        return jsonify({'ok': False, 'code': 'FETCH_FAILED', 'error': str(e)}), 200
    except Exception as e:  # 网络/解析等意外
        return jsonify({'ok': False, 'code': 'FETCH_ERROR', 'error': f'抓取异常：{e}'}), 200
    return jsonify(result)


_POSITION_ANALYZE_SCHEMA_HINT = (
    '仅输出一个 JSON 对象，不要解释、不要 ```json 包裹。结构固定：\n'
    '{\n'
    '  "positioning": "赛道定位(35字内：题材+细分切口+在榜位次含义)",\n'
    '  "one_liner_hook": "一句话卖点/故事钩子，基于简介反推(40字内)",\n'
    '  "genre_tags": ["标签1","标签2","标签3"],\n'
    '  "target_audience": "目标读者画像(50字内：人群+核心阅读期待)",\n'
    '  "selling_points": ["简介里可辨识的卖点/爽点假设1","...2","...3"],\n'
    '  "opening_hypothesis": ["黄金三章可能的开篇打法1","...2"],\n'
    '  "learnable_points": ["我写同题材可直接借鉴的点1","...2","...3"],\n'
    '  "differentiation": ["可切入的差异化机会1","...2"],\n'
    '  "to_verify": ["以上哪些只是元数据推测、必须读正文验证1","...2"]\n'
    '}\n'
    '要求：全部基于给定的真实榜单数据，不得编造简介中没有的剧情；没有把握就写进 to_verify。'
)


@novel_rank_bp.route('/api/rank/position-analyze', methods=['POST'])
@login_required
def api_rank_position_analyze():
    """榜单元数据 → 定位轻拆（不依赖正文）。输入为榜单 NRItem + 当前赛道热词。"""
    payload = request.get_json(silent=True) or {}
    title = _clean(payload.get('title') or payload.get('bookTitle'))
    intro = _clean(payload.get('intro') or '')
    if not title and not intro:
        return jsonify({'ok': False, 'error': '缺少书名/简介，无法分析'}), 400

    platform_name = next((s['name'] for s in RANK_SITES
                          if s['code'] == (payload.get('platform') or 'fanqie')), '榜单平台')
    lines = [f'平台：{platform_name}']
    if title:
        lines.append(f'书名：《{title}》')
    if payload.get('authorName') or payload.get('author'):
        lines.append(f'作者：{_clean(payload.get("authorName") or payload.get("author"))}')
    cat = ' / '.join(x for x in [_clean(payload.get('categoryName')),
                                 _clean(payload.get('categorySubName'))] if x)
    if cat:
        lines.append(f'分类：{cat}')
    if payload.get('metricText') or payload.get('metricValue'):
        metric = _clean(str(payload.get('metricText') or payload.get('metricValue') or ''))
        mname = _clean(payload.get('metricName') or '热度')
        lines.append(f'{mname}：{metric}')
    if payload.get('rankNo'):
        lines.append(f'当前排名：第{payload.get("rankNo")}名')
    if payload.get('statusText'):
        lines.append(f'状态：{_clean(payload.get("statusText"))}')
    hot_tags = payload.get('hotTags') or []
    if isinstance(hot_tags, list) and hot_tags:
        lines.append('该赛道当前热门标签：' + '、'.join(str(x) for x in hot_tags[:10]))
    if intro:
        lines.append(f'作品简介：\n{intro[:600]}')

    sys_prompt = ('你是番茄/起点资深爆款责编与竞品分析编辑。你手里只有榜单元数据（书名/简介/分类/热度/赛道热词），'
                  '没有正文。请站在"我要写一本同题材新书，该如何对标这本书并做出差异化"的角度，'
                  '给出克制、具体、可落地的判断，并明确标注哪些结论还需读正文验证。')
    messages = [
        {'role': 'system', 'content': sys_prompt},
        {'role': 'user', 'content': '【榜单书籍资料】\n' + '\n'.join(lines) + '\n\n【任务】\n' + _POSITION_ANALYZE_SCHEMA_HINT},
    ]
    data, err = _call_small_llm_json(messages, max_tokens=1600)
    if not data:
        return jsonify({'ok': False, 'error': err or '定位分析失败：请检查 AI 配置后重试'}), 200
    data['ok'] = True
    data['bookTitle'] = title
    return jsonify(data)
