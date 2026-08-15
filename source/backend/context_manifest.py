"""Context Manifest：章节生成前的上下文溯源 + 预算管理。

借鉴司命 siming-ai 的 ContextOrchestrator 设计，解决番茄项目"无法溯源
为什么这章引用了过时设定"的问题：

  - 章节生成前生成 ContextManifest，记录注入了哪些 bible 片段 + 各自 hash
  - token 预算校验（防止上下文超限导致 LLM 截断）
  - 失效检测：bible 改了 → 旧 manifest 标 stale
  - 落库可查：每章生成记录其上下文来源，支持事后审计

使用方式：
    from context_manifest import ContextManifest, ContextOrchestrator
    orch = ContextOrchestrator()
    manifest = orch.prepare(
        sources={'key_rules': '...', 'worldbuilding': '...'},
        chapter_num=5, book_id='xxx')
    if manifest.needs_truncation():
        sources = manifest.truncate_to_budget(8000)
    # ... 用 sources 构建 prompt ...
    # 章节生成后落库
    orch.persist(manifest, chapter_id='yyy')
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ManifestState(Enum):
    """Manifest 生命周期状态。"""
    READY = "ready"              # 已构建，可用
    NEEDS_TRUNCATION = "needs_truncation"  # 超 token 预算，需截断
    STALE = "stale"              # 上游 bible 已变更，需重建
    BLOCKED = "blocked"          # 无法构建（缺关键源）


@dataclass
class ContextSource:
    """单个上下文来源条目。"""
    name: str                    # 来源名（如 key_rules/worldbuilding）
    content: str                 # 注入的文本
    content_hash: str = ""       # 内容 hash（用于失效检测）
    token_estimate: int = 0      # 预估 token 数
    priority: int = 0            # 优先级（截断时按优先级保留）

    def __post_init__(self):
        if not self.content_hash and self.content:
            self.content_hash = hashlib.sha1(
                self.content.encode("utf-8")
            ).hexdigest()[:16]
        if not self.token_estimate and self.content:
            # 粗估：中文约 1.5 字/token，英文约 4 字符/token
            cn = len([c for c in self.content if '\u4e00' <= c <= '\u9fff'])
            other = len(self.content) - cn
            self.token_estimate = int(cn / 1.5 + other / 4)


@dataclass
class ContextManifest:
    """章节生成的上下文清单。

    记录本次 LLM 调用注入了哪些 bible 片段、各自 hash、总 token 预算。
    落库后可做溯源与失效检测。
    """
    book_id: str
    chapter_num: int
    sources: list[ContextSource] = field(default_factory=list)
    total_tokens: int = 0
    token_budget: int = 12000    # S2：默认从 8k 升级到 12k
    state: ManifestState = ManifestState.READY
    created_at: float = field(default_factory=time.time)
    manifest_id: str = ""

    def __post_init__(self):
        if not self.manifest_id:
            # 基于内容生成 manifest_id
            raw = f"{self.book_id}:{self.chapter_num}:{self.created_at}"
            self.manifest_id = hashlib.sha1(
                raw.encode("utf-8")
            ).hexdigest()[:12]
        self.total_tokens = sum(s.token_estimate for s in self.sources)

    @property
    def is_over_budget(self) -> bool:
        return self.total_tokens > self.token_budget

    @property
    def source_hashes(self) -> dict[str, str]:
        """返回 {source_name: hash}，用于失效检测。"""
        return {s.name: s.content_hash for s in self.sources}

    def needs_truncation(self) -> bool:
        """是否需要截断（超预算）。"""
        return self.is_over_budget

    def truncate_to_budget(self, budget: int | None = None) -> dict[str, str]:
        """按优先级截断到预算内，返回保留的 sources dict。"""
        budget = budget or self.token_budget
        # 按 priority 降序排列
        sorted_sources = sorted(self.sources, key=lambda s: -s.priority)
        kept: dict[str, str] = {}
        used = 0
        for src in sorted_sources:
            if used + src.token_estimate <= budget:
                kept[src.name] = src.content
                used += src.token_estimate
            elif budget - used > 200:  # 还有空间就截断保留
                # 保留前 N 字符
                max_chars = int((budget - used) * 1.5)
                kept[src.name] = src.content[:max_chars] + "\n[...已截断...]"
                used = budget
            # 没空间的跳过
        return kept

    def to_dict(self) -> dict:
        return {
            "manifest_id": self.manifest_id,
            "book_id": self.book_id,
            "chapter_num": self.chapter_num,
            "sources": [
                {"name": s.name, "hash": s.content_hash, "tokens": s.token_estimate,
                 "priority": s.priority, "preview": s.content[:80]}
                for s in self.sources
            ],
            "total_tokens": self.total_tokens,
            "token_budget": self.token_budget,
            "state": self.state.value,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class ContextOrchestrator:
    """上下文编排器：构建 + 校验 + 持久化 ContextManifest。

    在章节生成前调用 prepare() 生成 manifest，
    生成后调用 persist() 落库（落库到 chapter manifest_json 字段）。
    """

    # 来源优先级（截断时高优先级先保留）
    SOURCE_PRIORITY = {
        "key_rules": 100,         # 核心设定，最高优先
        "character_profiles": 95,  # 人设
        "plot_design": 90,        # 总纲
        "outline_hierarchy": 85,  # 章节大纲
        "worldbuilding": 80,      # 世界观
        "timeline": 75,           # 分卷剧情
        "foreshadowing": 70,      # 伏笔
        "concept": 60,            # 构思
        "prev_chapter": 50,       # 上一章正文
        "dynamic_report": 40,     # 动态报告
        "skill_pack": 30,         # 技能包
    }

    # 模型上下文窗口（启发式）。命中关键词 → 用对应值，否则默认 16k
    _MODEL_CONTEXT_WINDOWS: list[tuple[set[str], int]] = [
        # 32k 档
        ({'gpt-4o-mini', '4o-mini', 'claude-3-haiku', 'haiku', 'qwen2.5-14b', 'qwen2-14b', 'qwen2.5-7b', 'deepseek-v3', 'deepseek-chat-v3'}, 32768),
        # 128k 档
        ({'gpt-4o', '4o', 'o1', 'o3-mini', 'claude-3.5-sonnet', 'sonnet-4', 'sonnet', 'claude-3-opus', 'opus', 'gemini-2.0', 'gemini-1.5', 'gpt-4-turbo', 'qwen2.5-72b', 'qwen3-72b', 'yi-large', 'glm-4'}, 128000),
        # 200k 档
        ({'claude-sonnet-4', 'claude-opus', 'qwen-long', 'gemini-2.5', 'doubao-pro-32k', 'doubao-1.5-pro-256k', 'moonshot-v1-128k', 'moonshot-v1-8k-128k'}, 200000),
    ]
    DEFAULT_CONTEXT_WINDOW = 16384  # 未知模型：保守 16k

    @staticmethod
    def _heuristic_context_window(model_name: str | None) -> int:
        if not model_name:
            return ContextOrchestrator.DEFAULT_CONTEXT_WINDOW
        m = (model_name or '').lower()
        for keys, ctx in ContextOrchestrator._MODEL_CONTEXT_WINDOWS:
            if any(k in m for k in keys):
                return ctx
        return ContextOrchestrator.DEFAULT_CONTEXT_WINDOW

    @staticmethod
    def dynamic_budget(
        max_gen_tokens: int = 4000,
        model_name: str | None = None,
        system_prompt_estimate: int = 1000,
        ceiling: int = 12000,
    ) -> int:
        """S2：动态上下文预算。

        预算 = min( ceiling, 模型窗口 - 生成预算 - system_prompt - 1000(对话头余量) )
        - 未知模型默认 16k 窗口 → 若 ceiling=12k 则直接 12k
        - 128k 档模型：有必要时允许自动放宽到 ceiling（可传 ceiling=16k~20k）
        """
        ctx_win = ContextOrchestrator._heuristic_context_window(model_name)
        headroom = 1000 + max(0, system_prompt_estimate) + max(0, max_gen_tokens)
        budget = ctx_win - headroom
        # 下限：至少 8k（保证 bible 最小可用片段能塞下）
        if budget < 8000:
            budget = 8000
        if budget > ceiling:
            budget = ceiling
        return int(budget)

    def __init__(self, token_budget: int = 12000):
        # S2：默认预算 8k → 12k
        self.token_budget = token_budget

    def prepare(self, sources: dict[str, str], chapter_num: int,
                book_id: str, token_budget: int | None = None) -> ContextManifest:
        """构建 ContextManifest。

        Args:
            sources: {source_name: content} 字典
            chapter_num: 当前章节号
            book_id: 书 ID
            token_budget: token 预算上限（可选）

        Returns:
            ContextManifest，调用方可检查 needs_truncation() 并截断
        """
        budget = token_budget or self.token_budget
        src_list = []
        for name, content in sources.items():
            if not content or not content.strip():
                continue
            priority = self.SOURCE_PRIORITY.get(name, 10)
            src = ContextSource(name=name, content=content, priority=priority)
            src_list.append(src)

        manifest = ContextManifest(
            book_id=book_id,
            chapter_num=chapter_num,
            sources=src_list,
            token_budget=budget,
        )

        if manifest.is_over_budget:
            manifest.state = ManifestState.NEEDS_TRUNCATION

        return manifest

    def persist(self, manifest: ContextManifest, chapter_id: str = "",
                db_session=None, ChapterModel=None) -> bool:
        """将 manifest 落库到章节的 manifest_json 字段。

        Args:
            manifest: ContextManifest 对象
            chapter_id: 章节 ID
            db_session: SQLAlchemy session（可选）
            ChapterModel: Chapter 模型类（可选）

        Returns:
            bool: 是否成功落库
        """
        if not chapter_id or db_session is None or ChapterModel is None:
            return False
        try:
            ch = db_session.query(ChapterModel).get(chapter_id)
            if ch:
                # 存到 manifest_json 字段（若不存在则跳过）
                if hasattr(ch, "manifest_json"):
                    ch.manifest_json = manifest.to_json()
                    db_session.commit()
                    return True
        except Exception:
            pass
        return False

    def check_stale(self, manifest: ContextManifest,
                    current_sources: dict[str, str]) -> bool:
        """检查 manifest 是否过期（上游 bible 内容变了）。

        Args:
            manifest: 旧的 ContextManifest
            current_sources: 当前的 {source_name: content}

        Returns:
            bool: True 表示已过期，需重建
        """
        for src in manifest.sources:
            current_content = current_sources.get(src.name, "")
            if not current_content:
                continue
            current_hash = hashlib.sha1(
                current_content.encode("utf-8")
            ).hexdigest()[:16]
            if current_hash != src.content_hash:
                return True
        return False
