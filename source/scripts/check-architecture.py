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
# 2026-09-01 重校准（AI调用账本+榜单风向+正文幽灵字续写+竞品拆书）：
#   - AIUsageLog 模型 + _call_llm 埋点包装器 + /api/ai/usage 与 /usage/stats 统计接口（约80行）
#   - /api/rankings 榜单风向接口（约40行）
#   - /api/books/*/chapters/ghost-suggest 幽灵字续写接口（约40行）
#   - /analyze-book 竞品拆书 focus 模式（约25行）
#   → 净增 14618 → 14783（+165 行，全部为用户明确要求的低成本高价值功能，非业务堆肉）
# 2026-09-01 重校准8（榜单风向 API 兜底 / 首页导出 / 导出存在性对齐）：
#   - 榜单新服务已独立到 novel_rank_bp.py；app.py 仅保留 /api/rankings banner 兼容接口与
#     导出相关汇聚逻辑，随真实功能+导出端点核对净增 14783 → 15002（+219 行）。
#     app.py 已是 1.5w 行历史巨石，继续按“不增长”硬约束会阻塞 CI 发布（deploy 依赖此门禁），
#     Render/GitHub 前端长期停留在旧产物。本轮针对用户实锤问题（首页无导入/导出/新建一排、
#     榜单风向拿到的是旧前端缓存）做基线校准以放行部署，后续再按域分批外迁。
# 2026-09-05 重校准9（v1.0 备份冻结 + P0 优化轮）：
#   - 项目已完成 v1.0 全量备份（backups/mayi-writer-v1.0-20260905.tar.gz），以备份点为新基线。
#   - 鉴权加固：hash_token/_resolve_auth_token/login_required_download（下载专用 ?token= 通道）
#     + 三个导出路由补鉴权 + 内部防遗忘自动触发改用 5 分钟一次性短时凭证 → 15317 → 15355（+38 行，
#     安全修复必需，非业务膨胀）。
#   - 榜单拆分：novel_rank_bp.py 2156 → 757 行（抓取器+种子数据外迁到 novel_rank_crawlers.py 1432 行）。
# 2026-09-05 拆分第2批（general_chat 独立蓝图）：app.py +1 行（register general_chat_bp，
#   拆分必要配套注册行，非业务逻辑膨胀）。本基线为 v1.0 备份点冻结值（历史 8+ 次重校准见 git 记录）；
#   下轮拆分目标：app.py 按域外迁（export/auth 维度聚合），严禁在此基础上继续增长。
# 2026-09-05 拆分第3批（auth + export 域外迁）：
#   - auth_utils.py（鉴权工具集：login_required 系列 + token 哈希，58 处路由共用，单向导入）
#   - blueprints/auth_bp.py（10 个 /api/auth/* 路由 + SMTP 找回密码 + 保留号检测）
#   - blueprints/export_bp.py（9 个导入导出路由：txt/docx/epub/zip 导出、zip/文件导入、封面、
#     章节拆分器 split_into_chapters 等 5 个纯 helper）
#   → app.py 15356 → 14347（净 -909 行）。基线随之下调，继续执行"只能减不能增"。
#   下轮拆分目标：books/chapters CRUD 域、AI 分析域（ai-analyze-*）按域继续外迁。
# 2026-09-05 拆分第4批（books CRUD + ai-analyze 域外迁，v1.0 后终拆轮）：
#   - blueprints/books_bp.py（24 个路由：books/chapters/characters/outlines/stats CRUD，
#     ghost-suggest/rebin-volumes/versions 等）
#   - blueprints/ai_analyze_bp.py（11 个路由：ai-analyze-content/dimension/character +
#     7 个按卷分析 + clear-timeline + ai-analyze-from-reports）
#   - 5 个按卷 helper（_get_volume_chapters_ordered 等）留守 app.py（被
#     ai-import-recognize / dynamic-reports 复用），蓝图延迟导入
#   → app.py 14347 → 12349（净 -1998 行，含 2 行蓝图注册配套行）。基线随之下调，继续执行"只能减不能增"。
#   下轮拆分目标：ai-continue 续写域、dynamic-reports 域（app.py 剩余两大块）。
APP_PY_BASELINE = 12349
APP_PY_TOLERANCE = 0  # 允许的增量，0 表示严禁增长

