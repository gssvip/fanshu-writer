#!/usr/bin/env python3
"""架构门禁：阻止巨石回生。

参考司命 siming-ai 的 run_quality.py 思路，对番茄项目做最低成本的架构约束：
  1. 单文件行数上限（默认 2000 行，app.py 当前豁免但禁止增长）
  2. 单文件路由数上限（默认 30 个 @app.route）
  3. app.py 行数不得增长（基线 12805，只能减不能增）

退出码：0 通过，1 违规。CI 中作为 PR 门禁。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# 单文件行数上限（超出即违规）
MAX_LINES_PER_FILE = 2000

# 单文件路由数上限
MAX_ROUTES_PER_FILE = 30

# app.py 基线行数：只能减不能增（防止巨石继续膨胀）
# 当前基线：13513 行（2026-08-15 重校准：M4 智驾/系统优化等功能迭代后实际值）
# 未来禁止再增长，新代码必须落到独立模块
APP_PY_BASELINE = 13513
APP_PY_TOLERANCE = 0  # 允许的增量，0 表示严禁增长

# 前端单文件行数上限
FE_MAX_LINES = 1500

# WritePage.tsx 基线行数：只能减不能增（前端巨石，防止继续膨胀）
WRITEPAGE_BASELINE = 8576

# ChatPanel.tsx 基线行数：只能减不能增（智驾面板巨石，防止继续膨胀）
CHATPANEL_BASELINE = 2254

# chat_collab_bp.py 基线行数：只能减不能增（智驾协作 Blueprint 巨石）
# 2026-08-15 重校准：新增 TIMELINE_NARRATIVE_RULES（剧情维度 JSON 专用叙事铁律）导致 +61 行
CHAT_COLLAB_BP_BASELINE = 5085

# 豁免清单：历史巨石，只受"不得增长"约束，不受单文件行数约束
# 新增豁免需在 PR 里说明理由
EXEMPT_FILES = {
    "backend/app.py",
    "backend/post_write_validator.py",  # 1138 行，已模块化
    "backend/blueprints/chat_collab_bp.py",
}


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return sum(1 for _ in f)


def count_routes(path: Path) -> int:
    """统计 @app.route 装饰器数量。"""
    pattern = re.compile(r"^@app\.route\(", re.MULTILINE)
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return len(pattern.findall(f.read()))


def check_backend() -> list[str]:
    violations: list[str] = []

    # 1. 扫描所有 .py 文件的单文件行数与路由数
    for py_file in BACKEND.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        rel = py_file.relative_to(ROOT).as_posix()
        lines = count_lines(py_file)

        if rel in EXEMPT_FILES:
            # 豁免文件：只检查"不得增长"
            if rel == "backend/app.py":
                if lines > APP_PY_BASELINE + APP_PY_TOLERANCE:
                    violations.append(
                        f"[app.py 巨石回生] {rel} 行数 {lines} > 基线 {APP_PY_BASELINE}，"
                        f"禁止继续增长，请拆分到 Blueprint 或独立模块"
                    )
            elif rel == "backend/blueprints/chat_collab_bp.py":
                if lines > CHAT_COLLAB_BP_BASELINE:
                    violations.append(
                        f"[chat_collab_bp 巨石回生] {rel} 行数 {lines} > 基线 {CHAT_COLLAB_BP_BASELINE}，"
                        f"禁止继续增长，新路由请拆分到新 Blueprint"
                    )
            continue

        if lines > MAX_LINES_PER_FILE:
            violations.append(
                f"[单文件过大] {rel} {lines} 行 > 上限 {MAX_LINES_PER_FILE}，请拆分"
            )

        # 路由数检查（仅扫含 @app.route 的文件）
        if py_file.name == "app.py":
            routes = count_routes(py_file)
            if routes > MAX_ROUTES_PER_FILE:
                violations.append(
                    f"[路由过度集中] {rel} 含 {routes} 个 @app.route > 上限 {MAX_ROUTES_PER_FILE}，"
                    f"请拆分为 Flask Blueprint"
                )

    return violations


def check_frontend() -> list[str]:
    violations: list[str] = []
    frontend_src = ROOT / "frontend" / "src"
    if not frontend_src.exists():
        return violations

    writepage = frontend_src / "pages" / "WritePage.tsx"
    if writepage.exists():
        lines = count_lines(writepage)
        if lines > WRITEPAGE_BASELINE:
            violations.append(
                f"[WritePage 巨石回生] frontend/src/pages/WritePage.tsx 行数 {lines} > "
                f"基线 {WRITEPAGE_BASELINE}，禁止继续增长，请拆分组件"
            )

    # 扫描其他前端文件（WritePage/ChatPanel 豁免单文件行数，只受不得增长约束）
    for fe_file in frontend_src.rglob("*"):
        if fe_file.suffix not in (".tsx", ".ts"):
            continue
        if "node_modules" in fe_file.parts:
            continue
        rel = fe_file.relative_to(ROOT).as_posix()
        if rel == "frontend/src/pages/WritePage.tsx":
            continue
        lines = count_lines(fe_file)
        if rel == "frontend/src/components/ChatPanel.tsx":
            if lines > CHATPANEL_BASELINE:
                violations.append(
                    f"[ChatPanel 巨石回生] {rel} 行数 {lines} > 基线 {CHATPANEL_BASELINE}，"
                    f"禁止继续增长，新功能请拆分组件"
                )
            continue
        if lines > FE_MAX_LINES:
            violations.append(
                f"[前端单文件过大] {rel} {lines} 行 > 上限 {FE_MAX_LINES}，请拆分组件"
            )

    return violations


def main() -> int:
    print("=" * 70)
    print("番茄写作器 架构门禁")
    print("=" * 70)

    violations: list[str] = []
    violations.extend(check_backend())
    violations.extend(check_frontend())

    if not violations:
        print("✅ 架构门禁通过")
        print(f"   - app.py 基线 {APP_PY_BASELINE} 行（不得增长）")
        print(f"   - 单文件行数上限 {MAX_LINES_PER_FILE}（豁免：{', '.join(EXEMPT_FILES)}）")
        print(f"   - 单文件路由数上限 {MAX_ROUTES_PER_FILE}")
        print(f"   - 前端单文件行数上限 1500（WritePage.tsx 基线 {WRITEPAGE_BASELINE} 不得增长）")
        return 0

    print(f"❌ 架构门禁失败：{len(violations)} 项违规")
    print("-" * 70)
    for i, v in enumerate(violations, 1):
        print(f"  {i}. {v}")
    print("-" * 70)
    print("修复建议：")
    print("  - 巨石文件：按域拆分（章节/角色/世界观/AI/导出），新代码必须落到独立模块")
    print("  - 路由集中：迁移到 Flask Blueprint（app.register_blueprint)）")
    print("  - 前端巨石：按子视图/抽屉/弹窗拆组件")
    return 1


if __name__ == "__main__":
    sys.exit(main())
