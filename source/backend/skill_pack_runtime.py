# -*- coding: utf-8 -*-
"""
技能包运行期注入辅助（文风包生效链路补丁的核心承载模块）。

从 app.py 里拆出「文风激活 ID 解析」「激活文风包清单构造」两项，
避免 app.py 巨石增长（基线 14009）。被以下场景调用：
- 章节续写 (ai_continue) 时拼接 system_prompt
- 未来批量生成 / 审校等正文生成入口

Author: fanshu-writer
Date:   2026-08-21
"""

from __future__ import annotations

from typing import List

# ---------------- 激活 ID 解析（fix1） ----------------

def resolve_active_style_ids(requested_ids, book=None):
    """前端请求没传 skill_pack_ids 时，回退读 DB 里 book 级三列 *_skill_ids。

    这样即使用户只在"作品设置"里勾了文风而前端 POST 体忘记带数组，
    正文生成阶段也不会静默生成无文风内容（80% 的"勾了文风没生效"根因）。

    参数：
      requested_ids:  前端请求体传来的 skill_pack_ids（列表/可迭代/None/空字符串）
      book:           Book ORM 对象。调用方保证线程内 DB session 可用。

    返回：
      list 风格 skill_pack_id（含 master/style/review 三类，后续 category 会再过滤）
    """
    from app import _resolve_skill_ids_by_category  # 延迟导入避免循环

    active = []
    if requested_ids:
        try:
            active = list(requested_ids)
        except Exception:
            active = []
    # fix1: 空 → 回退读 book 持久化列
    if (not active) and book is not None:
        try:
            ids_m = _resolve_skill_ids_by_category(book, 'master') or []
            ids_s = _resolve_skill_ids_by_category(book, 'style') or []
            ids_r = _resolve_skill_ids_by_category(book, 'review') or []
            active = list({*ids_m, *ids_s, *ids_r})
        except Exception:
            # book/列缺失时静默回退为请求原始值，保证调用方一定拿得到 list
            active = []
    return active


# ---------------- 激活文风包自证清单（fix4） ----------------

def build_activated_skill_pack_manifest(active_style_ids) -> List[str]:
    """返回本次正文生成实际命中的文风包清单，供返回值字段 activated_skill_packs 使用。

    前端/grep 或 DB audit 时直接能看到：这次真加载了哪些文风、ID 是多少、
    要求的 genre_target 是什么、priority 排第几——
    避免"我选了历史脑洞但结果是玄幻写法"时无从验证。
    """
    manifest: List[str] = []
    if not active_style_ids:
        return manifest
    try:
        from app import SkillPack, app as _app  # 延迟导入
        for p in SkillPack.query.filter(SkillPack.id.in_(list(active_style_ids))).all():
            if (p.category or 'master') == 'style':
                manifest.append(
                    f"{p.name}(id={p.id},genre_target={p.genre_target or ''},priority={p.priority})"
                )
    except Exception as e:
        # 自证字段失败时不能影响正文生成，写入日志即可
        try:
            from app import app as _app
            _app.logger.warning(f'[skill_pack_runtime] build_manifest 失败: {e}')
        except Exception:
            pass
    return manifest
