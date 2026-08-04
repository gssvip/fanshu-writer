"""Run Recovery：幂等键 + 运行恢复。

借鉴司命 siming-ai 的 IdempotencyKey + RunRecoveryService 设计，
解决番茄项目"连续创作失败只能整批 break，用户得从头再来"的问题：

  - 幂等键：章节写入用自然键，重试不会重复落库
  - 运行恢复：失败后支持单步 retry / resume 从某步继续 / resume 整个 run
  - 状态追踪：记录每步状态（pending/running/succeeded/failed/skipped）

使用方式：
    from run_recovery import IdempotencyKey, RunRecoveryService, StepStatus

    # 幂等键
    key = IdempotencyKey.create_chapter("book_123", outline_node_id=5)
    if key.exists(db_session, ChapterModel):
        return existing_chapter  # 重试不重复

    # 运行恢复
    svc = RunRecoveryService(db_session, RunLogModel)
    run_id = svc.start_run("batch_create", book_id="book_123",
                           chapters=list(range(1, 11)))
    for ch_num in chapters:
        step_id = svc.start_step(run_id, f"chapter_{ch_num}")
        try:
            # ... 生成章节 ...
            svc.complete_step(step_id, result={"chapter_id": "..."})
        except Exception as e:
            svc.fail_step(step_id, error=str(e))
            break

    # 失败后恢复
    failed_steps = svc.get_failed_steps(run_id)
    for step in failed_steps:
        svc.retry_step(step.id)  # 单步重试
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(Enum):
    """运行步骤状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class IdempotencyKey:
    """幂等键：防止重复操作。

    章节创建/重写用自然键，重试时检查是否已存在，避免重复落库。
    """
    operation: str          # 操作类型（create_chapter/rewrite_chapter/...）
    entity_type: str        # 实体类型（chapter/character/...）
    natural_key: str        # 自然键（book_id + outline_node_id 等）
    key_hash: str = ""      # 完整 key 的 hash

    def __post_init__(self):
        raw = f"{self.operation}:{self.entity_type}:{self.natural_key}"
        self.key_hash = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def create_chapter(cls, book_id: str, outline_node_id: int | str = "",
                       chapter_num: int = 0) -> "IdempotencyKey":
        """章节创建幂等键。"""
        natural = f"{book_id}:{outline_node_id or chapter_num}"
        return cls("create_chapter", "chapter", natural)

    @classmethod
    def rewrite_chapter(cls, book_id: str, chapter_id: str,
                        content_hash: str = "") -> "IdempotencyKey":
        """章节重写幂等键。"""
        natural = f"{book_id}:{chapter_id}:{content_hash}"
        return cls("rewrite_chapter", "chapter", natural)

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "entity_type": self.entity_type,
            "natural_key": self.natural_key,
            "key_hash": self.key_hash,
        }

    def exists(self, db_session, ModelClass) -> bool:
        """检查此幂等键是否已执行过（查 idempotency_key 字段）。

        需要模型有 idempotency_key 字段。
        """
        if not hasattr(ModelClass, "idempotency_key"):
            return False
        try:
            existing = db_session.query(ModelClass).filter_by(
                idempotency_key=self.key_hash
            ).first()
            return existing is not None
        except Exception:
            return False

    def get_existing(self, db_session, ModelClass):
        """获取已存在的实体（若幂等键命中）。"""
        if not hasattr(ModelClass, "idempotency_key"):
            return None
        try:
            return db_session.query(ModelClass).filter_by(
                idempotency_key=self.key_hash
            ).first()
        except Exception:
            return None


@dataclass
class RunStep:
    """运行步骤记录。"""
    step_id: str
    run_id: str
    step_name: str           # 步骤名（如 chapter_5）
    step_index: int          # 步骤序号
    status: StepStatus = StepStatus.PENDING
    result: dict = field(default_factory=dict)
    error: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    attempt: int = 0         # 重试次数

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "step_name": self.step_name,
            "step_index": self.step_index,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempt": self.attempt,
        }