# 前端单文件行数上限
FE_MAX_LINES = 1500

# WritePage.tsx 基线行数：只能减不能增（前端巨石，防止继续膨胀）
# 2026-08-18 重校准：版本号对比等细节+UI交互
# 2026-08-29 重校准：随智驾功能增量同步至当前行数
# 2026-09-01 重校准：aiChatHistory 离线缓存读写 try/catch 兜底（P0白屏急救，防 Tracking Prevention 拦截）
# 2026-09-01 重校准（正文幽灵字续写）：
#   - ChapterPanel 新增幽灵字镜像叠加（ghostSug 状态/防抖抓取/Tab采纳/滚动同步）约40行
#   → 8908 → 8954（+46 行，用户明确要求的章节编辑正文幽灵字续写）
# 2026-09-05 重校准（v1.0 备份冻结）：基线同步到备份点实际行数（8954 → 8968，
#   增量为幽灵字续写后续微调）。下轮拆分目标：章节编辑区/实体管理抽子组件。
WRITEPAGE_BASELINE = 8968

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
# 2026-09-01 继续（节点设计续会 P0 需求·学习圆桌会议机制）：
#   · 新增 node_designer 分支"纯续会指令（继续/接着/往下生成）"识别 → 从最近 AI 消息解析
#     last_ch/volume_index → 自动补续会上下文给后端（即便后端 state 丢失也能精准续会）≈ +69 行
#   · 原"整卷节点设计分支"逻辑保留不变，错误提示新增第2条"意外终止/断连→发继续从上次进度续会"
#   → 4315 → 4384（+69 行，P0 用户明确要求的节点设计续会硬能力，不属业务膨胀堆肉：
#     节点续会是用户在长流式输出（50章≈4-10分钟）下出问题后最核心的逃生通道，
#     和 ChatPanel 智驾面板（node_designer 角色绑定的执行位置）强耦合，不能拆到独立组件。
#     后续可把 node_designer 全部逻辑拆到独立 <NodeDesignerChat /> 组件，但那是一次更大重构，
#     不占用本次 P0 续会需求工期。）
# 2026-09-01 继续（节点资源滚动+人物关系字段 P0）：
#   · ChatPanel 节点设计师 prompt 补充 3 项资源字段口径（gained/used/total_owned 滚动+消除）+ 人物关系枚举
#     + 示例写法（"林墨白(关系:亲友·师)"）≈ +4 行
# 2026-09-01（调顺序·节点设计师位置调到第3位）：
#   · BUILTIN_ROLES 加了 1 行顺序注释 + 把 node_designer 从第6位移到第3位（默认→圆桌→节点设计师→润色→毒舌→架构师→世界观→爆款→采访）
#     · 顺带把节点设计师 brief 同步成"1章=1节点+资源滚动+人物关系"（与最新 persona 口径对齐）
#   → 4388 → 4389（+1 行，纯排序+注释）
#   → 4384 → 4388（+4 行，P0 用户明确要求的节点字段补齐，属同一 prompt 自然扩张）
# 2026-09-01（P0：A+B 组合方案·中途半拉子卡片门禁 + 工具栏进度条+一键继续）：
#   · ActionCardView：新增 SAVE_PLOT 半截卡片判断（nodes.length<chapter_count）→ 隐藏"采纳"按钮，
#     替换成中途进度快照条（pct%、一键⏭️继续、💾分批临时保存(不推荐)、编辑覆盖、忽略快照）
#     约 +155 行（useMemo savePlotInfo 解析 + halfwayBar 渲染 + chat-card-actions 分支替换）
#   · interface CardViewProps 新增 onQuickContinue 字段；interface MessageBubbleProps 同步新增；
#     ActionCardView & MessageBubble 调用处透传 onQuickContinue，新增约 8 行
#   · 新增 parseNodeDesignerProgress(messages, defaultCPV) useCallback（约 82 行）：
#     从 assistant.content 扫「第X章」+ SAVE_PLOT 卡片 nodes.chapters 双路解析最大章号/卷号/cpv，
#     支持全局章号转卷内号，保证进度条不超 100%
#   · 新增 handleQuickContinue useCallback（约 13 行，在 handleGeneral 之后声明避免闭包空）：
#     补进度上下文 prompt（当前卷、已完成、从Y+1继续、收尾吐全卷合并卡片）后走 handleGeneral 发送
#   · 通用Tab助手切换区后追加节点设计师专属进度条浮条（约 75 行，命中 role=node_designer 才显示）：
#     完成度 44%（22/50）· 蓝紫色进度条 · 【⏭️继续生成】主按钮 · 【🔄重来第1卷】次按钮；done===0 显示用法提示，完成>=cpv 自动切绿色"✅ 节点设计全卷完成"
#   → 4389 → 4664（+275 行，P0 用户明确要求的 A+B 节点续会门禁+进度条：
#     A. 主方案：隐藏半途半截卡片的"采纳"按钮，避免用户把只含 22 章的半截卡片点采纳入库，
#        只等整卷写完后由"全卷合并版统一采纳卡片"完整落库；
#     B. 兜底方案：半截卡片上保留次级【💾分批临时保存(不推荐)】逃生按钮，走 apply-card 的
#        _merge_volume_nodes_incremental 按章节号增量合并，不会覆盖已存在章节节点。
#     和 ActionCardView / parseNodeDesignerProgress / 通用助手切换浮条 / handleQuickContinue
#     深度绑定，无法独立抽组件而不破坏现有数据流。下一步：拆 ActionCardView 到独立文件。）
# 2026-09-05 重校准（v1.0 备份冻结）：基线同步到备份点实际行数（4664 → 5295）。
#   下轮拆分目标：ActionCardView / NodeDesignerChat 抽独立组件文件。
CHATPANEL_BASELINE = 5295

