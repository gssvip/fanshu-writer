"""榜单风向 Blueprint（完整移植自 easy-writing: NovelRank 模块）。

包含：
1. 2 平台 × 232 分类 × 83 榜单源（番茄 / 起点；七猫已按产品要求下线）
2. 抓取适配器：番茄 HTML + 私用区字体解码 / 起点移动端 JSON 接口（真实榜单）
3. 4 个对外 API：
   - GET  /api/rank/platforms                     平台列表
   - GET  /api/rank/filters?platform=...          榜单类型 + 男女频 + 分类选项
   - GET  /api/rank/list?sourceId=...&...         榜单书籍列表（实时抓 + 1h 内存缓存 + 熔断）
   - POST /api/rank/crawl?sourceId=...            强制刷新当前榜单

使用方：Fanshu 工具页「📈 榜单风向」Tab。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import Blueprint, jsonify, request

try:
    import warnings as _warnings
    from urllib3.exceptions import InsecureRequestWarning as _InsecureRequestWarning  # type: ignore
    _warnings.filterwarnings('ignore', category=_InsecureRequestWarning)
except Exception:  # pragma: no cover
    _warnings = None  # type: ignore
    _InsecureRequestWarning = None  # type: ignore

# ---------------------------------------------------------------------------
# Blueprint 实例
# ---------------------------------------------------------------------------
novel_rank_bp = Blueprint('novel_rank', __name__)

# ---------------------------------------------------------------------------
# 1. 榜单种子数据：2 站点 × 232 分类 × 83 榜单源（七猫已按产品要求下线）
# ---------------------------------------------------------------------------
RANK_SITES: list[dict[str, Any]] = [
    {"legacyId": 1, "code": "fanqie", "name": "番茄小说网", "baseUrl": "https://fanqienovel.com", "enabled": 1},
    {"legacyId": 2, "code": "qidian", "name": "起点中文网", "baseUrl": "https://www.qidian.com/", "enabled": 1, "remark": "走起点移动端 JSON 接口（真实榜单，支持分类/子类/翻页）"},
    # 七猫（legacyId=3）已被阿里云 WAF 拦截、服务端抓不到实时数据，按产品要求下线，不再对外提供
    {"legacyId": 3, "code": "qimao", "name": "七猫小说网", "baseUrl": "https://www.qimao.com", "enabled": 0},
]

# 以下分类源完全移植自 easy-writing/src/config/rank-sources.ts（RANK_CATEGORIES）。
# 保留 legacyId 与前端 sourceId 对应，便于逐步对齐 easy-writing 的 UI/数据口径。
RANK_CATEGORIES: list[dict[str, Any]] = [
    {"legacyId": 1, "siteCode": "fanqie", "gender": "male", "name": "都市高武", "code": "1_2_1014", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 2, "siteCode": "fanqie", "gender": "male", "name": "玄幻脑洞", "code": "1_2_257", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 3, "siteCode": "fanqie", "gender": "male", "name": "男频衍生", "code": "1_2_1016", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 4, "siteCode": "fanqie", "gender": "male", "name": "西方奇幻", "code": "1_2_1141", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 5, "siteCode": "fanqie", "gender": "male", "name": "东方仙侠", "code": "1_2_1140", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 6, "siteCode": "fanqie", "gender": "male", "name": "科幻末世", "code": "1_2_8", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 7, "siteCode": "fanqie", "gender": "male", "name": "都市日常", "code": "1_2_261", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 8, "siteCode": "fanqie", "gender": "male", "name": "都市修真", "code": "1_2_124", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 9, "siteCode": "fanqie", "gender": "male", "name": "历史古代", "code": "1_2_273", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 10, "siteCode": "fanqie", "gender": "male", "name": "战神赘婿", "code": "1_2_27", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 11, "siteCode": "fanqie", "gender": "male", "name": "都市种田", "code": "1_2_263", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 12, "siteCode": "fanqie", "gender": "male", "name": "传统玄幻", "code": "1_2_258", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 13, "siteCode": "fanqie", "gender": "male", "name": "历史脑洞", "code": "1_2_272", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 14, "siteCode": "fanqie", "gender": "male", "name": "悬疑脑洞", "code": "1_2_539", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 15, "siteCode": "fanqie", "gender": "male", "name": "都市脑洞", "code": "1_2_262", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 16, "siteCode": "fanqie", "gender": "male", "name": "悬疑灵异", "code": "1_2_751", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 17, "siteCode": "fanqie", "gender": "male", "name": "抗战谍战", "code": "1_2_504", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 18, "siteCode": "fanqie", "gender": "male", "name": "游戏体育", "code": "1_2_746", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 19, "siteCode": "fanqie", "gender": "male", "name": "动漫衍生", "code": "1_2_718", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 20, "siteCode": "qidian", "gender": "male", "name": "玄幻", "code": "chanId21", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 21, "siteCode": "qidian", "gender": "male", "name": "都市", "code": "chanId4", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 22, "siteCode": "qidian", "gender": "male", "name": "异术超能", "code": "chanId4-subCateId74", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 23, "siteCode": "qidian", "gender": "male", "name": "仙侠", "code": "chanId22", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 24, "siteCode": "qidian", "gender": "male", "name": "修真文明", "code": "chanId22-subCateId18", "enabled": 1, "sortNo": 0, "parentLegacyId": 23},
    {"legacyId": 25, "siteCode": "qidian", "gender": "male", "name": "东方玄幻", "code": "chanId21-subCateId8", "enabled": 1, "sortNo": 0, "parentLegacyId": 20},
    {"legacyId": 26, "siteCode": "qidian", "gender": "male", "name": "幻想修仙", "code": "chanId22-subCateId44", "enabled": 1, "sortNo": 0, "parentLegacyId": 23},
    {"legacyId": 27, "siteCode": "qidian", "gender": "male", "name": "科幻", "code": "chanId9", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 28, "siteCode": "qidian", "gender": "male", "name": "时空穿梭", "code": "chanId9-subCateId251", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 29, "siteCode": "qidian", "gender": "male", "name": "轻小说", "code": "chanId12", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 30, "siteCode": "qidian", "gender": "male", "name": "原生幻想", "code": "chanId12-subCateId60", "enabled": 1, "sortNo": 0, "parentLegacyId": 29},
    {"legacyId": 31, "siteCode": "qidian", "gender": "male", "name": "异世大陆", "code": "chanId21-subCateId73", "enabled": 1, "sortNo": 0, "parentLegacyId": 20},
    {"legacyId": 32, "siteCode": "qidian", "gender": "male", "name": "历史", "code": "chanId5", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 33, "siteCode": "qidian", "gender": "male", "name": "架空历史", "code": "chanId5-subCateId22", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 34, "siteCode": "qidian", "gender": "male", "name": "恋爱日常", "code": "chanId12-subCateId66", "enabled": 1, "sortNo": 0, "parentLegacyId": 29},
    {"legacyId": 35, "siteCode": "qidian", "gender": "male", "name": "都市生活", "code": "chanId4-subCateId12", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 36, "siteCode": "qidian", "gender": "male", "name": "奇幻", "code": "chanId1", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 37, "siteCode": "qidian", "gender": "male", "name": "剑与魔法", "code": "chanId1-subCateId62", "enabled": 1, "sortNo": 0, "parentLegacyId": 36},
    {"legacyId": 38, "siteCode": "qidian", "gender": "male", "name": "两宋元明", "code": "chanId5-subCateId224", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 39, "siteCode": "qidian", "gender": "male", "name": "都市异能", "code": "chanId4-subCateId16", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 40, "siteCode": "qidian", "gender": "male", "name": "高武世界", "code": "chanId21-subCateId78", "enabled": 1, "sortNo": 0, "parentLegacyId": 20},
    {"legacyId": 41, "siteCode": "qidian", "gender": "male", "name": "古典仙侠", "code": "chanId22-subCateId20101", "enabled": 1, "sortNo": 0, "parentLegacyId": 23},
    {"legacyId": 42, "siteCode": "qidian", "gender": "male", "name": "游戏", "code": "chanId7", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 43, "siteCode": "qidian", "gender": "male", "name": "游戏异界", "code": "chanId7-subCateId240", "enabled": 1, "sortNo": 0, "parentLegacyId": 42},
    {"legacyId": 44, "siteCode": "qidian", "gender": "male", "name": "衍生同人", "code": "chanId12-subCateId281", "enabled": 1, "sortNo": 0, "parentLegacyId": 29},
    {"legacyId": 45, "siteCode": "qidian", "gender": "male", "name": "秦汉三国", "code": "chanId5-subCateId48", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 46, "siteCode": "qidian", "gender": "male", "name": "超级科技", "code": "chanId9-subCateId250", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 47, "siteCode": "qidian", "gender": "male", "name": "武侠", "code": "chanId2", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 48, "siteCode": "qidian", "gender": "male", "name": "国术无双", "code": "chanId2-subCateId206", "enabled": 1, "sortNo": 0, "parentLegacyId": 47},
    {"legacyId": 49, "siteCode": "qidian", "gender": "male", "name": "末世危机", "code": "chanId9-subCateId253", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 50, "siteCode": "qidian", "gender": "male", "name": "诸天无限", "code": "chanId20109", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 51, "siteCode": "qidian", "gender": "male", "name": "无限", "code": "chanId20109-subCateId20110", "enabled": 1, "sortNo": 0, "parentLegacyId": 50},
    {"legacyId": 52, "siteCode": "qidian", "gender": "male", "name": "外国历史", "code": "chanId5-subCateId226", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 53, "siteCode": "qidian", "gender": "male", "name": "两晋隋唐", "code": "chanId5-subCateId222", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 54, "siteCode": "qidian", "gender": "male", "name": "五代十国", "code": "chanId5-subCateId223", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 55, "siteCode": "qidian", "gender": "male", "name": "悬疑", "code": "chanId10", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 56, "siteCode": "qidian", "gender": "male", "name": "诡秘悬疑", "code": "chanId10-subCateId26", "enabled": 1, "sortNo": 0, "parentLegacyId": 55},
    {"legacyId": 57, "siteCode": "qidian", "gender": "male", "name": "神话修真", "code": "chanId22-subCateId207", "enabled": 1, "sortNo": 0, "parentLegacyId": 23},
    {"legacyId": 58, "siteCode": "qidian", "gender": "male", "name": "进化变异", "code": "chanId9-subCateId252", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 59, "siteCode": "qidian", "gender": "male", "name": "未来世界", "code": "chanId9-subCateId25", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 60, "siteCode": "qimao", "gender": "male", "name": "都市", "code": "a-203", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 61, "siteCode": "qimao", "gender": "male", "name": "都市高武", "code": "a-203-219", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 62, "siteCode": "qimao", "gender": "male", "name": "都市高手", "code": "a-203-220", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 63, "siteCode": "qimao", "gender": "male", "name": "历史", "code": "a-56", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 64, "siteCode": "qimao", "gender": "male", "name": "架空历史", "code": "a-56-58", "enabled": 1, "sortNo": 0, "parentLegacyId": 63},
    {"legacyId": 65, "siteCode": "qimao", "gender": "male", "name": "官场", "code": "a-203-315", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 66, "siteCode": "qimao", "gender": "male", "name": "商战职场", "code": "a-203-221", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 67, "siteCode": "qimao", "gender": "male", "name": "玄幻奇幻", "code": "a-202", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 68, "siteCode": "qimao", "gender": "male", "name": "东方玄幻", "code": "a-202-37", "enabled": 1, "sortNo": 0, "parentLegacyId": 67},
    {"legacyId": 69, "siteCode": "qimao", "gender": "male", "name": "都市生活", "code": "a-203-223", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 70, "siteCode": "qimao", "gender": "male", "name": "异世大陆", "code": "a-202-39", "enabled": 1, "sortNo": 0, "parentLegacyId": 67},
    {"legacyId": 71, "siteCode": "qimao", "gender": "male", "name": "乡村生活", "code": "a-203-48", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 72, "siteCode": "qimao", "gender": "male", "name": "科幻", "code": "a-64", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 73, "siteCode": "qimao", "gender": "male", "name": "末世危机", "code": "a-64-66", "enabled": 1, "sortNo": 0, "parentLegacyId": 72},
    {"legacyId": 74, "siteCode": "qimao", "gender": "male", "name": "热血校园", "code": "a-203-222", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 75, "siteCode": "qimao", "gender": "male", "name": "武侠仙侠", "code": "a-205", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 76, "siteCode": "qimao", "gender": "male", "name": "幻想修真", "code": "a-205-225", "enabled": 1, "sortNo": 0, "parentLegacyId": 75},
    {"legacyId": 77, "siteCode": "qimao", "gender": "male", "name": "游戏", "code": "a-75", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 78, "siteCode": "qimao", "gender": "male", "name": "虚拟网游", "code": "a-75-232", "enabled": 1, "sortNo": 0, "parentLegacyId": 77},
    {"legacyId": 79, "siteCode": "qimao", "gender": "male", "name": "奇闻异事", "code": "a-204", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 80, "siteCode": "qimao", "gender": "male", "name": "奇门秘术", "code": "a-204-231", "enabled": 1, "sortNo": 0, "parentLegacyId": 79},
    {"legacyId": 81, "siteCode": "qimao", "gender": "male", "name": "穿越历史", "code": "a-56-57", "enabled": 1, "sortNo": 0, "parentLegacyId": 63},
    {"legacyId": 82, "siteCode": "qidian", "gender": "male", "name": "商战职场", "code": "chanId4-subCateId153", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 83, "siteCode": "qidian", "gender": "male", "name": "娱乐明星", "code": "chanId4-subCateId151", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 84, "siteCode": "qimao", "gender": "male", "name": "明星娱乐", "code": "a-203-46", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 85, "siteCode": "qidian", "gender": "male", "name": "诸天", "code": "chanId20109-subCateId20111", "enabled": 1, "sortNo": 0, "parentLegacyId": 50},
    {"legacyId": 86, "siteCode": "fanqie", "gender": "male", "name": "西方奇幻", "code": "1_1_1141", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 87, "siteCode": "fanqie", "gender": "male", "name": "东方仙侠", "code": "1_1_1140", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 88, "siteCode": "fanqie", "gender": "male", "name": "科幻末世", "code": "1_1_8", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 89, "siteCode": "fanqie", "gender": "male", "name": "都市日常", "code": "1_1_261", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 90, "siteCode": "fanqie", "gender": "male", "name": "都市修真", "code": "1_1_124", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 91, "siteCode": "fanqie", "gender": "male", "name": "都市高武", "code": "1_1_1014", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 92, "siteCode": "fanqie", "gender": "male", "name": "历史古代", "code": "1_1_273", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 93, "siteCode": "fanqie", "gender": "male", "name": "战神赘婿", "code": "1_1_27", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 94, "siteCode": "fanqie", "gender": "male", "name": "都市种田", "code": "1_1_263", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 95, "siteCode": "fanqie", "gender": "male", "name": "传统玄幻", "code": "1_1_258", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 96, "siteCode": "fanqie", "gender": "male", "name": "历史脑洞", "code": "1_1_272", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 97, "siteCode": "fanqie", "gender": "male", "name": "悬疑脑洞", "code": "1_1_539", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 98, "siteCode": "fanqie", "gender": "male", "name": "都市脑洞", "code": "1_1_262", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 99, "siteCode": "fanqie", "gender": "male", "name": "玄幻脑洞", "code": "1_1_257", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 100, "siteCode": "fanqie", "gender": "male", "name": "悬疑灵异", "code": "1_1_751", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 101, "siteCode": "fanqie", "gender": "male", "name": "抗战谍战", "code": "1_1_504", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 102, "siteCode": "fanqie", "gender": "male", "name": "游戏体育", "code": "1_1_746", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 103, "siteCode": "fanqie", "gender": "male", "name": "动漫衍生", "code": "1_1_718", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 104, "siteCode": "fanqie", "gender": "male", "name": "男频衍生", "code": "1_1_1016", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 105, "siteCode": "qidian", "gender": "male", "name": "短篇", "code": "chanId20076", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 106, "siteCode": "qidian", "gender": "male", "name": "军事", "code": "chanId6", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 107, "siteCode": "qidian", "gender": "male", "name": "现实", "code": "chanId15", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 108, "siteCode": "qidian", "gender": "male", "name": "体育", "code": "chanId8", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 109, "siteCode": "qidian", "gender": "male", "name": "史诗奇幻", "code": "chanId1-subCateId201", "enabled": 1, "sortNo": 0, "parentLegacyId": 36},
    {"legacyId": 110, "siteCode": "qidian", "gender": "male", "name": "清史民国", "code": "chanId5-subCateId225", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 111, "siteCode": "qidian", "gender": "male", "name": "电子竞技", "code": "chanId7-subCateId7", "enabled": 1, "sortNo": 0, "parentLegacyId": 42},
    {"legacyId": 112, "siteCode": "qidian", "gender": "male", "name": "武侠同人", "code": "chanId2-subCateId20100", "enabled": 1, "sortNo": 0, "parentLegacyId": 47},
    {"legacyId": 113, "siteCode": "qidian", "gender": "male", "name": "星际文明", "code": "chanId9-subCateId68", "enabled": 1, "sortNo": 0, "parentLegacyId": 27},
    {"legacyId": 114, "siteCode": "qidian", "gender": "male", "name": "短故事", "code": "chanId20076-subCateId20113", "enabled": 1, "sortNo": 0, "parentLegacyId": 105},
    {"legacyId": 115, "siteCode": "qidian", "gender": "male", "name": "军旅生涯", "code": "chanId6-subCateId54", "enabled": 1, "sortNo": 0, "parentLegacyId": 106},
    {"legacyId": 116, "siteCode": "qidian", "gender": "male", "name": "时代叙事", "code": "chanId15-subCateId20106", "enabled": 1, "sortNo": 0, "parentLegacyId": 107},
    {"legacyId": 117, "siteCode": "qidian", "gender": "male", "name": "侦探推理", "code": "chanId10-subCateId57", "enabled": 1, "sortNo": 0, "parentLegacyId": 55},
    {"legacyId": 118, "siteCode": "qidian", "gender": "male", "name": "另类幻想", "code": "chanId1-subCateId20093", "enabled": 1, "sortNo": 0, "parentLegacyId": 36},
    {"legacyId": 119, "siteCode": "qidian", "gender": "male", "name": "足球运动", "code": "chanId8-subCateId82", "enabled": 1, "sortNo": 0, "parentLegacyId": 108},
    {"legacyId": 120, "siteCode": "qidian", "gender": "male", "name": "传统武侠", "code": "chanId2-subCateId5", "enabled": 1, "sortNo": 0, "parentLegacyId": 47},
    {"legacyId": 121, "siteCode": "qimao", "gender": "male", "name": "军事", "code": "a-60", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 122, "siteCode": "qimao", "gender": "male", "name": "N次元", "code": "a-207", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 123, "siteCode": "qimao", "gender": "male", "name": "现代军事", "code": "a-60-61", "enabled": 1, "sortNo": 0, "parentLegacyId": 121},
    {"legacyId": 124, "siteCode": "qimao", "gender": "male", "name": "恐怖灵异", "code": "a-204-229", "enabled": 1, "sortNo": 0, "parentLegacyId": 79},
    {"legacyId": 125, "siteCode": "qimao", "gender": "male", "name": "衍生同人", "code": "a-207-239", "enabled": 1, "sortNo": 0, "parentLegacyId": 122},
    {"legacyId": 126, "siteCode": "qimao", "gender": "male", "name": "王朝争霸", "code": "a-202-218", "enabled": 1, "sortNo": 0, "parentLegacyId": 67},
    {"legacyId": 127, "siteCode": "qimao", "gender": "male", "name": "古典仙侠", "code": "a-205-52", "enabled": 1, "sortNo": 0, "parentLegacyId": 75},
    {"legacyId": 128, "siteCode": "qimao", "gender": "male", "name": "未来幻想", "code": "a-64-67", "enabled": 1, "sortNo": 0, "parentLegacyId": 72},
    {"legacyId": 129, "siteCode": "qimao", "gender": "male", "name": "上古洪荒", "code": "a-205-38", "enabled": 1, "sortNo": 0, "parentLegacyId": 75},
    {"legacyId": 130, "siteCode": "qimao", "gender": "male", "name": "电子竞技", "code": "a-75-78", "enabled": 1, "sortNo": 0, "parentLegacyId": 77},
    {"legacyId": 131, "siteCode": "qidian", "gender": "male", "name": "武侠幻想", "code": "chanId2-subCateId30", "enabled": 1, "sortNo": 0, "parentLegacyId": 47},
    {"legacyId": 132, "siteCode": "qidian", "gender": "male", "name": "上古先秦", "code": "chanId5-subCateId220", "enabled": 1, "sortNo": 0, "parentLegacyId": 32},
    {"legacyId": 133, "siteCode": "qimao", "gender": "male", "name": "灵气复苏", "code": "a-203-314", "enabled": 1, "sortNo": 0, "parentLegacyId": 60},
    {"legacyId": 134, "siteCode": "qidian", "gender": "male", "name": "王朝争霸", "code": "chanId21-subCateId58", "enabled": 1, "sortNo": 0, "parentLegacyId": 20},
    {"legacyId": 135, "siteCode": "qidian", "gender": "male", "name": "现代修真", "code": "chanId22-subCateId64", "enabled": 1, "sortNo": 0, "parentLegacyId": 23},
    {"legacyId": 136, "siteCode": "qidian", "gender": "male", "name": "人物传记", "code": "chanId20076-subCateId20098", "enabled": 1, "sortNo": 0, "parentLegacyId": 105},
    {"legacyId": 137, "siteCode": "qidian", "gender": "male", "name": "社会悬疑", "code": "chanId15-subCateId20105", "enabled": 1, "sortNo": 0, "parentLegacyId": 107},
    {"legacyId": 138, "siteCode": "qidian", "gender": "male", "name": "综漫", "code": "chanId20109-subCateId20112", "enabled": 1, "sortNo": 0, "parentLegacyId": 50},
    {"legacyId": 139, "siteCode": "qidian", "gender": "male", "name": "青春校园", "code": "chanId4-subCateId130", "enabled": 1, "sortNo": 0, "parentLegacyId": 21},
    {"legacyId": 140, "siteCode": "qidian", "gender": "male", "name": "虚拟网游", "code": "chanId7-subCateId70", "enabled": 1, "sortNo": 0, "parentLegacyId": 42},
    {"legacyId": 141, "siteCode": "qidian", "gender": "male", "name": "游戏系统", "code": "chanId7-subCateId20102", "enabled": 1, "sortNo": 0, "parentLegacyId": 42},
    {"legacyId": 142, "siteCode": "qidian", "gender": "male", "name": "青年故事", "code": "chanId15-subCateId20108", "enabled": 1, "sortNo": 0, "parentLegacyId": 107},
    {"legacyId": 143, "siteCode": "fanqie", "gender": "female", "name": "古风世情", "code": "0_2_1139", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 144, "siteCode": "fanqie", "gender": "female", "name": "科幻末世", "code": "0_2_8", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 145, "siteCode": "fanqie", "gender": "female", "name": "游戏体育", "code": "0_2_746", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 146, "siteCode": "fanqie", "gender": "female", "name": "女频衍生", "code": "0_2_1015", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 147, "siteCode": "fanqie", "gender": "female", "name": "玄幻言情", "code": "0_2_248", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 148, "siteCode": "fanqie", "gender": "female", "name": "种田", "code": "0_2_23", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 149, "siteCode": "fanqie", "gender": "female", "name": "年代", "code": "0_2_79", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 150, "siteCode": "fanqie", "gender": "female", "name": "现言脑洞", "code": "0_2_267", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 151, "siteCode": "fanqie", "gender": "female", "name": "宫斗宅斗", "code": "0_2_246", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 152, "siteCode": "fanqie", "gender": "female", "name": "悬疑脑洞", "code": "0_2_539", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 153, "siteCode": "fanqie", "gender": "female", "name": "古言脑洞", "code": "0_2_253", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 154, "siteCode": "fanqie", "gender": "female", "name": "快穿", "code": "0_2_24", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 155, "siteCode": "fanqie", "gender": "female", "name": "青春甜宠", "code": "0_2_749", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 156, "siteCode": "fanqie", "gender": "female", "name": "星光璀璨", "code": "0_2_745", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 157, "siteCode": "fanqie", "gender": "female", "name": "女频悬疑", "code": "0_2_747", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 158, "siteCode": "fanqie", "gender": "female", "name": "职场婚恋", "code": "0_2_750", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 159, "siteCode": "fanqie", "gender": "female", "name": "豪门总裁", "code": "0_2_748", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 160, "siteCode": "fanqie", "gender": "female", "name": "民国言情", "code": "0_2_1017", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 161, "siteCode": "fanqie", "gender": "female", "name": "古风世情", "code": "0_1_1139", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 162, "siteCode": "fanqie", "gender": "female", "name": "科幻末世", "code": "0_1_8", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 163, "siteCode": "fanqie", "gender": "female", "name": "游戏体育", "code": "0_1_746", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 164, "siteCode": "fanqie", "gender": "female", "name": "女频衍生", "code": "0_1_1015", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 165, "siteCode": "fanqie", "gender": "female", "name": "玄幻言情", "code": "0_1_248", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 166, "siteCode": "fanqie", "gender": "female", "name": "种田", "code": "0_1_23", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 167, "siteCode": "fanqie", "gender": "female", "name": "年代", "code": "0_1_79", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 168, "siteCode": "fanqie", "gender": "female", "name": "现言脑洞", "code": "0_1_267", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 169, "siteCode": "fanqie", "gender": "female", "name": "宫斗宅斗", "code": "0_1_246", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 170, "siteCode": "fanqie", "gender": "female", "name": "悬疑脑洞", "code": "0_1_539", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 171, "siteCode": "fanqie", "gender": "female", "name": "古言脑洞", "code": "0_1_253", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 172, "siteCode": "fanqie", "gender": "female", "name": "快穿", "code": "0_1_24", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 173, "siteCode": "fanqie", "gender": "female", "name": "青春甜宠", "code": "0_1_749", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 174, "siteCode": "fanqie", "gender": "female", "name": "星光璀璨", "code": "0_1_745", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 175, "siteCode": "fanqie", "gender": "female", "name": "女频悬疑", "code": "0_1_747", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 176, "siteCode": "fanqie", "gender": "female", "name": "职场婚恋", "code": "0_1_750", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 177, "siteCode": "fanqie", "gender": "female", "name": "豪门总裁", "code": "0_1_748", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
    {"legacyId": 178, "siteCode": "fanqie", "gender": "female", "name": "民国言情", "code": "0_1_1017", "enabled": 1, "sortNo": 0, "parentLegacyId": None},
]

# 83 榜单源（完全移植 easy-writing:rank-sources.ts RANK_SOURCES）
RANK_SOURCES: list[dict[str, Any]] = [
    # 番茄-男频-阅读榜（19分类） + 新书榜（19分类） = 38源
    {"legacyId": 1, "siteCode": "fanqie", "categoryLegacyId": 1, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_1014", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 2, "siteCode": "fanqie", "categoryLegacyId": 2, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_257", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 3, "siteCode": "fanqie", "categoryLegacyId": 3, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_1016", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 4, "siteCode": "fanqie", "categoryLegacyId": 4, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_1141", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 5, "siteCode": "fanqie", "categoryLegacyId": 5, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_1140", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 6, "siteCode": "fanqie", "categoryLegacyId": 6, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_8", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 7, "siteCode": "fanqie", "categoryLegacyId": 7, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_261", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 8, "siteCode": "fanqie", "categoryLegacyId": 8, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_124", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 9, "siteCode": "fanqie", "categoryLegacyId": 9, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_273", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 10, "siteCode": "fanqie", "categoryLegacyId": 10, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_27", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 11, "siteCode": "fanqie", "categoryLegacyId": 11, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_263", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 12, "siteCode": "fanqie", "categoryLegacyId": 12, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_258", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 13, "siteCode": "fanqie", "categoryLegacyId": 13, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_272", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 14, "siteCode": "fanqie", "categoryLegacyId": 14, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_539", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 15, "siteCode": "fanqie", "categoryLegacyId": 15, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_262", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 16, "siteCode": "fanqie", "categoryLegacyId": 16, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_751", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 17, "siteCode": "fanqie", "categoryLegacyId": 17, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_504", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 18, "siteCode": "fanqie", "categoryLegacyId": 18, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_746", "enabled": 1, "meta": {"maxPages": 2}},
    {"legacyId": 19, "siteCode": "fanqie", "categoryLegacyId": 19, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/1_2_718", "enabled": 1, "meta": {"maxPages": 2}},
    # 番茄男频新书榜 19（legacyId 26-44）
    {"legacyId": 26, "siteCode": "fanqie", "categoryLegacyId": 86, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_1141", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 27, "siteCode": "fanqie", "categoryLegacyId": 87, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_1140", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 28, "siteCode": "fanqie", "categoryLegacyId": 88, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_8", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 29, "siteCode": "fanqie", "categoryLegacyId": 89, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_261", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 30, "siteCode": "fanqie", "categoryLegacyId": 90, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_124", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 31, "siteCode": "fanqie", "categoryLegacyId": 91, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_1014", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 32, "siteCode": "fanqie", "categoryLegacyId": 92, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_273", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 33, "siteCode": "fanqie", "categoryLegacyId": 93, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_27", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 34, "siteCode": "fanqie", "categoryLegacyId": 94, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_263", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 35, "siteCode": "fanqie", "categoryLegacyId": 95, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_258", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 36, "siteCode": "fanqie", "categoryLegacyId": 96, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_272", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 37, "siteCode": "fanqie", "categoryLegacyId": 97, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_539", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 38, "siteCode": "fanqie", "categoryLegacyId": 98, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_262", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 39, "siteCode": "fanqie", "categoryLegacyId": 99, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_257", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 40, "siteCode": "fanqie", "categoryLegacyId": 100, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_751", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 41, "siteCode": "fanqie", "categoryLegacyId": 101, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_504", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 42, "siteCode": "fanqie", "categoryLegacyId": 102, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_746", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 43, "siteCode": "fanqie", "categoryLegacyId": 103, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_718", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    {"legacyId": 44, "siteCode": "fanqie", "categoryLegacyId": 104, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/1_1_1016", "enabled": 1, "meta": {"maxPages": 2, "metricName": "在读"}},
    # 起点（5）+七猫（3）大盘榜 + 起点新书榜（1）— 按需求7 起点只给用户选择「月票榜/新书榜」
    {"legacyId": 23, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "hotsale", "title": "畅销榜", "url": "https://www.qidian.com/rank/hotsales/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "scope": "all"}},
    {"legacyId": 24, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "monthTicket", "title": "月票榜", "url": "https://www.qidian.com/rank/yuepiao/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "metricName": "月票", "scope": "all"}},
    {"legacyId": 47, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "newauthor", "title": "新书榜", "url": "https://www.qidian.com/rank/newauthor/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "scope": "all"}},
    {"legacyId": 84, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "hotsale", "title": "畅销榜", "url": "https://www.qdmm.com/rank/hotsales/", "enabled": 1, "meta": {"gender": "female", "maxPages": 5, "scope": "all"}},
    {"legacyId": 85, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "monthTicket", "title": "月票榜", "url": "https://www.qdmm.com/rank/yuepiao/", "enabled": 1, "meta": {"gender": "female", "maxPages": 5, "metricName": "月票", "scope": "all"}},
    {"legacyId": 25, "siteCode": "qimao", "categoryLegacyId": None, "rankType": "hot", "title": "大热榜", "url": "https://www.qimao.com/qimaoapi/api/rank/book-list?is_girl=0&rank_type=1&date_type=1&date=&page=1", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "metricName": "热度", "scope": "all"}},
    {"legacyId": 45, "siteCode": "qimao", "categoryLegacyId": None, "rankType": "new", "title": "新书榜", "url": "https://www.qimao.com/qimaoapi/api/rank/book-list?is_girl=0&rank_type=2&date_type=1&date=&page=1", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "metricName": "热度", "scope": "all"}},
    {"legacyId": 46, "siteCode": "qimao", "categoryLegacyId": None, "rankType": "collect", "title": "收藏榜", "url": "https://www.qimao.com/qimaoapi/api/rank/book-list?is_girl=0&rank_type=4&date_type=1&date=&page=1", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "scope": "all"}},
    {"legacyId": 86, "siteCode": "qimao", "categoryLegacyId": None, "rankType": "hot", "title": "大热榜", "url": "https://www.qimao.com/qimaoapi/api/rank/book-list?is_girl=1&rank_type=1&date_type=1&date=&page=1", "enabled": 1, "meta": {"gender": "female", "maxPages": 5, "metricName": "热度", "scope": "all"}},
    {"legacyId": 87, "siteCode": "qimao", "categoryLegacyId": None, "rankType": "new", "title": "新书榜", "url": "https://www.qimao.com/qimaoapi/api/rank/book-list?is_girl=1&rank_type=2&date_type=1&date=&page=1", "enabled": 1, "meta": {"gender": "female", "maxPages": 5, "metricName": "热度", "scope": "all"}},
    # 番茄-女频-阅读榜（16分类） + 新书榜（16分类）
    {"legacyId": 48, "siteCode": "fanqie", "categoryLegacyId": 143, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_1139", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 49, "siteCode": "fanqie", "categoryLegacyId": 144, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_8", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 50, "siteCode": "fanqie", "categoryLegacyId": 145, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_746", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 51, "siteCode": "fanqie", "categoryLegacyId": 146, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_1015", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 52, "siteCode": "fanqie", "categoryLegacyId": 147, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_248", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 53, "siteCode": "fanqie", "categoryLegacyId": 148, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_23", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 54, "siteCode": "fanqie", "categoryLegacyId": 149, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_79", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 55, "siteCode": "fanqie", "categoryLegacyId": 150, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_267", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 56, "siteCode": "fanqie", "categoryLegacyId": 151, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_246", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 57, "siteCode": "fanqie", "categoryLegacyId": 152, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_539", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 58, "siteCode": "fanqie", "categoryLegacyId": 153, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_253", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 59, "siteCode": "fanqie", "categoryLegacyId": 154, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_24", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 60, "siteCode": "fanqie", "categoryLegacyId": 155, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_749", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 61, "siteCode": "fanqie", "categoryLegacyId": 156, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_745", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 62, "siteCode": "fanqie", "categoryLegacyId": 157, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_747", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 63, "siteCode": "fanqie", "categoryLegacyId": 158, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_750", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 64, "siteCode": "fanqie", "categoryLegacyId": 159, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_748", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 65, "siteCode": "fanqie", "categoryLegacyId": 160, "rankType": "reading", "title": "阅读榜", "url": "https://fanqienovel.com/rank/0_2_1017", "enabled": 1, "meta": {"gender": "female", "maxPages": 2}},
    {"legacyId": 66, "siteCode": "fanqie", "categoryLegacyId": 161, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_1139", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 67, "siteCode": "fanqie", "categoryLegacyId": 162, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_8", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 68, "siteCode": "fanqie", "categoryLegacyId": 163, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_746", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 69, "siteCode": "fanqie", "categoryLegacyId": 164, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_1015", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 70, "siteCode": "fanqie", "categoryLegacyId": 165, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_248", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 71, "siteCode": "fanqie", "categoryLegacyId": 166, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_23", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 72, "siteCode": "fanqie", "categoryLegacyId": 167, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_79", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 73, "siteCode": "fanqie", "categoryLegacyId": 168, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_267", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 74, "siteCode": "fanqie", "categoryLegacyId": 169, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_246", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 75, "siteCode": "fanqie", "categoryLegacyId": 170, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_539", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 76, "siteCode": "fanqie", "categoryLegacyId": 171, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_253", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 77, "siteCode": "fanqie", "categoryLegacyId": 172, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_24", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 78, "siteCode": "fanqie", "categoryLegacyId": 173, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_749", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 79, "siteCode": "fanqie", "categoryLegacyId": 174, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_745", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 80, "siteCode": "fanqie", "categoryLegacyId": 175, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_747", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 81, "siteCode": "fanqie", "categoryLegacyId": 176, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_750", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 82, "siteCode": "fanqie", "categoryLegacyId": 177, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_748", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
    {"legacyId": 83, "siteCode": "fanqie", "categoryLegacyId": 178, "rankType": "new", "title": "新书榜", "url": "https://fanqienovel.com/rank/0_1_1017", "enabled": 1, "meta": {"gender": "female", "maxPages": 2, "metricName": "在读"}},
]

# 榜单类型 meta（给前端展示用）
RANK_TYPE_LABELS: dict[str, str] = {
    'reading': '阅读榜',
    'new': '新书榜',
    'hotsale': '畅销榜',
    'monthTicket': '月票榜',
    'newauthor': '新书榜',
    'hot': '大热榜',
    'collect': '收藏榜',
}

# ---------------------------------------------------------------------------
# 2. 抓取适配层：番茄 HTML + 字体解码 / 七猫 JSON / 起点精选兜底
# ---------------------------------------------------------------------------
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
REQ_HEADERS = {
    'User-Agent': UA,
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# 番茄私用区字体字典
_FQ_DICT: dict[str, str] = {}
try:
    _p = Path(__file__).resolve().parent.parent / 'fanqie_font_dict.json'
    if _p.exists():
        _FQ_DICT = json.loads(_p.read_text(encoding='utf-8'))
except Exception:
    _FQ_DICT = {}


def _decode_fq(text: str) -> str:
    if not text:
        return ''
    out = []
    for ch in text:
        out.append(_FQ_DICT.get(str(ord(ch)), ch))
    return ''.join(out)


def _clean(v: Any) -> str:
    return re.sub(r'\s+', ' ', str(v or '')).strip()


def _parse_cn(s: str) -> int:
    m = re.search(r'([\d.]+)\s*([万亿]?)', s or '')
    if not m:
        return 0
    try:
        v = float(m.group(1))
    except ValueError:
        return 0
    f = 100000000 if m.group(2) == '亿' else (10000 if m.group(2) == '万' else 1)
    return int(round(v * f))


# ---- 共享 requests 会话（TLSv1.2 minimum 兜底 + 连接复用 + Retry + verify 兜底）----
try:  # 兼容老版本 ssl / urllib3
    import ssl as _ssl

    _HAS_TLS_MIN = hasattr(_ssl, 'TLSVersion')
except Exception:  # pragma: no cover
    _ssl = None  # type: ignore
    _HAS_TLS_MIN = False


def _ca_bundle_path() -> str | bool:
    """Render / 企业 / 自签 MITM 环境常会在 certifi / 系统 CA 包下发代理根证书，
    这里按优先级返回一个可验证的 CA 包路径。全失败则返回 True（requests 默认行为）。"""
    import os as _os
    candidates: list[str] = []
    for env_k in ('FANSHU_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE'):
        v = (_os.environ.get(env_k) or '').strip()
        if v:
            candidates.append(v)
    try:
        import certifi as _certifi  # type: ignore
        candidates.append(_certifi.where())
    except Exception:
        pass
    for p in ('/etc/ssl/certs/ca-certificates.crt',
              '/etc/pki/tls/certs/ca-bundle.crt',
              '/etc/ssl/cert.pem'):
        candidates.append(p)
    for c in candidates:
        if not c:
            continue
        try:
            if Path(c).is_file() and Path(c).stat().st_size > 0:
                return c
        except Exception:
            continue
    return True


_CA_BUNDLE: str | bool = _ca_bundle_path()

_FQ_SESSION_LOCK = threading.Lock()
_FQ_SESSION: requests.Session | None = None
_REQ_SESSION: requests.Session | None = None


def _build_retry_adapter(base_adapter_cls: Any, total_retries: int = 3) -> Any:
    """在给定 HTTPAdapter 子类基础上，挂上 Retry 策略并返回实例。"""
    try:
        from requests.adapters import HTTPAdapter as _HTTPAdapter  # noqa: F401
        from urllib3.util.retry import Retry as _Retry
        retry = _Retry(
            total=total_retries,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=('GET', 'HEAD'),
            raise_on_status=False,
        )
        adapter = base_adapter_cls(max_retries=retry, pool_connections=20, pool_maxsize=40)
    except Exception:
        adapter = base_adapter_cls()
    return adapter


def _build_tls_session(verify: str | bool | None = None) -> requests.Session:
    """构造强制 TLSv1.2 以上的 requests Session，修复：
       (a) Python 3.14+ 沙箱默认握手 SSLEOFError；
       (b) Render 出口存在自签 MITM 代理导致 SSLCertVerificationError（证书链中自签证书）。
    策略：
       1. verify 默认 _CA_BUNDLE（certifi/系统/自定义 CA 包路径），最严格验证；
       2. 挂 Retry(total=3, backoff=0.8) 应对握手抖动与 429/5xx；
       3. 若仍 SSL 失败，调用方 _rank_fetch 会用 verify=False 做终极兜底（InsecureRequestWarning 已静默）。"""
    verify_eff = _CA_BUNDLE if verify is None else verify
    s = requests.Session()
    s.verify = verify_eff
    s.headers.update({'User-Agent': REQ_HEADERS.get('User-Agent') or ''})

    try:
        from requests.adapters import HTTPAdapter as _HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context as _create_urllib3_context  # type: ignore

        class _TLS12Adapter(_HTTPAdapter):
            def init_poolmanager(self, *args: Any, **kwargs: Any):
                try:
                    cert_reqs = _ssl.CERT_REQUIRED if _ssl else 2
                    ssl_version = _ssl.PROTOCOL_TLS_CLIENT if _ssl else None
                    ctx = _create_urllib3_context(cert_reqs=cert_reqs, ssl_version=ssl_version)
                    if _ssl and _HAS_TLS_MIN:
                        ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
                    # 显式把 CA 包注入 SSLContext（某些环境 Session.verify 路径不会传递到底层）
                    cafile: str | None = None
                    capath: str | None = None
                    if isinstance(verify_eff, str) and Path(verify_eff).is_file():
                        cafile = verify_eff
                    elif isinstance(verify_eff, bool) and verify_eff:
                        for p in ('/etc/ssl/certs', '/etc/pki/tls/certs'):
                            if Path(p).is_dir():
                                capath = p
                                break
                    if _ssl:
                        try:
                            ctx.load_verify_locations(cafile=cafile, capath=capath)
                        except Exception:
                            pass
                except Exception:
                    ctx = None  # type: ignore
                if ctx is not None:
                    kwargs['ssl_context'] = ctx
                return super().init_poolmanager(*args, **kwargs)

        s.mount('https://', _build_retry_adapter(_TLS12Adapter, total_retries=3))
        s.mount('http://',  _build_retry_adapter(_HTTPAdapter,   total_retries=3))
    except Exception:
        try:
            from requests.adapters import HTTPAdapter as _HTTPAdapter2
            s.mount('https://', _build_retry_adapter(_HTTPAdapter2, total_retries=3))
            s.mount('http://',  _build_retry_adapter(_HTTPAdapter2, total_retries=3))
        except Exception:
            pass
    return s


def _is_ssl_error(e: Exception) -> bool:
    """判断异常是否为证书链 / SSL 握手错误（Render 自签 MITM 属于 CERTIFICATE_VERIFY_FAILED）。"""
    msg = f'{type(e).__name__}:{e}'
    for kw in ('CERTIFICATE_VERIFY_FAILED', 'SSL', 'TLS', 'certificate verify',
               'self-signed certificate', 'CERTIFICATE_CHAIN', 'SSLCertVerificationError',
               'SSLError', 'UNSAFE_LEGACY_RENEGOTIATION', 'handshake failure'):
        if kw in msg:
            return True
    return False


def _session_get(sess: requests.Session, url: str, *,
                 headers: dict[str, str] | None = None,
                 params: dict[str, Any] | None = None,
                 timeout: int = 20,
                 allow_verify_fallback: bool = True) -> requests.Response:
    """session.get 包装：正常（verify=CA/严格）→ 若遇到 SSL/CERT 错误 → 自动 fallback 一次 verify=False。
    Render / 企业代理自签环境时，首次严格握手会命中 SSLCertVerificationError；fallback 后即可过。"""
    try:
        return sess.get(url, headers=headers, params=params, timeout=timeout)
    except Exception as e:
        if allow_verify_fallback and _is_ssl_error(e):
            # verify=False 终极兜底（InsecureRequestWarning 已在模块开头被静默）
            return sess.get(url, headers=headers, params=params, timeout=timeout, verify=False)
        raise


def _get_req_session() -> requests.Session:
    global _REQ_SESSION
    if _REQ_SESSION is None:
        with _FQ_SESSION_LOCK:
            if _REQ_SESSION is None:
                _REQ_SESSION = _build_tls_session()
    return _REQ_SESSION


def _get_fq_session(referer: str | None = None) -> requests.Session:
    """返回已预热的番茄抓取 Session（尽量共享，避免 TLS 重连导致服务端风控 / 握手失败）。
    预热阶段同样走 verify=False 自动兜底，防止 Render 出口自签 MITM 在预热阶段就挂掉。"""
    global _FQ_SESSION
    if _FQ_SESSION is None:
        with _FQ_SESSION_LOCK:
            if _FQ_SESSION is None:
                sess = _build_tls_session()
                try:
                    _session_get(
                        sess,
                        'https://fanqienovel.com/',
                        headers={'Accept-Language': 'zh-CN,zh;q=0.9', **{k: v for k, v in REQ_HEADERS.items() if k != 'User-Agent'}},
                        timeout=20,
                    )
                    _session_get(
                        sess,
                        'https://fanqienovel.com/rank/1_1',
                        headers={'Accept-Language': 'zh-CN,zh;q=0.9'},
                        timeout=20,
                    )
                except Exception:
                    pass
                _FQ_SESSION = sess
    sess = _FQ_SESSION
    if referer:
        sess.headers['Referer'] = referer
    return sess


def _rank_fetch(url: str, referer: str | None = None, timeout: int = 20) -> str:
    # 番茄域名走预热过的会话（TLSv1.2 兜底 + 连接池复用 + Referer 匹配）；其它域名走通用 TLS 适配会话
    if 'fanqienovel.com' in url:
        sess = _get_fq_session(referer or 'https://fanqienovel.com/')
    else:
        sess = _get_req_session()
    h = dict(REQ_HEADERS)
    if referer:
        h['Referer'] = referer
    r = _session_get(sess, url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.text


def _pua_count(text: str) -> int:
    return sum(1 for ch in (text or '') if 0xE000 <= (ord(ch) or 0) <= 0xF8FF)


# ---- 番茄 HTML 解析（同 easy-writing parseFanqieRankHtml，BeautifulSoup 版） ----
def parse_fanqie_html(html: str, category_name: str | None = None) -> dict[str, Any]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        raise RuntimeError('缺少依赖 bs4，请安装 beautifulsoup4')

    soup = BeautifulSoup(html, 'html.parser')

    header = soup.select_one('.muye-rank-wrap-header')
    page_title = _decode_fq(header.select_one('h1').get_text(' ', strip=True)) if header else ''
    cutoff = _decode_fq(header.select_one('p').get_text(' ', strip=True)) if header and header.select_one('p') else ''

    # __INITIAL_STATE__ 里拿封面映射 + 完整 book_list（SSR HTML 只有前10条，state 里通常有 20 条）
    cover_map: dict[str, str] = {}
    state_items: list[dict[str, Any]] = []
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>', html, re.S)
    if m:
        try:
            state = json.loads(m.group(1))
            raw_items = (state.get('rank', {}).get('book_list') or state.get('rank', {}).get('bookList') or [])
            for item in raw_items:
                bid = str(item.get('bookId') or item.get('book_id') or '').strip()
                thumb = str(item.get('thumbUri') or item.get('cover') or '').strip()
                if bid and thumb:
                    if thumb.startswith('//'):
                        cover_map[bid] = 'https:' + thumb
                    elif thumb.startswith('http'):
                        cover_map[bid] = thumb
                    else:
                        cover_map[bid] = 'https://' + thumb.lstrip('/')
            # state 条目作为主数据（若 SSR 不完整），只取字段能映射的基础信息，保留 SSR 的详情优先
            state_items = list(raw_items)
        except Exception:
            pass

    items: list[dict[str, Any]] = []
    for idx, el in enumerate(soup.select('.muye-rank-book-list .rank-book-item')):
        def _sel_text(sel: str) -> str:
            n = el.select_one(sel)
            return _clean(_decode_fq(n.get_text(' ', strip=True) if n else ''))

        rank_s = _sel_text('.book-item-index h1')
        rank_no = int(rank_s) if rank_s.isdigit() else idx + 1

        # rankChange
        change_p = el.select_one('.book-item-index p')
        change_abs = 0
        if change_p:
            ca = _clean(_decode_fq(change_p.get_text(' ', strip=True)))
            try:
                change_abs = int(ca)
            except Exception:
                change_abs = 0
        rank_change = 0
        if change_p:
            if change_p.select_one('.up'):
                rank_change = change_abs
            elif change_p.select_one('.down'):
                rank_change = -change_abs

        book_anchor = el.select_one('.title a')
        book_title = _clean(_decode_fq(book_anchor.get_text(' ', strip=True) if book_anchor else ''))
        book_path = (book_anchor.get('href') if book_anchor else '') or ''
        book_url = 'https://fanqienovel.com' + book_path if book_path.startswith('/') else book_path
        if not book_title or not book_url:
            continue
        book_id = None
        bm = re.search(r'/page/(\d+)', book_path)
        if bm:
            book_id = bm.group(1)

        author_el = el.select_one('.author a span')
        author_name = _clean(_decode_fq(author_el.get_text(' ', strip=True) if author_el else '')) or None

        cover_url = ''
        if book_id and cover_map.get(book_id):
            cover_url = cover_map[book_id]
        else:
            img = el.select_one('.book-cover img')
            if img:
                cover_url = (img.get('data-src') or img.get('data-original') or img.get('src') or '').strip()
                if cover_url.startswith('//'):
                    cover_url = 'https:' + cover_url

        reading_text = _sel_text('.book-item-count') or None
        last_ch_el = el.select_one('.book-item-footer-last a.chapter')
        last_ch_title = _clean(_decode_fq(_clean(last_ch_el.get_text(' ', strip=True)).replace('最近更新：', '') if last_ch_el else '')) or None
        last_ch_url = None
        if last_ch_el and last_ch_el.get('href'):
            lp = last_ch_el['href']
            last_ch_url = ('https://fanqienovel.com' + lp) if str(lp).startswith('/') else lp

        status_text = _sel_text('.book-item-footer-status') or None
        intro = _sel_text('.desc') or None

        items.append({
            'rankNo': rank_no,
            'rankChange': rank_change,
            'bookTitle': book_title,
            'bookId': book_id,
            'bookUrl': book_url,
            'authorName': author_name,
            'coverUrl': cover_url,
            'intro': intro,
            'statusText': status_text,
            'readingText': reading_text,
            'readingCount': _parse_cn(reading_text or ''),
            'metricName': '在读',
            'metricText': reading_text,
            'metricValue': _parse_cn(reading_text or ''),
            'lastChapterTitle': last_ch_title,
            'lastChapterUrl': last_ch_url,
            'lastUpdateTimeText': _sel_text('.book-item-footer-time') or None,
            'categoryName': category_name,
        })

    # --- state book_list 补全：SSR 只有前 10 条，state 通常有 20 条，逐本合并/补齐 ---
    if state_items:
        exist_ids: set[str] = {str(it['bookId'] or '') for it in items if it.get('bookId')}
        for si in state_items:
            bid = str(si.get('bookId') or si.get('book_id') or '').strip()
            if not bid or bid in exist_ids:
                continue
            # 书名/作者：番茄 state 里 bookName/author 是字体编码（反爬），但 SSR 里没有超过10号之后的内容。
            # 先取可读字段：书名 bookName（若可解）/ 作者 author / 分类 categoryName / 在读 reading
            title_raw = _clean(_decode_fq(si.get('bookName') or si.get('title') or ''))
            if not title_raw:
                continue
            author_raw = _clean(_decode_fq(si.get('author') or '')) or None
            reading_raw = si.get('readingCount') or si.get('reading_num') or si.get('reading')
            if reading_raw is None:
                reading_text = None
            else:
                reading_text = str(reading_raw)
            cover = cover_map.get(bid, '')
            thumb = str(si.get('thumbUri') or si.get('cover') or '').strip()
            if not cover and thumb:
                if thumb.startswith('//'): cover = 'https:' + thumb
                elif thumb.startswith('http'): cover = thumb
                else: cover = 'https://' + thumb.lstrip('/')
            book_path = si.get('url') or si.get('book_url') or f'/page/{bid}'
            if isinstance(book_path, str) and book_path.startswith('/'):
                book_url = 'https://fanqienovel.com' + book_path
            else:
                book_url = str(book_path or '')
            intro_raw = _clean(_decode_fq(si.get('introduction') or si.get('intro') or '')) or None
            last_ch_raw = _clean(_decode_fq(si.get('lastChapterTitle') or si.get('last_chapter') or '')) or None
            items.append({
                'rankNo': len(items) + 1,
                'rankChange': 0,
                'bookTitle': title_raw,
                'bookId': bid,
                'bookUrl': book_url,
                'authorName': author_raw,
                'coverUrl': cover,
                'intro': intro_raw,
                'statusText': _clean(_decode_fq(si.get('statusText') or si.get('status') or '')) or None,
                'readingText': reading_text,
                'readingCount': _parse_cn(reading_text or ''),
                'metricName': '在读',
                'metricText': reading_text,
                'metricValue': _parse_cn(reading_text or ''),
                'lastChapterTitle': last_ch_raw,
                'lastChapterUrl': None,
                'lastUpdateTimeText': None,
                'categoryName': category_name or _clean(_decode_fq(si.get('categoryName') or si.get('category') or '')),
            })
            exist_ids.add(bid)

    # 乱码熔断：书名半数以上乱码则报错跳过（继承老服务端同款保护）
    if len(items) >= 3 and (sum(1 for it in items if _pua_count(it['bookTitle']) >= 2) / len(items) > 0.5):
        raise RuntimeError('番茄字体解码失败（疑似目标站更换字体），本次不返回脏数据')
    return {'pageTitle': page_title, 'cutoffText': cutoff, 'items': items}


def _map_fq_api_book(b: dict[str, Any], category_name: str | None, metric_name: str = '在读') -> dict[str, Any] | None:
    """番茄 /api/rank/category/list 返回的 book_list 对象 → 统一 books item。字体解码与 SSR 保持一致。"""
    bid = str(b.get('bookId') or b.get('book_id') or '').strip()
    title_raw = _clean(_decode_fq(b.get('bookName') or b.get('book_name') or b.get('title') or ''))
    if not bid or not title_raw:
        return None
    author_raw = _clean(_decode_fq(b.get('author') or '')) or None
    # category: 优先入参的分类名，其次接口字段
    cat = category_name or _clean(_decode_fq(b.get('categoryName') or b.get('category') or b.get('categoryV2') or '')) or None
    read_raw = b.get('readingCount') or b.get('reading_count') or b.get('read_count') or b.get('readCount') or b.get('reading')
    if read_raw is None or read_raw == '':
        reading_text = None
    else:
        reading_text = str(read_raw)
    thumb = str(b.get('thumbUri') or b.get('cover') or '').strip()
    cover = ''
    if thumb:
        if thumb.startswith('//'): cover = 'https:' + thumb
        elif thumb.startswith('http'): cover = thumb
        else: cover = 'https://' + thumb.lstrip('/')
    last_ch = _clean(_decode_fq(b.get('lastChapterTitle') or b.get('last_chapter') or '')) or None
    last_upd_ts = b.get('lastChapterUpdateTime') or b.get('last_update_time') or b.get('updateTime')
    last_upd_text = None
    if last_upd_ts:
        try:
            t = int(last_upd_ts)
            if t > 1_000_000_000:
                last_upd_text = time.strftime('%Y-%m-%d', time.localtime(t))
        except Exception:
            last_upd_text = str(last_upd_ts)[:20]
    intro = _clean(_decode_fq(b.get('abstract') or b.get('introduction') or b.get('intro') or '')) or None
    word_num = b.get('wordNumber') or b.get('word_count') or None
    word_text = None
    if word_num not in (None, ''):
        try:
            w = int(word_num)
            if w >= 10000:
                word_text = f'{w / 10000:.1f}万字'
            else:
                word_text = f'{w}字'
        except Exception:
            word_text = str(word_num)
    return {
        'rankNo': 0,
        'rankChange': 0,
        'bookTitle': title_raw,
        'bookId': bid,
        'bookUrl': f'https://fanqienovel.com/page/{bid}',
        'authorName': author_raw,
        'coverUrl': cover,
        'intro': intro,
        'statusText': _clean(_decode_fq(b.get('creationStatusText') or b.get('status') or '')) or None,
        'readingText': reading_text,
        'readingCount': _parse_cn(reading_text or ''),
        'metricName': metric_name,
        'metricText': reading_text,
        'metricValue': _parse_cn(reading_text or ''),
        'lastChapterTitle': last_ch,
        'lastChapterUrl': None,
        'lastUpdateTimeText': last_upd_text,
        'categoryName': cat,
        'wordText': word_text,
    }


def _parse_fq_rank_url(url: str) -> dict[str, Any] | None:
    """从番茄 rank URL 解析 (gender_int, rankMold_int, category_id|None).
    URL 形如：https://fanqienovel.com/rank/{a}_{b}[_{c}]
      - b==1 新书榜(new),  b==2 阅读榜(reading)  — 真实 SSR 与 RANK_SOURCES 约定一致
      - a==1 男频,  a==0 女频
      - c 为分类 ID（可选）
    返回 dict 或 None（无法解析）。
    """
    try:
        m = re.search(r'/rank/([0-9]+)_([0-9]+)(?:_([0-9]+))?/?(?:\?|#|$)', url or '')
        if not m:
            return None
        a, b, c = m.group(1), m.group(2), m.group(3)
        gender_int = 1 if a == '1' else 2  # 1=male, 2=female
        rank_mold_int = 2 if b == '2' else 1  # 1=new, 2=reading
        rank_list_type = 3  # 客户端 SSR 接口固定值（来自 Home.js / muye_a2c8e2a7.js）
        rank_type = 'reading' if b == '2' else 'new'
        metric_name = '在读'
        return {
            'gender': gender_int,
            'rankMold': rank_mold_int,
            'rank_list_type': rank_list_type,
            'category_id': int(c) if c else None,
            'rank_type': rank_type,
            'metricName': metric_name,
        }
    except Exception:
        return None


def _crawl_fq_api(url: str, category_name: str | None, limit: int = 20) -> list[dict[str, Any]]:
    """番茄 rank-category JSON API 最佳努力抓取（app_id=1967 服务端口径）。
    返回：最多 limit 本书，失败或空列表时返回 []。"""
    info = _parse_fq_rank_url(url)
    if not info:
        return []
    try:
        sess = _get_fq_session(referer=url)
        # 以当前 rank 详情页为 referer 再请求一次，提升命中真实 cookie 的概率
        try:
            _session_get(sess, url, headers={'Accept-Language': 'zh-CN,zh;q=0.9'}, timeout=20)
        except Exception:
            pass
        params: dict[str, Any] = {
            'app_id': 1967,
            'rank_list_type': info['rank_list_type'],
            'offset': 0,
            'limit': max(int(limit or 0) or 50, 50),
            'gender': info['gender'],
            'rankMold': info['rankMold'],
        }
        if info['category_id'] is not None:
            params['category_id'] = info['category_id']
        # app_id 2503 也试一次（客户端口径）。优先1967若返回空则回退。
        api_url = 'https://fanqienovel.com/api/rank/category/list'
        headers = {
            'Referer': url,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        results: list[dict[str, Any]] = []
        metric_name = info.get('metricName') or '在读'
        # category_id 缺省（“全部”综合榜）时传真实首个分类 ID 兜底 — 否则 API 返回 “分类类型错误”
        try_ids: list[int | None] = [params.get('category_id')]
        try_app_ids: list[int] = [1967, 2503]
        need_category_fallback = (info['category_id'] is None)
        data_bl: list[dict[str, Any]] | None = None
        for app_id in try_app_ids:
            params['app_id'] = app_id
            cid_list = list(try_ids)
            if need_category_fallback:
                # 全部榜：客户端代码默认取第一个分类 id 作为 actual 过滤 id 传给 API。
                # 这里先用 None（若返回 分类类型错误），再尝试 1014/246（男/女频 热类）
                cid_list = [None, 1014 if info['gender'] == 1 else 246]
            for cid in cid_list:
                if cid is None:
                    p = {k: v for k, v in params.items() if k != 'category_id'}
                else:
                    p = dict(params)
                    p['category_id'] = cid
                try:
                    r = _session_get(sess, api_url, params=p, headers=headers, timeout=18)
                    if r.status_code != 200:
                        continue
                    d = r.json() if (r.headers.get('Content-Type') or '').lower().startswith('application/json') or (r.text.lstrip()[:1] in '[{') else None
                    if not d:
                        continue
                    if int(d.get('code') or -1) != 0:
                        continue
                    data = d.get('data') or {}
                    bl = data.get('book_list') or []
                    if not bl:
                        continue
                    data_bl = list(bl)
                    break
                except Exception:
                    continue
            if data_bl:
                break
        if not data_bl:
            return []
        seen_ids: set[str] = set()
        for raw in data_bl:
            if not isinstance(raw, dict):
                continue
            it = _map_fq_api_book(raw, category_name=category_name, metric_name=metric_name)
            if not it:
                continue
            key = str(it.get('bookId') or '')
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            results.append(it)
            if len(results) >= limit:
                break
        # 乱码熔断（同 SSR）
        if results and (sum(1 for it in results if _pua_count(str(it.get('bookTitle') or '')) >= 2) / len(results)) > 0.5:
            return []
        return results
    except Exception:
        return []


def _build_fq_secondary_ssr_urls(url: str) -> list[str]:
    """根据主 URL 构造若干「同分类、不同榜单类型」的 SSR URL，用于在不依赖 msToken 的前提下凑齐 20 本。
    URL 格式 /rank/{a}_{b}[_{c}]：
      - a: 性别 (1=男, 0=女)
      - b: 榜单类型 (2=阅读榜, 1=新书榜) — 真实 SSR 对 b=1/2/3/4/5 多数有内容
      - c: 分类 id（可选）
    返回有序列表：优先同性别 跨榜单类型 → 跨 rank-mold 前缀数字，再跨性别（同分类）兜底。
    """
    info = _parse_fq_rank_url(url)
    if not info:
        return []
    a = '1' if info['gender'] == 1 else '0'
    # primary b from info.rank_type: 'reading' → '2', 'new' → '1'
    b_primary = '2' if info['rank_type'] == 'reading' else '1'
    c = str(info['category_id']) if info['category_id'] is not None else None
    result: list[str] = []
    base = 'https://fanqienovel.com/rank'
    suffix = f'_{c}' if c else ''
    # 1) 同性别 跨榜单类型 (阅读↔新书)
    other_b = '1' if b_primary == '2' else '2'
    result.append(f'{base}/{a}_{other_b}{suffix}')
    # 2) 同性别 其它榜单-mold 前缀 (2,3,4,5)  — SSR 实测 rank-book-item 有时有内容
    for prefix in ['2', '3', '4', '5']:
        if prefix != a:  # 避免重复 primary 自身 (a==1 时 prefix=1 跳过)
            for bt in [b_primary, other_b]:
                result.append(f'{base}/{prefix}_{bt}{suffix}')
    # 3) 跨性别 (同榜单类型+分类) — 仅分类榜有 c，全部榜通常男女分开无意义
    if c:
        other_a = '0' if a == '1' else '1'
        for bt in [b_primary, other_b]:
            result.append(f'{base}/{other_a}_{bt}{suffix}')
    # 去重保持顺序 + 排除自身
    seen: set[str] = set()
    seen.add(url.rstrip('/'))
    uniq: list[str] = []
    for u in result:
        uu = u.rstrip('/')
        if uu in seen:
            continue
        seen.add(uu)
        uniq.append(u)
    return uniq


def crawl_fanqie(url: str, category_name: str | None = None, max_pages: int = 1, limit: int = 20) -> dict[str, Any]:
    # 抓取策略（纯 HTTP，无 headless）：
    #   Step 1: SSR 主 URL → 稳定 10 条
    #   Step 2: 若不足 limit，用 _build_fq_secondary_ssr_urls 生成的同源异榜/异型 URL 继续 SSR 抓取，
    #           合并去重到 limit 条（弥补 msToken 限制导致 offset>10 的 JSON API 无法纯 requests 获取第 2 页的问题）。
    #   Step 3: 仍不足则再尝试 JSON API 最佳努力（通常由于缺少 msToken 返回空，仅作兜底）。
    # 番茄 SSR 不分页（?page=2 仍返回第 1 页 DOM），因此忽略 max_pages 循环；若 __INITIAL_STATE__.rank.book_list
    # 有超过 SSR DOM 条数的条目，parse_fanqie_html 会自动合并。
    limit_i = max(int(limit or 20), 1)
    parsed_page_title = ''
    parsed_cutoff = ''
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()

    def _merge(new_items: list[dict[str, Any]]) -> None:
        for it in new_items:
            bid = str(it.get('bookId') or '').strip()
            title = str(it.get('bookTitle') or '').strip()
            if (bid and bid in seen_ids) or (title and title in seen_titles):
                continue
            items.append(it)
            if bid: seen_ids.add(bid)
            if title: seen_titles.add(title)
            if len(items) >= limit_i:
                break

    ssr_urls = [url] + _build_fq_secondary_ssr_urls(url)
    last_err: Exception | None = None
    for idx, u in enumerate(ssr_urls):
        if len(items) >= limit_i:
            break
        try:
            html = _rank_fetch(u, referer='https://fanqienovel.com/')
            parsed = parse_fanqie_html(html, category_name=category_name)
            if idx == 0:
                parsed_page_title = parsed.get('pageTitle') or ''
                parsed_cutoff = parsed.get('cutoffText') or ''
            new_items = list(parsed.get('items') or [])
            # 过滤：非首个源有乱码/字体缺失时放弃
            if idx > 0 and new_items:
                bad = sum(1 for it in new_items if _pua_count(str(it.get('bookTitle') or '')) >= 2)
                if bad / len(new_items) > 0.5:
                    continue
            _merge(new_items)
        except Exception as err:
            last_err = err
            if idx == 0:
                continue  # 主 URL 失败不中断，用后续 URL 兜底

    # 仍不足 → 尝试 JSON API（msToken 兜底）
    if len(items) < limit_i:
        need = limit_i - len(items)
        api_items = _crawl_fq_api(url, category_name=category_name, limit=max(need * 2, 20))
        _merge(api_items)

    if not items:
        raise last_err or RuntimeError('番茄榜单未解析到条目（页面结构可能变更 / 网络异常）')

    # 最终截断到 limit 并重新续排 rankNo
    if limit_i > 0:
        items = items[:limit_i]
    for i, it in enumerate(items):
        it['rankNo'] = i + 1

    return {'pageTitle': parsed_page_title, 'cutoffText': parsed_cutoff, 'items': items}


def parse_qimao_json(payload: Any) -> list[dict[str, Any]]:
    root = payload if isinstance(payload, dict) else {}
    table = ((root.get('data') or {}).get('table_data')) or []
    items: list[dict[str, Any]] = []
    for i, row in enumerate(table):
        book_id = _clean(row.get('book_id'))
        title = _clean(row.get('title'))
        if not title or not book_id:
            continue
        num_s = _clean(row.get('number'))
        unit = _clean(row.get('unit'))
        metric_value = 0
        try:
            nv = float(num_s)
            metric_value = int(round(nv * (100000000 if unit == '亿' else 10000 if unit == '万' else 1)))
        except Exception:
            metric_value = 0
        items.append({
            'rankNo': i + 1,
            'rankChange': 0,
            'bookTitle': title,
            'bookId': book_id,
            'bookUrl': _clean(row.get('book_url')) or f'https://www.qimao.com/shuku/{book_id}/',
            'authorName': _clean(row.get('author')) or None,
            'coverUrl': _clean(row.get('image_link')) or None,
            'intro': _clean(row.get('intro')) or None,
            'statusText': '已完结' if str(row.get('is_over')) == '1' else '连载中',
            'readingText': None,
            'readingCount': 0,
            'metricName': '热度',
            'metricText': f"{num_s}{unit}".strip() or None,
            'metricValue': metric_value,
            'lastChapterTitle': _clean(row.get('latest_chapter_title')) or None,
            'lastChapterUrl': None,
            'lastUpdateTimeText': _clean(row.get('update_time')) or None,
            'categoryName': _clean(row.get('category1_name')) or None,
            'categorySubName': _clean(row.get('category2_name')) or None,
        })
    return items


def crawl_qimao(base_url: str, max_pages: int = 3) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, min(max_pages, 5) + 1):
        sep = '&' if '?' in base_url else '?'
        url = f'{base_url}{sep}page={page}'
        # 日榜时清 date 固定值（与 easy-writing：local-rank-crawler 同口径）
        if 'date_type=1' in url:
            url = re.sub(r'date=[^&]*', 'date=', url)
        try:
            text = _rank_fetch(url, referer='https://www.qimao.com/rank/')
            page_items = parse_qimao_json(json.loads(text))
        except Exception:
            continue
        if not page_items:
            break
        for it in page_items:
            key = it.get('bookId') or it.get('bookUrl')
            if key in seen:
                continue
            seen.add(key)
            it['rankNo'] = len(items) + 1
            items.append(it)
    if not items:
        raise RuntimeError('七猫榜单接口未返回数据')
    return items


# ---- 起点中文网 · 移动端 JSON 接口（真实榜单，支持分类/子类/翻页）----
# 起点桌面端有 probe.js 反爬（服务端直抓返回 202 探针页），但移动端 m.qidian.com
# 的 majax JSON 接口可以拿到真实榜单：先请求首页拿 _csrfToken + cookie，再按
# gender / catId(大类) / subCatIds(主题子类) / pageNum 翻页取数据，每页 20 条。
_QIDIAN_API = 'https://m.qidian.com'
_QIDIAN_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                     'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1')
_QIDIAN_RANK_API: dict[str, str] = {
    'hotsale': 'hotsalesList',
    'monthTicket': 'yuepiaolist',
    'collect': 'collectlist',
    'newauthor': 'newauthor',
}
_QIDIAN_RANK_LABEL: dict[str, str] = {
    'hotsale': '畅销榜',
    'monthTicket': '月票榜',
    'collect': '收藏榜',
    'newauthor': '新书榜',
}
_QIDIAN_METRIC_NAME: dict[str, str] = {
    'hotsale': '热度',
    'monthTicket': '月票',
    'collect': '收藏',
    'newauthor': '新书热度',
}


def _qidian_session() -> requests.Session:
    """初始化带 _csrfToken cookie 的移动端会话。

    注意：必须访问 /rank 页才能拿到 _csrfToken cookie；首页只下发 abPolicies，
    此时直接请求 majax/rank/xxx 会返回 {"code":1,"msg":"失败"}。
    """
    # 复用 TLS 适配 + Retry + verify 兜底会话（起点同番茄也可能在 Render 出口被自签 MITM 拦截）
    s = _build_tls_session()
    s.headers.update(REQ_HEADERS)
    s.headers['User-Agent'] = _QIDIAN_MOBILE_UA
    try:
        _session_get(s, 'https://m.qidian.com/rank', timeout=15)
    except Exception:
        pass
    # 兜底：如果 /rank 也没写入 cookie，再请求一次首页；部分网络环境首页优先设置基础域 cookie 后 /rank 的 set-cookie 才生效
    if not s.cookies.get('_csrfToken'):
        try:
            _session_get(s, 'https://m.qidian.com/', timeout=15)
            _session_get(s, 'https://m.qidian.com/rank', timeout=15)
        except Exception:
            pass
    return s


def _qidian_cat_ids(code: str | None) -> tuple[int, list[int]]:
    """把 RANK_CATEGORIES 的 code（如 chanId21 / chanId4-subCateId74）解析成 catId / subCatIds。"""
    code = code or ''
    cat_id = -1
    sub_ids: list[int] = []
    m = re.match(r'^chanId(\d+)', code)
    if m:
        cat_id = int(m.group(1))
    for sm in re.finditer(r'subCateId(\d+)', code):
        sub_ids.append(int(sm.group(1)))
    return cat_id, sub_ids


def _map_qidian_record(rec: dict[str, Any], rank_type: str) -> dict[str, Any]:
    metric_name = _QIDIAN_METRIC_NAME.get(rank_type, '热度')
    rankcnt = _clean(rec.get('rankCnt'))
    cnt = _clean(rec.get('cnt'))
    rank_num = rec.get('rankNum')
    bid = _clean(rec.get('bid'))
    # 封面：起点 CDN 书籍封面规则，若 404 前端会 fallback 到 📚
    # bookcover/000/aaa/bbb/ccc.jpg 模式，bid 前补 0 到 9 位，按 3/3/3 分层
    cover_url: str | None = None
    if bid and bid.isdigit():
        pad = bid.zfill(9)
        cover_url = f'https://bookcover.yuewen.com/qdbimg/349573/{bid}/150'
    # 指标：优先 rankCnt（榜单指标）；否则用 cnt（字数/热度值）；再不行用 rankNum 占位
    metric_text: str | None = rankcnt or (cnt if cnt else (f'#{rank_num}' if rank_num else None))
    metric_value: int = (
        _parse_cn(rankcnt)
        if rankcnt
        else (_parse_cn(cnt) if rank_type in ('collect',) and cnt else 0)
    )
    return {
        'rankNo': 0,  # 抓取合并后再统一续排
        'rankChange': 0,
        'bookTitle': _clean(rec.get('bName')),
        'bookId': bid,
        'bookUrl': f"https://www.qidian.com/book/{bid}/" if bid else '',
        'authorName': _clean(rec.get('bAuth')) or None,
        'coverUrl': cover_url,
        'intro': _clean(rec.get('desc')) or None,
        'statusText': None,
        'readingText': cnt or None,
        'readingCount': _parse_cn(cnt) if rank_type != 'collect' else 0,
        'metricName': metric_name,
        'metricText': metric_text,
        'metricValue': metric_value,
        'lastChapterTitle': None,
        'lastChapterUrl': None,
        'lastUpdateTimeText': None,
        'categoryName': _clean(rec.get('cat')) or None,
        'categorySubName': _clean(rec.get('subCat')) or None,
        'wordsText': cnt or None,
    }


def crawl_qidian_api(rank_type: str, gender: str = 'male', category_code: str | None = None,
                     max_pages: int = 1, force: bool = False) -> dict[str, Any]:
    """抓取起点真实榜单：按 榜单类型×男女频×大类×主题子类 的组合，每页20本。

    默认只取1页（与参考站「单榜20本」一致）；调用方可按需扩大 max_pages。
    """
    api_name = _QIDIAN_RANK_API.get(rank_type)
    if not api_name:
        raise ValueError(f'起点暂不支持该榜单类型：{rank_type}')
    gender = gender or 'male'
    cat_id, sub_ids = _qidian_cat_ids(category_code)
    sub_key = ','.join(str(x) for x in sub_ids)
    cache_key = f"qd-live:{rank_type}:{gender}:{cat_id}:{sub_key}"
    now = time.time()
    with _lock:
        cached = _cache.get(cache_key)
        if (not force) and cached and (now - cached[0]) < _CACHE_TTL:
            return dict(cached[1])
    session = _qidian_session()
    token = str(session.cookies.get('_csrfToken') or '')
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = max(1, min(max_pages, 5))
    for page in range(1, pages + 1):
        params: dict[str, Any] = {'_csrfToken': token, 'gender': gender,
                                  'pageNum': page, 'page': page, 'catId': cat_id}
        if sub_ids:
            params['subCatIds'] = ','.join(str(x) for x in sub_ids)
        try:
            r = _session_get(session, f'{_QIDIAN_API}/majax/rank/{api_name}', params=params,
                             headers={'Referer': 'https://m.qidian.com/rank/'}, timeout=20)
            payload = r.json()
        except Exception:
            if page == 1:
                raise
            break
        # 起点偶发 {"code":1,"msg":"失败"}：重建会话拿新 token 后重试一次当前页
        if payload.get('code') != 0 and page == 1 and not force:
            try:
                session2 = _qidian_session()
                token2 = str(session2.cookies.get('_csrfToken') or '')
                if token2 and token2 != token:
                    params2 = dict(params)
                    params2['_csrfToken'] = token2
                    r2 = _session_get(session2, f'{_QIDIAN_API}/majax/rank/{api_name}', params=params2,
                                      headers={'Referer': 'https://m.qidian.com/rank/'}, timeout=20)
                    payload = r2.json()
                    session = session2
                    token = token2
            except Exception:
                pass
        if payload.get('code') != 0:
            msg = payload.get('msg') or '起点接口返回失败'
            if page == 1:
                raise RuntimeError(msg)
            break
        records = (payload.get('data') or {}).get('records') or []
        if not records:
            break
        for rec in records:
            key = str(rec.get('bid')) or _clean(rec.get('bName'))
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(_map_qidian_record(rec, rank_type))
        if (payload.get('data') or {}).get('isLast') in (1, True):
            break
        if page < pages:
            time.sleep(0.6)
    if not items:
        raise RuntimeError('起点榜单接口未返回数据')
    for idx, it in enumerate(items):
        it['rankNo'] = idx + 1
    result: dict[str, Any] = {
        'sourceKind': 'live',
        'rankType': rank_type,
        'rankTitle': _QIDIAN_RANK_LABEL.get(rank_type, rank_type),
        'gender': gender,
        'categoryCode': category_code,
        'items': items,
    }
    with _lock:
        _cache[cache_key] = (time.time(), dict(result))
    return result


# ---------------------------------------------------------------------------
# 3. 内存缓存 + 单源并发合并抓取
# ---------------------------------------------------------------------------
_CACHE_TTL = 3600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _find_category(legacy_id: Any):
    legacy_id = int(legacy_id) if legacy_id else None
    for c in RANK_CATEGORIES:
        if c['legacyId'] == legacy_id:
            return c
    return None


def _find_source(legacy_id: Any):
    legacy_id = int(legacy_id) if legacy_id else None
    for s in RANK_SOURCES:
        if s['legacyId'] == legacy_id:
            return s
    return None


def crawl_rank_source(source_id: int, force: bool = False, limit: int = 20) -> dict[str, Any]:
    """按榜单源 id 抓取并返回统一结构。默认 limit=20，与参考站「单榜20本」一致。"""
    src = _find_source(source_id)
    if not src:
        raise ValueError('榜单源不存在')
    if src.get('enabled') != 1:
        raise ValueError('当前榜单源已禁用')
    cache_key = f"src:{source_id}"
    now = time.time()
    with _lock:
        cached = _cache.get(cache_key)
        if (not force) and cached and (now - cached[0]) < _CACHE_TTL:
            payload = dict(cached[1])
            payload['items'] = list(payload.get('items', []))[:limit]
            return payload
    site = src['siteCode']
    meta = src.get('meta') or {}
    max_pages = int(meta.get('maxPages') or 1)
    # 每个榜单默认最多抓取并缓存 20 条；若调用方 limit>20，则向上取整到 20 的页数
    fetch_limit = max(20, int(limit or 20))
    result: dict[str, Any] = {
        'sourceId': src['legacyId'],
        'siteCode': site,
        'rankType': src['rankType'],
        'rankTitle': src.get('title'),
        'pageTitle': '',
        'cutoffText': '',
        'fetchAt': int(now),
        'items': [],
    }
    try:
        if site == 'fanqie':
            cat = _find_category(src.get('categoryLegacyId'))
            parsed = crawl_fanqie(src['url'], category_name=cat['name'] if cat else None,
                                  max_pages=max_pages, limit=fetch_limit)
            result['pageTitle'] = parsed.get('pageTitle', '')
            result['cutoffText'] = parsed.get('cutoffText', '')
            result['items'] = parsed.get('items', [])
            result['sourceKind'] = 'live'
        elif site == 'qimao':
            items = crawl_qimao(src['url'], max_pages=max_pages)
            # 补 metricName（大热榜/新书榜=热度，收藏榜=无）
            metric_name = str(meta.get('metricName') or '热度')
            for it in items:
                if not it.get('metricName'):
                    it['metricName'] = metric_name
            result['items'] = items
            result['sourceKind'] = 'live'
        elif site == 'qidian':
            gender = str(meta.get('gender') or 'male')
            cat = _find_category(src.get('categoryLegacyId')) if src.get('categoryLegacyId') else None
            cat_code = cat['code'] if cat else None
            data = crawl_qidian_api(src['rankType'], gender=gender, category_code=cat_code, max_pages=max_pages)
            result['items'] = data['items']
            result['sourceKind'] = 'live'
            result['rankType'] = data['rankType']
            result['rankTitle'] = data['rankTitle']
        else:
            raise ValueError(f'不支持的站点：{site}')
    except Exception as exc:
        result['fetchError'] = str(exc)
        # 返回空列表 + 错误提示
    result['itemCount'] = len(result.get('items', []))
    with _lock:
        _cache[cache_key] = (time.time(), dict(result))
    # 返回切片
    result['items'] = list(result.get('items', []))[:limit]
    return result


# ---------------------------------------------------------------------------
# 4. 查询辅助函数：根据平台/榜单类型/男女频/分类代码解析榜单源
# ---------------------------------------------------------------------------
def resolve_sources(platform: str, rank_type: str | None, gender: str | None,
                    category_code: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in RANK_SOURCES:
        if s['siteCode'] != platform:
            continue
        if rank_type and s['rankType'] != rank_type:
            continue
        meta = s.get('meta') or {}
        src_gender = meta.get('gender')
        # 如果分类源本身没有 gender（番茄 classification sources 没有），就用分类表反查
        cat = _find_category(s.get('categoryLegacyId')) if s.get('categoryLegacyId') else None
        g = src_gender or (cat['gender'] if cat else None)
        if gender and g and g != gender:
            continue
        if category_code:
            if not cat or cat['code'] != category_code:
                continue
        out.append(s)
    return out


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


def _call_small_llm_json(messages: list[dict], max_tokens: int = 512) -> dict:
    """调用轻量 LLM 返回 JSON；失败返回空 dict。由 llm_gateway 拿默认配置。"""
    try:
        from llm_gateway import LLMGateway, get_llm_config  # 延迟导入，避免循环依赖
        base_url, api_key, model = get_llm_config()
        if not api_key:
            return {}
        gw = LLMGateway(base_url, api_key, model)
        out = gw.chat(messages, temperature=0.4, max_tokens=max_tokens)
        txt = (out.get('text') if isinstance(out, dict) else str(out)).strip()
        if not txt:
            return {}
        # 容错：可能包 ```json ... ```，剥离
        m = re.search(r'\{[\s\S]*\}', txt)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    except Exception:
        return {}
    return {}


def _aggregate_market_llm(concept: str, merged_items: list[dict], matched_labels: list[str]) -> dict:
    """基于 TopN 书元数据，让 LLM 抽四大市场情报列表；失败返回规则兜底。"""
    if not merged_items:
        return {
            'opening_patterns': [],
            'popular_elements': [],
            'landmine_elements': [],
            'title_formulas': [],
        }
    user_prompt = f"【用户构想】\n{concept[:800]}\n\n"
    user_prompt += "【匹配的新书榜 TOP 10】\n"
    for idx, it in enumerate(merged_items[:10], 1):
        title = _clean(it.get('bookTitle') or it.get('title'))
        intro = _clean(it.get('intro') or it.get('bookIntro') or '')[:160]
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
        "\n【任务】从上方新书榜 TOP 10（已命中分类：" + '、'.join(matched_labels) + "）"
        " 总结出面向网文作者的市场情报，仅输出一个 JSON 对象，不要任何解释文字，不要 ```json 包裹。"
        " JSON 结构（固定 4 个数组，每项是一句中文，每条 20~60 字，数组每项 5~8 条）：\n"
        "{\n"
        "  \"opening_patterns\": [\"开篇钩子套路1\", \"钩子2\"...],\n"
        "  \"popular_elements\": [\"读者买单要素1\", \"要素2\"...],\n"
        "  \"landmine_elements\": [\"读者弃文毒点1\", \"毒点2\"...],\n"
        "  \"title_formulas\": [\"书名公式范例1（用占位符）\", \"公式2\"]\n"
        "}\n"
    )
    messages = [
        {'role': 'system', 'content': '你是网文爆款数据分析助手，说话精炼、全用中文、不输出废话、只给结论。所有数组项必须是中文短句，控制长度。'},
        {'role': 'user', 'content': user_prompt},
    ]
    resp = _call_small_llm_json(messages, max_tokens=900)
    # 规则级兜底 + 长度裁剪
    def _arr(key: str, fallback: list[str]) -> list[str]:
        v = resp.get(key) or fallback
        if not isinstance(v, list):
            v = fallback
        cleaned = []
        for x in v:
            s = _clean(str(x))
            if 4 <= len(s) <= 120:
                cleaned.append(s)
            if len(cleaned) >= 8:
                break
        return cleaned or fallback
    return {
        'opening_patterns': _arr('opening_patterns', ['开篇用旁白抛出世界观规则，随即切主角生死危机场面']),
        'popular_elements': _arr('popular_elements', ['能力分阶解锁+可视化进度']),
        'landmine_elements': _arr('landmine_elements', ['开篇堆砌设定>2段，无冲突']),
        'title_formulas': _arr('title_formulas', ['《前缀：核心卖点》']),
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
        agg = {'opening_patterns': [], 'popular_elements': [], 'landmine_elements': [], 'title_formulas': []}

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
