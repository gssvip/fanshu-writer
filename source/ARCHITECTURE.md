# 蚂蚁写作平台 · 架构说明

> 人机协作半自动化小说写作平台。本文档记录当前模块化分层结构与拆分路线，作为后续维护与重构的事实源。

## 1. 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Flask 3.1 + Flask-SQLAlchemy 3.1 + gunicorn |
| 前端 | React 19 + Vite + Zustand + TypeScript |
| 数据 | PostgreSQL（生产）/ SQLite（本地开发，`FANSHU_DATA_DIR` 指向） |
| 架构门禁 | `scripts/check-architecture.py` |

## 2. 后端分层

```
backend/
├── app.py                  # 应用入口：create_app + 路由汇聚 + 存量辅助逻辑（历史巨石，只减不增）
├── wsgi.py                 # gunicorn 入口
├── extensions.py           # db = SQLAlchemy()（与 app 解耦，避免循环 import）
├── models.py               # 21 个数据模型（User/Book/Chapter/AISession/... 统一导出）
├── safe_degrade.py         # 可选组件静默降级治理（safe_import + FANSHU_STRICT）
├── llm_gateway.py          # LLM 统一网关（模型能力钳制、400 自学习、流式保活）
├── session_persist.py      # 会话消息持久化 + 断流抢救
├── sse_keepalive.py        # SSE 心跳保活
├── entity_registry.py      # 实体注册表（人物/势力/地点/物品/技能 跨维度重命名/合并）
├── generation_gate.py      # 生成门禁（引用校验：正则 → 实体注册表对照）
├── meta_optimizer.py       # 自我进化层（FailureDB + prompt 补丁 + 自动采纳 + 回滚）
├── post_write_validator.py # 正文后置校验（去AI味硬卡）
├── node_design_bp.py       # 节点设计师落地核算
└── blueprints/             # Flask 蓝图（按域拆分）
    ├── auth_bp.py          # 鉴权域（10 路由）
    ├── export_bp.py        # 导入导出域（9 路由）
    ├── books_bp.py         # 书籍/章节域
    ├── ai_analyze_bp.py    # 竞品拆书分析域
    ├── ai_continue_bp.py   # 正文滚动创作域（5 路由：续写/定点修订/批写/SSE）
    ├── ai_continue_helpers.py # 正文滚动创作域·纯辅助函数（章节计划/一致性/指纹/去AI/解析评分）
    ├── ai_config_bp.py     # AI 配置域
    ├── health_bp.py        # 健康检查
    ├── general_chat.py     # 通用对话蓝图
    ├── chat_collab_bp.py   # 智驾协作核心（历史巨石，只减不增，见 §6 路线图）
    ├── chat_smart_gen_bp.py  # 智能生成域（smart_generate / smart_suggest 系列）
    ├── chat_smart_edit_bp.py # 智能编辑·校审域
    ├── chat_smart_fix_bp.py  # 防遗忘修正闭环
    ├── chat_smart_opt_bp.py  # 优化建议报告
    ├── chat_roundtable_bp.py # 圆桌会议
    ├── novel_rank_bp.py    # 榜单风向路由（含 /api/rank/* 与 /api/rankings banner）
    ├── novel_rank_crawlers.py # 榜单抓取套件（健康度/熔断/结构校验/种子数据）
    ├── nd_helpers.py       # 节点续会工具 + 卡片聚合门禁
    ├── nd_apply_card.py    # SAVE_PLOT 采纳：卷级合并 + 节点 A+C 门禁 + 续会增量合并
    └── persona_config.py   # 人设配置（纯数据）
```

## 3. 关键设计模式

### 3.1 App Factory + 扩展解耦
`db` 实例在 `extensions.py` 中独立声明，模型在 `models.py` 中定义，`app.py` 通过 `create_app()` 统一 `init_app`。避免单文件同时承载路由与数据层导致的循环 import 与膨胀。