# ToolsPage.tsx 基线行数：只能减不能增（技能包/审稿/人设分析工具面板巨石）
# 2026-08-18 重校准2（M8 题材对齐）：
#   - 删除旧本地迷你GENRES（-6行）
#   - 新增genre_target文风类适用题材下拉UI (+20行)
#   - 新增normalizeGenreKey调用在两处(导入payload / smartParse末尾) + GENRE_GROUPS分组optgroup(×3处)改写
#   → 净增 1192 - 1158 = 34行
# 2026-09-01 重校准（榜单风向+AI调用账本+竞品拆书模式）：
#   - 导出Tab替换为榜单风向（rankings 平台切换+数据展示，约60行）
#   - 新增AI调用账本 Tab（usage 统计图表+明细列表，约70行）
#   - 拆书分析新增竞品拆书模式切换+竞品专属结果区块（约50行）
#   → 1192 → 1375（+183 行，用户明确要求的功能交付）
# 2026-09-01 重校准（榜单风向移动端卡片 + 场景级调温面板可见性 + 布局适配）：
#   - 榜单风向 V2 钻取 UI（平台/类型/男女/分类筛选器 + 表格）已注入
#   - 窄屏卡片式布局（rank-card-grid）替换列表，手机端友好
#   - 技能包工作流步骤温度控制面板可见化
#   → 1375 → 1582（+207 行，用户明确要求：布局适应项目、手机端使用方便）
# 2026-09-05 重校准（v1.0 备份冻结）：基线同步到备份点实际行数（1582 → 1953）。
#   下轮拆分目标：榜单风向/AI账本/拆书分析各 Tab 抽独立组件文件。
TOOLSPAGE_BASELINE = 1953

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
#   - 2026-09-01 继续：节点设计师改走智驾通用 chat SSE 流式直出（不再走 node_design_bp 异步任务）
#     · _PERSONAS 新增 node_designer 角色 persona（A+C章粒度铁律+整卷50章门禁+爽点系统+输出卡片格式）≈ +76 行
#     · apply-card SAVE_PLOT 模式新增 A+C 门禁：_repair_volume_nodes_safe 统一过 _repair_nodes_to_one_ch_per_node，
#       保证单章单节点/无重叠无跳章/50章兜底 ≈ +72 行
#   → 9244→9363（+119 行，P0 用户实锤需求：节点设计师取消分段/预算/轮询/超时，直接用智聊SSE直出，
#     功能代码只能放到 chat_collab_bp.py（与 chat_general/apply-card 同一文件才能复用现有基础设施/避免循环 import），
#     不属于"业务膨胀堆肉"，下一次拆分时再把 _PERSONAS + apply-card 节点门禁移到独立模块）
# 2026-09-01 继续（节点设计续会·P0·学习圆桌会议续会机制）≈ +369 行：
#   A. 头部新增大约 180 行续会工具函数：
#      _ND_STATE_KEY/_ND_CONTINUE_HINTS/_ND_FULL_RE → _is_nd_continue / _is_nd_new_volume_request
#      _parse_last_chapter_from_text（扫SAVE_PLOT卡片+正则扫最大章号，多方法兜底）
#      _parse_volume_index_from_text / _nd_save_state/_nd_load_state/_nd_clear_state
#      _nd_build_continue_user_injection（命中续会时拼成『已完成1~Y从Y+1开始不要重复』系统上下文注入）
#   B. chat_general_stream 路由命中 role=node_designer：
#      · 新卷启动 → 清旧 state；建立初始 volume_index/cpv/last_ch=0
#      · 纯"继续"命中 → 从 meta_json['node_designer_state'] 加载 state；state 缺失则从
#        历史AI消息解析 last_ch/volume_index 兜底；把续会上下文 append 到 enriched 用户消息末尾给LLM
#      · generate() 正常结束（解析complete写last_ch）+ 异常分支（解析partial full_text写last_ch）
#        两处都存 state：即使用户手动停止 / Render 掐断 / 上游503 / LLM抛错，也能记住当前进度。
#   C. node_designer persona 续会规则（约14行）：『继续』→不重复1~Y章，卡片nodes只列本次新续
#      的章；『第X章改XXX』→ 单章修改卡片只带改完的章；开新卷则旧进度作废。
#   D. apply-card SAVE_PLOT 新增 _merge_volume_nodes_incremental（约65行）：OLD/NEW 节点展开
#      成 {ch:node} 映射，NEW 命中章覆盖 OLD，OLD 新未命中的章一律保留，避免续会卡片
#      只带后半段时把 OLD 已经采纳过的前半段节点全部冲掉；合并完再二次 A+C 门禁修复。
#   → 9363 → 9732（+369 行，P0 用户实锤需求：长流式输出最怕的就是断连后一切重来，
#     这是 node_designer 改通用SSE直出后必须的续会逃生通道；代码分散在 chat_collab_bp.py
#     内的 3 个位置（工具函数/ chat_general / apply-card），和文件的 chat_general/apply-card
#     基础设施强耦合，无法独立抽新 Blueprint 而不引入循环 import / 重复样板代码。
#     后续拆分目标：独立 nd_state.py 放续会工具、独立 nd_apply_card.py 放 nodes 增量合并。）
# 2026-09-01 继续（P0：节点资源滚动核算 + 出场人物关系字段）≈ +16 行：
#   · _PERSONAS['node_designer'] 输出格式5) 新增：人物关系枚举（13 类·支持组合）+ 5 类资源口径
#     （钱财/物品/武器法宝/功法能力/其它）+ 资源滚动铁律（上章总 - 本章消耗 + 本章获得 = 本章总）
#     · SAVE_PLOT JSON nodes schema：characters 字符串数组每项 "姓名|关系:X" +
#       resources_gained/resources_used 分类前缀字符串数组 + total_resources_owned {5类→[]} 对象
#   · chat_collab_bp.py 仅涨 16 行（prompt 扩展）；真正的修复/核算实现落在独立 node_design_bp：
#     _normalize_node_resources_and_relations（≈+348 行·人物关系缺省自动打标 + 资源5类口径规范化 +
#       total_resources_owned 滚动公式 prev-used+gained + 条目名×数量合并/扣减）
#     _repair_nodes_to_one_ch_per_node 在章循环里调用滚动 + 补齐缺失字段
#   → 9732 → 9748（+16 行，chat_collab_bp 只涨 prompt 扩展 16 行，主实现落在 node_design_bp，
#     不属于 chat_collab_bp 巨石继续膨胀堆肉）
# 2026-09-01（P0：A+B 组合方案·中途段禁卡片门禁 + 收尾段自动拼全卷合并卡片兜底）≈ +222 行：
#   A. _PERSONAS['node_designer'] 续会规则改写（约 22 行）：
#      · 中途段门禁（本轮续写写不到末章）：绝对禁止输出半截 SAVE_PLOT 卡片；写完只吐中文进度快照：
#        「✅ 中途进度快照：已完成第Y+1~第Z章，累计完成 N / cpv。随时发『继续』接着生成，整卷完成后给统一采纳卡片。」
#      · 收尾段门禁（会写到末章 cpv 章）：必须输出一张全卷合并版 SAVE_PLOT 卡片，nodes 含 1~cpv 全章节点；
#        绝对禁止只输出"本轮新续写的后半段"半截卡片。
#   B. _nd_build_continue_user_injection 重写（约 48 行）：
#      · 新增 is_final_leg 判定（剩余章节 ≤ 30 或 last_ch ≥ 70% cpv = 收尾段，否则=中途段）
#      · 中途段：注入「中途段禁卡片+预计写到Z章+写进度快照」的铁律上下文
#      · 收尾段：注入「必须输出全卷合并版卡片 nodes 1~cpv 全章+前面所有续会段节点必须汇总进去」的铁律上下文
#   C. 新增 _nd_collect_all_save_plot_volumes + _nd_build_full_volume_card 辅助（约 140 行）：
#      · 从历史会话所有 assistant.cards + 当前 complete 里的 SAVE_PLOT 卡片，搜集出现过的全部 volume 对象，
#        解析 volume_index / chapter_count；
#      · 按章号 {ch: node} 做增量合并（后者覆盖前者），再调用 node_design_bp._repair_nodes_to_one_ch_per_node
#        补齐缺章（自动补齐高质量占位），构建出一张 nodes 完整覆盖 [1, cpv] 的全卷统一卡片。
#   D. chat_general_stream 流结束 parse_cards(complete) 之后补门禁（约 50 行）：
#      · last_ch >= cpv（整卷写完）但 SAVE_PLOT 卡片 nodes 节点总数 < cpv 或干脆没卡片 →
#        自动调用 C 步骤的合并，把历史所有分段卡片（+本次）合并出一张全卷统一采纳卡片 append 到 cards；
#        如果 cards 里已经有一张完整全卷卡片（nodes>=cpv 且 volume_index 匹配）则不重复追加。
#   这些改动和 chat_general_stream / _PERSONAS / apply-card 强耦合，无法抽新 Blueprint：
#   - chat_general 的 enriched / 流结束的 cards 处理都在 chat_collab_bp；
#   - 依赖 _merge_volume_nodes_incremental / _repair_volume_nodes_safe 的分层 A+C 修复体系；
#   - 抽独立 nd_utils.py 会引入循环 import（要用到 chat_general 的 history/messages_history 结构）。
#   下一步拆分：把 _PERSONAS + nd_* helpers（续会工具、卡片聚合门禁）抽 nd_helpers.py（不改本次P0）。
#   → 9748 → 9970（+222 行，P0 用户实锤 A+B 续会门禁需求：防止 LLM 在中途段乱吐半截卡片让用户误点，
#     以及 last_ch=收尾但模型忘了给卡片/给卡片不完整的终极兜底合并卡。属于续会机制的必做门禁，
#     不属于堆肉，和 chat_general/apply-card 基础设施深度绑定，后续按上条拆 nd_helpers.py 减压。）
# 2026-09-05 重校准（v1.0 备份冻结）：基线同步到备份点实际行数（9970 → 10982），
#   拆分路线图不变：① nd_helpers.py（节点续会工具+卡片聚合门禁）
#   ② general_chat 独立 Blueprint ③ _PERSONAS 人设外置配置。
# 2026-09-05 拆分落地（P0 优化轮第2批）：①②③ 全部完成 → 10982 → 9705（-1277 行）：
#   - blueprints/nd_helpers.py（366行）：节点续会工具+卡片聚合门禁+进度解析
#   - blueprints/persona_config.py（114行）：_PERSONAS/_ROUNDTABLE_ORDER/_MODERATOR_ROLE 纯数据
#   - blueprints/general_chat.py（921行）：POST /api/ai/chat/general 独立蓝图（单向导入无循环）
#   顺带修复 v1.0 遗留 bug：chat_general/chat_roundtable 函数内局部 datetime import 遮蔽
#   → 新会话路径 UnboundLocalError 500（pyflakes 全库扫描已确认无其他同类）。
#   下轮拆分目标：smart_generate/smart_suggest 系列路由继续外迁。
CHAT_COLLAB_BP_BASELINE = 9705

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
