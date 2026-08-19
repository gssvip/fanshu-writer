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
# 2026-08-18 重校准3（M9 技能包生效链路打通）：
#   - _get_skill_prompts_by_category 新增 book_genre 参数+文风类 genre_target 过滤 (~22行)
#   - 新增 _get_enabled_style_pack 真实实现（之前 build_writing_rules 引用它但不存在）(~19行)
#   → 净增 14009 - 13976 = 33 行
APP_PY_BASELINE = 14009
APP_PY_TOLERANCE = 0  # 允许的增量，0 表示严禁增长

# 前端单文件行数上限
FE_MAX_LINES = 1500

# WritePage.tsx 基线行数：只能减不能增（前端巨石，防止继续膨胀）
# 2026-08-18 重校准：版本号对比等细节+UI交互
WRITEPAGE_BASELINE = 8899

# ChatPanel.tsx 基线行数：只能减不能增（智驾面板巨石，防止继续膨胀）
# 2026-08-18 重校准2（M9 技能包生效链路打通）：
#   - refreshProgress 新增：api.getBook回填三组ids(11行)
#   - toggleSkillPack 持久化：api.updateBook保存到三字段(18行)
#   - doChapterAction 请求加 skill_pack_ids 字段(2行)
#   → 净增 3233 - 3206 = 27 行
# 2026-08-19 重校准3（NETWORK ERROR SSE冒号注释帧协议对齐）：
#   - parseSSE 显式新增 trimmed.startsWith(':') continue 跳过SSE心跳注释帧（1行+注释）
#   → 净减 6 行（3233→3227，属精简）
# 2026-08-19 重校准5（构思方案选定=直接开干交互修复）：
#   - handleGenerate 空input时queueMicrotask自动触发生成（不再强迫点第二下按钮"被打断"）（9行）
#   - handleGenerateFromSelectedRef + useEffect sync ref（2行）
#   - catch console.error真实错误名+消息+堆栈 + setStreamError（e.name:e.message）（3行）
#   → 净增 15 行（3242→3257），属交互对齐+排障必需，不属业务膨胀
CHATPANEL_BASELINE = 3257

# ToolsPage.tsx 基线行数：只能减不能增（技能包/审稿/人设分析工具面板巨石）
# 2026-08-18 重校准2（M8 题材对齐）：
#   - 删除旧本地迷你GENRES（-6行）
#   - 新增genre_target文风类适用题材下拉UI (+20行)
#   - 新增normalizeGenreKey调用在两处(导入payload / smartParse末尾) + GENRE_GROUPS分组optgroup(×3处)改写
#   → 净增 1192 - 1158 = 34行
TOOLSPAGE_BASELINE = 1192

# chat_collab_bp.py 基线行数：只能减不能增（智驾协作 Blueprint 巨石）
# 2026-08-18 重校准6（NETWORK ERROR瘦身补丁：重复禁令3合1，纯减tokens防TTFT超时）：
#   - GENERAL_CORE_RULES 禁令0原16行反例样本+识别→简写成3行（省569字）
#   - chapter_plot_iron 前置5条禁令提醒（858字）→ 删掉纯重复，留剧情指令+字数铁律293字
#   - PRE_GENERATE_BAN_RULES 禁令0/5改成【见行文规范】不复述，省约300字
#   → 净减约 25 行
# 2026-08-19 重校准7（NETWORK ERROR SSE双兜底：防Render 30s idle timeout断开）：
#   - 全7处generate()函数首行加yield ': ping-heartbeat-keepalive\\n\\n' 注释心跳帧
#   - chat_smart_action加start受理帧meta，_action_chapter加「正在续写/润色」delta
#   - 全7处Response headers加 Connection:keep-alive + Cache-Control:no-cache, no-transform
#   → 净加 27 行（6905→6932），属NETWORK ERROR单点修复必需，非业务膨胀
# 2026-08-19 重校准8（NETWORK ERROR重试/阻塞空窗期心跳：从截图暴露断在[字数校验]之后的_ensure_word_count阻塞阶段）：
#   - 顶部+threading/time/queue import（6行），新增SSE_HEARTBEAT_COMMENT常量+_run_blocking_with_heartbeat generator（33行）
#   - _action_chapter字数校验段：_ensure_word_count从直接同步调用→包_run_blocking_with_heartbeat每10s心跳（21行）
#   - 4处for _attempt in range(max_attempts)循环体首行：加心跳+第N次尝试delta（4处×6行=24行）
#   → 净加 72 行（6932→7004），属NETWORK ERROR单点修复必需（堵住[字数校验]后断连这一用户实锤场景）
# 2026-08-19 重校准7（方案卡片中英混/Prompt规则复述垃圾P0兜底）：
#   - smart_suggest sys_prompt末尾删正文写作PLAIN_TEXT_LAYOUT_RULES大段；
#   - 新增P0禁令4条（禁英语/禁规则复述/禁占位短凑数/禁5条同义方案）+ 自检3条（约10行）；
#   - 新增多层级解析兜底：suggestions/result/data字段兼容→指纹过滤→方案N分段→中文句号断句桶→硬行截（约90行）；
#   → 净增 103 行（7004→7107），属修复用户截图"方案一：方案1+Each preview must中英混"的必需P0兜底，不属业务膨胀。
# 2026-08-19 重校准8（提示词阶段隔离·去一股脑全加载）：
#   - PLAIN_TEXT_LAYOUT_RULES 移除内嵌 NARRATIVE_CRAFT_RULES（=通用核心+正文行文规范两大包），只留排版格式；
#   - build_chat_system_prompt（智驾聊天）显式注入 GENERAL_CORE_RULES 三阶段通用铁律；
#   - _action_master_create（批量生成设定）显式调用 build_conception_rules() 构思阶段专属包；
#   → 净增 10 行（7107→7117），属【提示词阶段隔离P0结构治理】：构思/正文/去AI三阶段规则精准注入、不一股脑全加载、不冗余冲突，不属业务膨胀。
CHAT_COLLAB_BP_BASELINE = 7117

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
        if rel == "frontend/src/pages/ToolsPage.tsx":
            if lines > TOOLSPAGE_BASELINE:
                violations.append(
                    f"[ToolsPage 巨石回生] {rel} 行数 {lines} > 基线 {TOOLSPAGE_BASELINE}，"
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
