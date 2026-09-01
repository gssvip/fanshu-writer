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
    # 起点（5）+七猫（3）大盘榜 + 起点新人新书榜（1）
    {"legacyId": 23, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "hotsale", "title": "畅销榜", "url": "https://www.qidian.com/rank/hotsales/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "scope": "all"}},
    {"legacyId": 24, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "monthTicket", "title": "月票榜", "url": "https://www.qidian.com/rank/yuepiao/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "metricName": "月票", "scope": "all"}},
    {"legacyId": 47, "siteCode": "qidian", "categoryLegacyId": None, "rankType": "newauthor", "title": "新人作者新书榜", "url": "https://www.qidian.com/rank/newauthor/", "enabled": 1, "meta": {"gender": "male", "maxPages": 5, "scope": "all"}},
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
    'newauthor': '新人新书榜',
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


def _rank_fetch(url: str, referer: str | None = None, timeout: int = 20) -> str:
    h = dict(REQ_HEADERS)
    if referer:
        h['Referer'] = referer
    r = requests.get(url, headers=h, timeout=timeout)
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

    # __INITIAL_STATE__ 里拿封面映射
    cover_map: dict[str, str] = {}
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>', html, re.S)
    if m:
        try:
            state = json.loads(m.group(1))
            for item in state.get('rank', {}).get('book_list', []) or []:
                bid = str(item.get('bookId') or '').strip()
                thumb = str(item.get('thumbUri') or '').strip()
                if bid and thumb:
                    if thumb.startswith('//'):
                        cover_map[bid] = 'https:' + thumb
                    elif thumb.startswith('http'):
                        cover_map[bid] = thumb
                    else:
                        cover_map[bid] = 'https://' + thumb.lstrip('/')
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

    # 乱码熔断：书名半数以上乱码则报错跳过（继承老服务端同款保护）
    if len(items) >= 3 and (sum(1 for it in items if _pua_count(it['bookTitle']) >= 2) / len(items) > 0.5):
        raise RuntimeError('番茄字体解码失败（疑似目标站更换字体），本次不返回脏数据')
    return {'pageTitle': page_title, 'cutoffText': cutoff, 'items': items}


def crawl_fanqie(url: str, category_name: str | None = None, max_pages: int = 1) -> dict[str, Any]:
    # SSR 只给首屏约 20 条，最多请求首屏（与当前 app.py 旧实现口径一致；多页需浏览器懒加载，留给后续窗口版）
    html = _rank_fetch(url, referer='https://fanqienovel.com/')
    parsed = parse_fanqie_html(html, category_name=category_name)
    if not parsed['items']:
        raise RuntimeError('番茄榜单未解析到条目（页面结构可能变更）')
    return parsed


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
    'newauthor': '新人作者新书榜',
}
_QIDIAN_METRIC_NAME: dict[str, str] = {
    'hotsale': '热度',
    'monthTicket': '月票',
    'collect': '收藏',
    'newauthor': '新书热度',
}


def _qidian_session() -> requests.Session:
    """初始化带 _csrfToken cookie 的移动端会话（先访问首页取 token）。"""
    s = requests.Session()
    s.headers.update(REQ_HEADERS)
    s.headers['User-Agent'] = _QIDIAN_MOBILE_UA
    try:
        s.get('https://m.qidian.com/', timeout=15)
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
    return {
        'rankNo': 0,  # 抓取合并后再统一续排
        'rankChange': 0,
        'bookTitle': _clean(rec.get('bName')),
        'bookId': _clean(rec.get('bid')),
        'bookUrl': f"https://www.qidian.com/book/{_clean(rec.get('bid'))}/",
        'authorName': _clean(rec.get('bAuth')) or None,
        'coverUrl': None,
        'intro': _clean(rec.get('desc')) or None,
        'statusText': None,
        'readingText': _clean(rec.get('cnt')) or None,
        'readingCount': 0,
        'metricName': metric_name,
        'metricText': rankcnt or None,
        'metricValue': _parse_cn(rankcnt or ''),
        'lastChapterTitle': None,
        'lastChapterUrl': None,
        'lastUpdateTimeText': None,
        'categoryName': _clean(rec.get('cat')) or None,
        'categorySubName': _clean(rec.get('subCat')) or None,
        'wordsText': _clean(rec.get('cnt')) or None,
    }


def crawl_qidian_api(rank_type: str, gender: str = 'male', category_code: str | None = None,
                     max_pages: int = 2, force: bool = False) -> dict[str, Any]:
    """抓取起点真实榜单：按 榜单类型×男女频×大类×主题子类 的组合，逐页合并去重。"""
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
            r = session.get(f'{_QIDIAN_API}/majax/rank/{api_name}', params=params,
                            headers={'Referer': 'https://m.qidian.com/rank/'}, timeout=20)
            payload = r.json()
        except Exception:
            if page == 1:
                raise
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


def crawl_rank_source(source_id: int, force: bool = False, limit: int = 100) -> dict[str, Any]:
    """按榜单源 id 抓取并返回统一结构。"""
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
            parsed = crawl_fanqie(src['url'], category_name=cat['name'] if cat else None, max_pages=max_pages)
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
    if platform == 'qidian':
        rank_types: list[dict[str, str]] = [
            {'value': 'hotsale', 'label': '畅销榜'},
            {'value': 'monthTicket', 'label': '月票榜'},
            {'value': 'collect', 'label': '收藏榜'},
            {'value': 'newauthor', 'label': '新人作者新书榜'},
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
    try:
        page_size = min(200, max(10, int(request.args.get('pageSize', 50))))
    except Exception:
        page_size = 50

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
    data = crawl_rank_source(source_id, force=force, limit=200)
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
