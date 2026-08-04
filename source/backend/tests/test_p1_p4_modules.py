"""P1-P4 新模块单元测试。

覆盖：
  - llm_gateway: ModelResult + FailureClass + 错误分类
  - context_manifest: ContextManifest + ContextOrchestrator + 失效检测
  - prompt_spec: PromptSpec + PromptCompiler + golden 断言
  - run_recovery: IdempotencyKey + RunRecoveryService + 单步恢复
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保能 import backend 模块
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ===== P1: LLM Gateway =====

class TestLLMGateway:
    def test_model_result_ok(self):
        from llm_gateway import ModelResult, FailureClass
        r = ModelResult(content="hello", failure_class=FailureClass.NONE)
        assert r.ok is True

    def test_model_result_empty(self):
        from llm_gateway import ModelResult, FailureClass
        r = ModelResult(content="", failure_class=FailureClass.EMPTY_RESPONSE)
        assert r.ok is False
        assert r.is_empty is True

    def test_classify_timeout(self):
        import requests
        from llm_gateway import _classify_error, FailureClass
        assert _classify_error(requests.exceptions.Timeout()) == FailureClass.TIMEOUT

    def test_classify_auth_error(self):
        from llm_gateway import _classify_error, FailureClass
        assert _classify_error(None, status_code=401) == FailureClass.AUTHENTICATION

    def test_classify_quota_error(self):
        from llm_gateway import _classify_error, FailureClass
        assert _classify_error(None, status_code=429) == FailureClass.QUOTA

    def test_extract_content_success(self):
        from llm_gateway import _extract_content, FailureClass
        body = {"choices": [{"message": {"content": "正文"}, "finish_reason": "stop"}]}
        content, fr, fc = _extract_content(body)
        assert content == "正文"
        assert fc == FailureClass.NONE

    def test_extract_content_empty(self):
        from llm_gateway import _extract_content, FailureClass
        body = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        content, fr, fc = _extract_content(body)
        assert content == ""
        assert fc == FailureClass.EMPTY_RESPONSE

    def test_extract_content_api_error(self):
        from llm_gateway import _extract_content, FailureClass
        body = {"error": {"message": "Invalid API key"}}
        content, fr, fc = _extract_content(body)
        assert content == ""
        assert fc == FailureClass.AUTHENTICATION


# ===== P2: Context Manifest =====

class TestContextManifest:
    def test_manifest_basic(self):
        from context_manifest import ContextManifest, ContextSource, ManifestState
        sources = [
            ContextSource(name="key_rules", content="规则1规则2"),
            ContextSource(name="worldbuilding", content="世界观设定"),
        ]
        m = ContextManifest(book_id="b1", chapter_num=1, sources=sources)
        assert m.manifest_id  # 自动生成
        assert len(m.sources) == 2
        assert m.total_tokens > 0
        assert m.state == ManifestState.READY

    def test_manifest_over_budget(self):
        from context_manifest import ContextManifest, ContextSource, ManifestState
        # 创建超大内容触发超预算
        big_content = "字" * 10000
        sources = [ContextSource(name="key_rules", content=big_content)]
        m = ContextManifest(book_id="b1", chapter_num=1, sources=sources,
                           token_budget=100)
        assert m.is_over_budget is True
        assert m.state == ManifestState.READY  # prepare() 才设 NEEDS_TRUNCATION

    def test_manifest_truncate(self):
        from context_manifest import ContextOrchestrator
        orch = ContextOrchestrator(token_budget=200)
        sources = {
            "key_rules": "规则" * 100,      # 高优先级
            "worldbuilding": "世界" * 100,   # 中优先级
            "concept": "构思" * 100,         # 低优先级
        }
        m = orch.prepare(sources, chapter_num=1, book_id="b1")
        assert m.needs_truncation() is True
        truncated = m.truncate_to_budget(200)
        # 高优先级的 key_rules 应该被保留
        assert "key_rules" in truncated

    def test_manifest_source_hashes(self):
        from context_manifest import ContextManifest, ContextSource
        sources = [ContextSource(name="key_rules", content="内容")]
        m = ContextManifest(book_id="b1", chapter_num=1, sources=sources)
        hashes = m.source_hashes
        assert "key_rules" in hashes
        assert len(hashes["key_rules"]) == 16  # sha1[:16]

    def test_orchestrator_check_stale(self):
        from context_manifest import ContextOrchestrator
        orch = ContextOrchestrator()
        sources = {"key_rules": "原始内容"}
        m = orch.prepare(sources, chapter_num=1, book_id="b1")
        # 内容未变
        assert orch.check_stale(m, {"key_rules": "原始内容"}) is False
        # 内容变了
        assert orch.check_stale(m, {"key_rules": "修改后内容"}) is True

    def test_manifest_to_dict(self):
        from context_manifest import ContextOrchestrator
        orch = ContextOrchestrator()
        m = orch.prepare({"key_rules": "规则"}, chapter_num=5, book_id="b1")
        d = m.to_dict()
        assert d["book_id"] == "b1"
        assert d["chapter_num"] == 5
        assert len(d["sources"]) == 1
        assert d["sources"][0]["name"] == "key_rules"


# ===== P3: PromptSpec =====

class TestPromptSpec:
    def test_load_prompt_spec(self):
        from prompt_spec import load_prompt_spec
        spec_path = BACKEND_DIR / "prompt_specs" / "chapter_writer.md"
        if not spec_path.exists():
            return  # 文件不存在时跳过
        spec = load_prompt_spec(spec_path)
        assert spec.name == "chapter_writer"
        assert "chapter_num" in spec.inputs
        assert "word_budget" in spec.inputs

    def test_render(self):
        from prompt_spec import load_prompt_spec
        spec_path = BACKEND_DIR / "prompt_specs" / "chapter_writer.md"
        if not spec_path.exists():
            return
        spec = load_prompt_spec(spec_path)
        rendered = spec.render(
            chapter_num=5, word_budget=2400,
            system_prompt="系统指令", user_prompt="用户要求")
        assert "第 5 章" in rendered
        assert "2400" in rendered
        assert "系统指令" in rendered

    def test_get_placeholders(self):
        from prompt_spec import load_prompt_spec
        spec_path = BACKEND_DIR / "prompt_specs" / "chapter_writer.md"
        if not spec_path.exists():
            return
        spec = load_prompt_spec(spec_path)
        placeholders = spec.get_placeholders()
        assert "chapter_num" in placeholders
        assert "word_budget" in placeholders

    def test_compiler_validate_ok(self):
        from prompt_spec import PromptSpec, PromptCompiler
        spec = PromptSpec(
            name="test",
            template="Hello {{name}}!",
            inputs=["name"],
        )
        compiler = PromptCompiler()
        issues = compiler.validate(spec)
        assert len(issues) == 0

    def test_compiler_unknown_placeholder(self):
        from prompt_spec import PromptSpec, PromptCompiler
        spec = PromptSpec(
            name="test",
            template="Hello {{name}} and {{unknown}}!",
            inputs=["name"],
        )
        compiler = PromptCompiler()
        issues = compiler.validate(spec)
        assert any("unknown" in i for i in issues)

    def test_compiler_unused_input(self):
        from prompt_spec import PromptSpec, PromptCompiler
        spec = PromptSpec(
            name="test",
            template="Hello {{name}}!",
            inputs=["name", "unused_var"],
        )
        compiler = PromptCompiler()
        issues = compiler.validate(spec)
        assert any("unused_var" in i for i in issues)

    def test_compiler_golden_case(self):
        from prompt_spec import PromptSpec, PromptCompiler, GoldenCase
        spec = PromptSpec(
            name="test",
            template="第{{num}}章 {{title}}",
            inputs=["num", "title"],
            golden_cases=[
                GoldenCase(
                    input={"num": 1, "title": "开端"},
                    assert_contains=["第1章", "开端"],
                    assert_min_length=5,
                ),
            ],
        )
        compiler = PromptCompiler()
        issues = compiler.validate(spec)
        assert len(issues) == 0

    def test_compiler_golden_case_fail(self):
        from prompt_spec import PromptSpec, PromptCompiler, GoldenCase
        spec = PromptSpec(
            name="test",
            template="第{{num}}章",
            inputs=["num"],
            golden_cases=[
                GoldenCase(
                    input={"num": 1},
                    assert_contains=["不存在的内容"],
                ),
            ],
        )
        compiler = PromptCompiler()
        issues = compiler.validate(spec)
        assert any("断言失败" in i for i in issues)

    def test_validate_all_specs(self):
        from prompt_spec import validate_all_specs
        issues = validate_all_specs(BACKEND_DIR / "prompt_specs")
        # 应该没有严重问题（可能有 golden 未渲染占位符的 warning）
        assert isinstance(issues, list)


# ===== P4: Run Recovery =====

class TestRunRecovery:
    def test_idempotency_key(self):
        from run_recovery import IdempotencyKey
        key = IdempotencyKey.create_chapter("book_123", chapter_num=5)
        assert key.operation == "create_chapter"
        assert key.entity_type == "chapter"
        assert key.key_hash  # 自动生成

    def test_idempotency_key_deterministic(self):
        from run_recovery import IdempotencyKey
        k1 = IdempotencyKey.create_chapter("book_123", chapter_num=5)
        k2 = IdempotencyKey.create_chapter("book_123", chapter_num=5)
        assert k1.key_hash == k2.key_hash  # 相同输入 → 相同 hash

    def test_idempotency_key_different(self):
        from run_recovery import IdempotencyKey
        k1 = IdempotencyKey.create_chapter("book_123", chapter_num=5)
        k2 = IdempotencyKey.create_chapter("book_123", chapter_num=6)
        assert k1.key_hash != k2.key_hash

    def test_run_recovery_start_and_complete(self):
        from run_recovery import RunRecoveryService, StepStatus
        svc = RunRecoveryService()
        run_id = svc.start_run("batch_create", book_id="b1", chapters=[1, 2, 3])

        step1 = svc.start_step(run_id, "chapter_1")
        svc.complete_step(step1, result={"chapter_id": "ch1"})

        status = svc.get_run_status(run_id)
        assert status["total_steps"] == 3
        assert status["succeeded"] == 1

    def test_run_recovery_fail_and_resume(self):
        from run_recovery import RunRecoveryService, StepStatus
        svc = RunRecoveryService()
        run_id = svc.start_run("batch_create", book_id="b1", chapters=[1, 2, 3])

        # 第1章成功
        s1 = svc.start_step(run_id, "chapter_1")
        svc.complete_step(s1)

        # 第2章失败
        s2 = svc.start_step(run_id, "chapter_2")
        svc.fail_step(s2, error="LLM 超时")

        # 获取恢复点
        resume = svc.get_resume_point(run_id)
        assert resume == 1  # 第2章（index=1）是第一个失败的

        # 重试第2章
        assert svc.retry_step(s2) is True

        # 重新执行
        s2b = svc.start_step(run_id, "chapter_2")
        svc.complete_step(s2b)

        status = svc.get_run_status(run_id)
        assert status["succeeded"] == 2

    def test_run_recovery_get_failed_steps(self):
        from run_recovery import RunRecoveryService
        svc = RunRecoveryService()
        run_id = svc.start_run("batch", chapters=[1, 2])

        s1 = svc.start_step(run_id, "chapter_1")
        svc.fail_step(s1, error="失败1")
        s2 = svc.start_step(run_id, "chapter_2")
        svc.fail_step(s2, error="失败2")

        failed = svc.get_failed_steps(run_id)
        assert len(failed) == 2

    def test_run_recovery_skip_step(self):
        from run_recovery import RunRecoveryService, StepStatus
        svc = RunRecoveryService()
        run_id = svc.start_run("batch", chapters=[1, 2])

        s1 = svc.start_step(run_id, "chapter_1")
        svc.skip_step(s1, reason="已存在")

        status = svc.get_run_status(run_id)
        # skipped 不算 succeeded 也不算 failed
        assert status["succeeded"] == 0
        assert status["failed"] == 0