class RunRecoveryService:
    """运行恢复服务。

    记录每次批量运行（如连续创作）的步骤状态，
    支持失败后单步重试 / 从某步继续 / 整个 run 恢复。

    状态存储：优先落库到 run_log 表（若有），否则内存存储。
    """

    def __init__(self, db_session=None, RunLogModel=None):
        self.db_session = db_session
        self.RunLogModel = RunLogModel
        # 内存存储（无 DB 时的 fallback）
        self._runs: dict[str, dict] = {}
        self._steps: dict[str, RunStep] = {}

    def start_run(self, operation: str, book_id: str = "",
                  chapters: list[int] | None = None) -> str:
        """开始一次运行，返回 run_id。"""
        run_id = uuid.uuid4().hex[:16]
        run_record = {
            "run_id": run_id,
            "operation": operation,
            "book_id": book_id,
            "chapters": chapters or [],
            "started_at": time.time(),
            "status": "running",
        }
        self._runs[run_id] = run_record

        # 落库（若支持）
        if self.db_session and self.RunLogModel:
            try:
                log = self.RunLogModel(
                    run_id=run_id,
                    operation=operation,
                    book_id=book_id,
                    status="running",
                    details_json=json.dumps(run_record, ensure_ascii=False),
                )
                self.db_session.add(log)
                self.db_session.commit()
            except Exception:
                pass

        # 预创建步骤
        for i, ch_num in enumerate(chapters or []):
            step_id = f"{run_id}_step_{i}"
            step = RunStep(
                step_id=step_id, run_id=run_id,
                step_name=f"chapter_{ch_num}", step_index=i,
            )
            self._steps[step_id] = step

        return run_id

    def start_step(self, run_id: str, step_name: str) -> str:
        """标记步骤开始执行，返回 step_id。"""
        # 查找已预创建的步骤
        for step_id, step in self._steps.items():
            if step.run_id == run_id and step.step_name == step_name:
                step.status = StepStatus.RUNNING
                step.started_at = time.time()
                step.attempt += 1
                self._persist_step(step)
                return step_id

        # 未预创建，新建
        step_id = f"{run_id}_step_{uuid.uuid4().hex[:8]}"
        step = RunStep(
            step_id=step_id, run_id=run_id,
            step_name=step_name, step_index=len(self._steps),
            status=StepStatus.RUNNING, started_at=time.time(), attempt=1,
        )
        self._steps[step_id] = step
        self._persist_step(step)
        return step_id

    def complete_step(self, step_id: str, result: dict | None = None):
        """标记步骤成功完成。"""
        step = self._steps.get(step_id)
        if step:
            step.status = StepStatus.SUCCEEDED
            step.result = result or {}
            step.completed_at = time.time()
            self._persist_step(step)

    def fail_step(self, step_id: str, error: str = ""):
        """标记步骤失败。"""
        step = self._steps.get(step_id)
        if step:
            step.status = StepStatus.FAILED
            step.error = error
            step.completed_at = time.time()
            self._persist_step(step)

    def skip_step(self, step_id: str, reason: str = ""):
        """标记步骤跳过。"""
        step = self._steps.get(step_id)
        if step:
            step.status = StepStatus.SKIPPED
            step.error = reason
            step.completed_at = time.time()
            self._persist_step(step)

    def get_failed_steps(self, run_id: str) -> list[RunStep]:
        """获取 run 中所有失败的步骤。"""
        return [
            s for s in self._steps.values()
            if s.run_id == run_id and s.status == StepStatus.FAILED
        ]

    def get_run_status(self, run_id: str) -> dict:
        """获取 run 的整体状态。"""
        run = self._runs.get(run_id, {})
        steps = [s for s in self._steps.values() if s.run_id == run_id]
        succeeded = sum(1 for s in steps if s.status == StepStatus.SUCCEEDED)
        failed = sum(1 for s in steps if s.status == StepStatus.FAILED)
        pending = sum(1 for s in steps if s.status == StepStatus.PENDING)
        running = sum(1 for s in steps if s.status == StepStatus.RUNNING)

        return {
            "run_id": run_id,
            "operation": run.get("operation", ""),
            "book_id": run.get("book_id", ""),
            "total_steps": len(steps),
            "succeeded": succeeded,
            "failed": failed,
            "pending": pending,
            "running": running,
            "status": "completed" if failed == 0 and pending == 0 and running == 0
                     else "failed" if failed > 0
                     else "running",
            "failed_steps": [s.to_dict() for s in steps if s.status == StepStatus.FAILED],
            "resumable": failed > 0 or pending > 0,
        }

    def get_resume_point(self, run_id: str) -> int | None:
        """获取恢复点：第一个失败/待执行的步骤 index。

        返回 step_index，调用方可从该步骤继续。
        """
        steps = [s for s in self._steps.values() if s.run_id == run_id]
        steps.sort(key=lambda s: s.step_index)
        for step in steps:
            if step.status in (StepStatus.FAILED, StepStatus.PENDING):
                return step.step_index
        return None  # 全部完成

    def retry_step(self, step_id: str) -> bool:
        """重置步骤为 pending，允许重试。返回是否可重试。"""
        step = self._steps.get(step_id)
        if step and step.status == StepStatus.FAILED:
            step.status = StepStatus.PENDING
            step.error = ""
            self._persist_step(step)
            return True
        return False

    def _persist_step(self, step: RunStep):
        """落库步骤状态（若支持）。"""
        if not self.db_session or not self.RunLogModel:
            return
        try:
            # 尝试更新或插入
            existing = self.db_session.query(self.RunLogModel).filter_by(
                run_id=step.run_id, step_name=step.step_name
            ).first()
            if existing:
                existing.status = step.status.value
                existing.error = step.error
                existing.attempt = step.attempt
                existing.details_json = json.dumps(step.to_dict(), ensure_ascii=False)
            else:
                log = self.RunLogModel(
                    run_id=step.run_id,
                    step_name=step.step_name,
                    status=step.status.value,
                    error=step.error,
                    attempt=step.attempt,
                    details_json=json.dumps(step.to_dict(), ensure_ascii=False),
                )
                self.db_session.add(log)
            self.db_session.commit()
        except Exception:
            pass
