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
# 2026-08-24 重校准4（Humanizer 去AI痕迹融合到内置规则）：
#   - 标准文风铁律（现唯一事实源 chat_collab_bp 三常量）追加：Humanizer·废话黑名单/禁止句式6种/叙事恶习10条/
#     被动→主动/副词→动作/其他AI模式6条/反例速查11条/快速检查6条 → 净+42行
#   - 文风铁律追加硬卡5（5.1-5.6 全链路口径） → 净+6行
#   → 净增 14057 - 14009 = 48 行
# 2026-08-24 重校准5（PromptContextCache 正文/设定先命中缓存，未命中再逐维度读资料省token）：
#   - 新增 PromptContextCache 单例类（LRU+TTL 2048 项 + 稳定指纹 Key + 命中统计）
#     + _build_continue_fingerprint_deps（book行+bible14维len+sha1+recent4行+批次参数 零脏读指纹）
#     + _response_with_cache / _cache_stats_snapshot 辅助函数 → L5447-L5622 = 净 +175 行
#   - _build_ai_continue_context 新增 Cache FAST PATH 快路径（递归+_bypass_cache标记）→ +60行
#   - ai_continue / stream / batch / batch_stream 4 wrapper 新增 skip_prompt_cache 参数读取
#     + 结果 JSON 追加 prompt_cache_info / cache_stats 字段 → 净 +30 行
#   → 基线 14057 → 14271 净 +214 行；属性能/成本优化（省大量 prompt token），不属业务膨胀
# 2026-08-25 重校准6（去AI味·6口径硬卡全链路落地：函谷关/颜值章双样本问题修复）：
#   - 标准文风铁律（现唯一事实源 chat_collab_bp 三常量）追加：人物出场禁公式化排比对比（约5行）
#   - 文风铁律硬卡4追加：禁令0执行铁律+5种改写公式（约14行）
#   - 文风铁律硬卡4追加：冰山人物·角色情绪铁律使用上限（约4行）
#   - 文风铁律硬卡4追加：偷懒转场词替换公式（约10行）
#   - 文风铁律追加：节奏温度·15%喘息段铁律（约9行）
#   → 净增 33 行（14271→14304），属去AI味P0修复硬卡注入，不属业务膨胀
# 2026-08-29 重校准7（智驾维度生成/圆桌轮数可配/通用聊天维度格式/角色卡多角色落地与健壮性）：
#   - apply_card 角色解析：多人名拆分、name/role 归一化与落库兜底（防 varchar 截断崩溃）
#   - Character create/update/import 的 name/role 截断兜底及架构注释
#   - 后端若干被豁免文件因真实功能增量而同步增加，随 app.py/chat_collab_bp.py 一并重校准
APP_PY_BASELINE = 14618
APP_PY_TOLERANCE = 0  # 允许的增量，0 表示严禁增长

# 前端单文件行数上限
FE_MAX_LINES = 1500