### 3.2 域蓝图外迁（拆分标准模式）
`chat_collab_bp.py` 末尾的 `_register_split_domains()` 调用各域模块的 `init(**deps)`（注入共享符号）与 `register(bp)`（把路由挂到同一 Blueprint），保证 URL/endpoint 与拆分前完全一致，前端零感知，且避免域模块反向 import 造成的循环依赖。

### 3.3 静默降级（safe_degrade.py）
可选组件（爬虫、MCP、联网搜索等）统一走 `safe_import` / `safe_component` 包装。任一组件异常只记录降级日志、不阻断主流程；设置 `FANSHU_STRICT=1` 时转为严格模式便于排障。

### 3.4 引用校验（entity_registry + generation_gate）
人物/地点/势力引用不再依赖正则粗略匹配，而是优先对照实体注册表，支持多维度实体识别与跨维度重命名/合并，降低误判。

### 3.5 自我进化（meta_optimizer）
`FailureDB` 记录失败案例 → `MetaPromptOptimizer` 分析并生成 prompt 补丁。低风险类别（`format`/`entity`）且失败案例达阈值（≥5 次）时自动采纳；支持手动回滚。

### 3.6 榜单抓取韧性（novel_rank_crawlers）
健康度评分（连续成功/失败加权）+ 熔断冷却（连续 5 次失败触发）+ 结构校验（剔除缺关键字段残条），保证抓取失败可观测且不污染下游。

## 4. 前端结构

```
frontend/src/
├── api.ts                  # 后端接口封装（1359 行）
├── types.ts                # 类型定义
├── constants.ts            # 常量
├── components/
│   ├── ChatPanel.tsx       # 智驾面板（历史巨石，只减不增）
│   ├── ChatPanelCards.tsx  # 卡片/消息气泡子组件（ActionCardView / MessageBubble 等）
│   ├── ChatPanelToolbars.tsx # 工具栏子组件（SkillPackSelector / GeneralAssistantSelector）
│   ├── ChatPanelNodeDesigner.tsx # 节点设计师子组件（parseNodeDesignerProgress + NodeDesignerProgress 进度条）
│   └── NodeDesignView.tsx
├── pages/
│   ├── WritePage.tsx       # 写作主页面（已拆到 write/ 子面板）
│   ├── write/              # 12 个写作面板子组件
│   ├── ToolsPage.tsx       # 工具面板骨架（Tab 切换 + 作品选择器）
│   ├── tools/              # 工具面板 4 个 Tab 子组件（review/skills/analyze/rankings）
│   └── ...
└── hooks/
```

拆分原则：子视图/抽屉/弹窗抽独立组件文件，主页面只保留骨架与状态编排。

## 5. 架构门禁（scripts/check-architecture.py）

- 普通 `.py` / `.tsx` 单文件 ≤ 2000 / 1500 行。
- 豁免文件（`app.py` / `chat_collab_bp.py` / `WritePage.tsx` / `ChatPanel.tsx` / `ToolsPage.tsx`）只受「只能减不能增」约束，基线在脚本内记录每次重校准原因。
- `app.py` 路由数 ≤ 30。

## 6. 拆分路线（剩余目标）

| 目标 | 现状 | 方向 |
|---|---|---|
| `chat_collab_bp.py` | 5039 行 | `smart_generate`/`smart_suggest` 已外迁到 `chat_smart_gen_bp`/`chat_smart_edit_bp`；`apply_card` 节点门禁已外迁到 `nd_apply_card.py` |
| `app.py` | 8836 行 | `ai-continue` 续写域、`dynamic-reports` 域已完成外迁（`ai_continue_bp`/`ai_continue_helpers` 与 `dynamic_reports_bp`） |
| `ChatPanel.tsx` | 3656 行 | `ActionCardView`/`MessageBubble` 已抽到 `ChatPanelCards.tsx`；`NodeDesignerChat` 已独立到 `ChatPanelNodeDesigner.tsx` |
| `ToolsPage.tsx` | 89 行 | 已拆到 `tools/` 4 个 Tab 子组件（review/skills/analyze/rankings） |