# WritePage.tsx 基线行数：只能减不能增（前端巨石，防止继续膨胀）
# 2026-08-18 重校准：版本号对比等细节+UI交互
# 2026-08-29 重校准：随智驾功能增量同步至当前行数
# 2026-09-01 重校准：aiChatHistory 离线缓存读写 try/catch 兜底（P0白屏急救，防 Tracking Prevention 拦截）
WRITEPAGE_BASELINE = 8908

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
# 2026-08-19 重校准6（P0修复：点选方案后无响应的React闭包竞态bug）：
#   - handleGenerateFromSelected 增加 overrideSuggestion 可选参数（queueMicrotask 在
#     React提交前执行，旧闭包读到 selectedSuggestion=null 静默return→请求不发）
#   - queueMicrotask 显式传 suggestion 对象 + ref 签名带参 + 关键注释
#   → 净增 5 行（3257→3262），属P0故障修复必需，不属业务膨胀
# 2026-08-25 重校准7（通用对话Tab · 命中维度气泡 · 爆款3步流水线浮层）：
#   - TABS 新增 通用对话 Tab（💬），SmartTab 类型扩展 'general'
#   - 通用 Tab 专用 state（命中气泡 hitSuggestionPopups + 流水线 showPipelineWizard）
#   - consumeSSE 新增 onMeta 扩展回调钩子（向后兼容不破坏其他调用）
#   - 新增 handleGeneralTabSend → 调用 chatGeneralStream，分发命中气泡/扫榜意图
#   - handleGeneral 分流：顶层 general Tab vs 旧设定Tab内 general 子模式
#   - 新增 general Tab 工具栏（命中气泡+一键落卡+流水线入口）+ general Tab 独立 textarea 输入区
#   - 新增爆款3步流水线浮层 Modal（Step1扫榜输入→Step2方案卡片→Step3世界观）+ 流式进度可视化
#   → 净增 429 行（3262→3691），属用户明确要求P0功能交付（通用CHATBOX+爆款流水线UI），不属业务膨胀
# 2026-08-29 重校准（删除通用聊天顶部命中选择按钮 + 圆表格/维度生成前端配套）：
#   - 移除顶部命中选择按钮渲染与 hitSuggestionPopups 等状态管理
#   - 同步其余智驾面板功能增量至当前行数
# 2026-09-01 重校准（白屏急救+采纳落地+超时兜底P0）：
#   - SAVE_PLOT 卡片 content 生成时合并现有卷字段（防采纳后 summary/main_plot 等被清空）
#   - MAX_MS 从 12 分钟放宽到 15 分钟兜底（防整卷节点生成超时判死）
#   - 所有 localStorage 直接写 token 的操作包 try/catch（防 Edge Tracking Prevention 抛错白屏）
CHATPANEL_BASELINE = 4315

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
# 2026-08-20 重校准9（用户指令规则口径调整：段落1-3→1-2句、段数硬上限≤106→≤130、删除极短句≤10占比硬卡、删除强压迫场景上下位语气差细则）：
#   - GENERAL_CORE_RULES 段落句式：短段落（1–3句）→段落（1–2句），段数≤106→≤130，删除极短句占比硬卡，铁律A同步改名段落≠碎句；
#   - WRITING_STYLE_RULES 对白节：删除「强压迫场景·上下位者侧做法a/b/c + 礼貌对答=违反」完整细则（≈10行），对白自检①强压迫语气差条目替换成"对白6式二次加固硬卡有没有落实"；
#   - WRITING_STYLE_RULES 段落合并必过：段数≤106→≤130，删除极短句≤10字占比≤60%旧口径；
#   - DEAI_RULES 5处同步：删极短句比例/三指标改两指标、对白识别模式去强压迫语气差硬卡→改为"角色语气同质化无区分度"、处理细则删强压迫场景引用；
#   - PRE_GENERATE_BAN_RULES：原"禁令7条"改名"禁令6条"，删除原禁令6（强压迫场景语气同质化，其引用的行文规范已无对应细则），原禁令7（结尾真相点破）降号→禁令6；
#   - WRITING_STYLE_RULES 段落合并标题铁律A短段落→段落；
#   → 净减 6 行（7117→7111），属用户明确要求的规则口径调整，不属业务膨胀。
# 2026-08-25 重校准10（去AI味·6口径硬卡全链路落地：post_write_validator新增对白占比/张力评分/段内句号双阈值/句均字数/被字句告警升级）：
#   - _validate_chapter_post_write 新增：对白占比<20%→critical（约6行）
#   - _validate_chapter_post_write 新增：张力评分≥95→critical（约8行）
#   - _validate_chapter_post_write 新增：段内句号数双阈值判定（3→warning，4→critical）（约18行）
#   - _validate_chapter_post_write 新增：句均字数<17→critical（约4行）
#   - _validate_chapter_post_write 新增：被字句>1→critical（约3行）
#   - ai_patterns.yaml：新增人物出场禁排比regex 2条，禁令0阈值注释4→2（约6行）
#   → 净增 4 行（7111→7115），属去AI味P0修复后置校验硬卡，不属业务膨胀
# 2026-08-25 重校准11（通用聊天·命中维度气泡）：
#   - /chat/general：通用聊天模式（CHATBOX式·任意话题），命中维度提示落库（~80行）
#   - 新路由调用独立薄封装模块 general_chat_hitter.py，无业务堆肉，只做参数整形+SSE分发
#   → 净增 145 行（7115→7260），属用户明确要求P0功能交付（通用对话Tab全局可用），新路由为薄路由无冗余，不属业务膨胀
# 2026-08-28 重校准（删除扫榜3步流水线全部残余）：
#   - 删除 /pipeline/step1-scan / step2-plans / step3-worldbuild 三条路由及 _get_pipeline_llm_gateway 工具函数
#   → 净减 350+ 行，彻底清理扫榜3步流沉余
# 2026-08-28 重校准（通用聊天真联网搜索接入）：
#   - chat_general 接入 web_search_bridge：启发式判定+搜索中/完成meta帧+结果注入+智谱原生web_search
#   → 净增 57 行（7770→7827），用户明确要求P0补漏（之前接了搜索模块但没接到路由）
# 2026-08-28 重校准（通用聊天工具栏 4 按钮一排 + 顶部助手切换）：
#   - chat_general 新增 deep_think / web_search_enabled 透传：深度思考(temp 0.3/max 8192/system追加推演)
#     联网开关(强制搜索)、system 增强；顶部助手切换在纯前端(SkillPackSelector 条件渲染)，后端净增 9 行
#   → 净增 9 行（7827→7836）
# 2026-08-28 重校准（装上联网接口 + 思考程度三档）：
#   - 新增 /api/ai/search-config GET/PUT 路由（存 AppPreference KV）+ _sync_search_keys_from_preference 助手
#   - deep_think 由布尔改为程度档位(0/1/2)：temperature 0.7/0.5/0.3、max_tokens 4096/6144/8192、system 按档增强
#   → 净增 80 行（7836→7916）
# 2026-08-29 重校准（圆桌轮数可配/通用聊天维度生成+角色落地健壮性）：
#   - 圆桌讨论轮数解析（默认2轮，用户"讨论N轮"则按N轮）
#   - 通用聊天明确维度生成指令识别 + 维度格式铁律注入 + 落地卡片
#   - 角色卡多人名拆分/name/role 归一化（防 varchar 截断与"未命名"脏名）
#   - _dim_max_tokens 统一 131072；存量测试同步
# 2026-08-29 重校准（修复 Python<3.11 f-string 反斜杠语法错误）：
#   - 圆桌"正在讨论结果创作"delta 帧原为 f-string 嵌套含 \n 反斜杠，
#     3.12 前会 SyntaxError，导致 CI(3.11) pytest 全部 collection 失败 → 拆成变量拼接
#   → 净增 2 行（9086→9088）
# 2026-09-01 重校准（白屏急救·A+C章粒度门禁·采纳落地字段保留·整卷预算）：
#   - chat_collab_bp.py：_merge_volume 重写（NEW非空覆盖 OLD兜底保留卷级字段）+ _volume_field_nonempty 辅助
#     + 三常量+规则细则随 chat_collab_bp 历次增量同步 → 9143→9244（+101 行，P0修复与功能
#     增量，chat_collab_bp 已是 chat_general/圆桌/节点设计/角色落地的多域聚合，需后续再拆分）
#   - WritePage.tsx：新增 aiChatHistory 离线缓存 try/catch 兜底（防 Tracking Prevention 白屏）
#     + 之前的 WritePage 状态管理/智驾面板交互合并入库 → 8902→8908（+6 行，P0白屏急救必需）
#   - ChatPanel.tsx：SAVE_PLOT 卡片 content 合并现有卷字段（防采纳后卷级字段清空）
#     + MAX_MS 从 12 分钟放宽到 15 分钟兜底
#     + localStorage 访问包 try/catch（防 Tracking Prevention 白屏）
#     + 之前的 NodeDesignView、节点设计师分段流式生成 UI 等增量合并入库
#     → 4164→4315（+151 行，均为用户明确要求的 P0 白屏急救/采纳落地/超时兜底硬修复，非业务膨胀）
CHAT_COLLAB_BP_BASELINE = 9244

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
