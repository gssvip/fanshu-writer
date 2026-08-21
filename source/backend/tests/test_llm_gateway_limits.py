"""LLM Gateway 模型输出上限自动适配测试。

覆盖：
  - _parse_max_tokens_limit：各家 400 报错文案解析（OpenAI/Anthropic/旧式/中文）
  - _known_output_limit：已知模型表精确/变体子串匹配
  - get_output_limit：报错自学习缓存优先于已知表
  - chat / chat_stream：400 → 解析真实上限 → 钳制重发成功（自学习生效）
  - 报错无数字 → 退 8192 兜底重试（不写缓存）
  - 与 max_tokens 无关的 400 不触发适配（正常报错）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import llm_gateway as lg  # noqa: E402
from llm_gateway import LLMGateway, LLMError  # noqa: E402

_SSE_OK = (
    'data: {"choices":[{"delta":{"content":"正文"}}]}\n\n'
    'data: [DONE]\n\n'
).encode('utf-8')


class _Resp400:
    status_code = 400

    def __init__(self, msg):
        self.text = msg
        self._msg = msg

    def json(self):
        return {"error": {"message": self._msg}}


class _RespStream200:
    status_code = 200
    text = ""

    def __init__(self, chunks):
        # 允许传单个 bytes 或列表（迭代 bytes 会产出 int，这里统一包成列表）
        self._chunks = list(chunks) if isinstance(chunks, (list, tuple)) else [chunks]

    def json(self):
        return {}

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


class _Resp200:
    """非流式 200 响应。"""
    status_code = 200
    text = ""

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}, "finish_reason": "stop"}]}


class TestParseMaxTokensLimit:
    def test_openai_style(self):
        msg = "Invalid 'max_tokens': integer exceeds the maximum allowed value of 16384."
        assert lg._parse_max_tokens_limit(msg, 27000) == 16384

    def test_anthropic_style(self):
        msg = "max_tokens: 27000 > 8191, which is the maximum allowed number of output tokens"
        assert lg._parse_max_tokens_limit(msg, 27000) == 8191

    def test_old_style(self):
        msg = "max_tokens is too large: 27000. This model supports at most 4096."
        assert lg._parse_max_tokens_limit(msg, 27000) == 4096

    def test_chinese(self):
        assert lg._parse_max_tokens_limit('max_tokens 参数最大值为 8192', 27000) == 8192
        assert lg._parse_max_tokens_limit('max_tokens 不能超过 8192', 27000) == 8192

    def test_limit_above_request_returns_zero(self):
        """解析出的上限 ≥ 请求值：无需降档，返回 0（不触发适配）。"""
        assert lg._parse_max_tokens_limit('max_tokens 参数最大值为 65536', 27000) == 0

    def test_limit_at_least_request_returns_zero(self):
        msg = "max_tokens must be at most 32768"
        assert lg._parse_max_tokens_limit(msg, 27000) == 0  # 32768 ≥ 请求值，无需降档

    def test_no_max_tokens_mention(self):
        """与 max_tokens 无关的报错不解析（返回 0，不触发适配）。"""
        assert lg._parse_max_tokens_limit('Invalid model name: foo', 27000) == 0

    def test_mention_without_number(self):
        assert lg._parse_max_tokens_limit('max_tokens is invalid', 27000) == 0

    def test_empty_message(self):
        assert lg._parse_max_tokens_limit('', 27000) == 0


class TestKnownOutputLimit:
    def test_exact_match(self):
        assert lg._known_output_limit('deepseek-chat') == 8192
        assert lg._known_output_limit('gpt-4o') == 16384
        assert lg._known_output_limit('deepseek-reasoner') == 65536

    def test_variant_substring_match(self):
        """变体名（带日期/版本后缀）按子串匹配。"""
        assert lg._known_output_limit('gpt-4o-2024-08-06') == 16384
        assert lg._known_output_limit('deepseek-v3-0324') == 8192

    def test_unknown_returns_zero(self):
        assert lg._known_output_limit('unknown-model-xyz') == 0
        assert lg._known_output_limit('') == 0


class TestGetOutputLimit:
    def test_known_model(self):
        assert lg.get_output_limit('https://x.com/v1', 'deepseek-chat') == 8192

    def test_learned_cache_overrides_known(self):
        """报错自学习缓存优先于已知模型表。"""
        key = ('https://x.com/v1', 'deepseek-chat')
        lg._LEARNED_OUTPUT_LIMITS[key] = 4096
        try:
            assert lg.get_output_limit('https://x.com/v1', 'deepseek-chat') == 4096
        finally:
            lg._LEARNED_OUTPUT_LIMITS.pop(key, None)


class TestChatStreamAdaptation:
    def test_400_then_success_with_parsed_limit(self, monkeypatch):
        """未知模型 27000 → 400（报错含 8192）→ 钳制重发成功，且学习缓存生效。"""
        calls = []

        def post(url, **kw):
            calls.append(kw['json']['max_tokens'])
            if len(calls) == 1:
                return _Resp400("Invalid max_tokens: integer exceeds the maximum "
                                "allowed value of 8192.")
            return _RespStream200(_SSE_OK)

        monkeypatch.setattr('llm_gateway.requests.post', post)
        gw = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-model-xyz')
        out = ''.join(gw.chat_stream([{'role': 'user', 'content': 'hi'}], max_tokens=27000))
        assert out == '正文'
        assert calls == [27000, 8192]
        # 自学习缓存已写入：同 (base_url, model) 后续直接钳制
        assert lg._LEARNED_OUTPUT_LIMITS[('https://x.com/v1', 'unknown-model-xyz')] == 8192

    def test_learned_cache_applies_to_new_gateway(self, monkeypatch):
        """学习缓存（进程级）对新建网关生效：直接钳制，不再付 400 往返。"""
        key = ('https://x.com/v1', 'unknown-model-xyz')
        lg._LEARNED_OUTPUT_LIMITS.pop(key, None)
        calls2 = []

        def post2(url, **kw):
            calls2.append(kw['json']['max_tokens'])
            return _RespStream200(_SSE_OK)

        monkeypatch.setattr('llm_gateway.requests.post', post2)
        lg._LEARNED_OUTPUT_LIMITS[key] = 8192
        try:
            gw2 = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-model-xyz')
            out2 = ''.join(gw2.chat_stream([{'role': 'user', 'content': 'hi'}], max_tokens=27000))
            assert out2 == '正文'
            assert calls2 == [8192]
        finally:
            lg._LEARNED_OUTPUT_LIMITS.pop(key, None)

    def test_known_model_preclamped_no_400(self, monkeypatch):
        """已知模型（deepseek-chat 8192）：payload 预钳制，单次成功。"""
        calls3 = []

        def post3(url, **kw):
            calls3.append(kw['json']['max_tokens'])
            return _RespStream200(_SSE_OK)

        monkeypatch.setattr('llm_gateway.requests.post', post3)
        gw3 = LLMGateway('https://api.deepseek.com/v1', 'sk-x', 'deepseek-chat')
        out3 = ''.join(gw3.chat_stream([{'role': 'user', 'content': 'hi'}], max_tokens=27000))
        assert out3 == '正文'
        assert calls3 == [8192]

    def test_400_no_number_falls_back_8192(self, monkeypatch):
        """报错提 max_tokens 但没给数字：本次退到 8192 重试（不写缓存）。"""
        calls = []

        def post(url, **kw):
            calls.append(kw['json']['max_tokens'])
            if len(calls) == 1:
                return _Resp400('max_tokens value is not supported for this model')
            return _RespStream200(_SSE_OK)

        monkeypatch.setattr('llm_gateway.requests.post', post)
        gw = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-model-abc')
        out = ''.join(gw.chat_stream([{'role': 'user', 'content': 'hi'}], max_tokens=27000))
        assert out == '正文'
        assert calls == [27000, 8192]
        # 没解析到具体上限 → 不写学习缓存
        assert ('https://x.com/v1', 'unknown-model-abc') not in lg._LEARNED_OUTPUT_LIMITS

    def test_unrelated_400_raises(self, monkeypatch):
        """与 max_tokens 无关的 400 不触发适配（正常报错）。"""
        def post(url, **kw):
            return _Resp400("Model not found: unknown-model-xyz")

        monkeypatch.setattr('llm_gateway.requests.post', post)
        gw = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-model-xyz')
        with pytest.raises(LLMError):
            list(gw.chat_stream([{'role': 'user', 'content': 'hi'}], max_tokens=2000))


class TestChatAdaptation:
    def test_chat_400_then_success(self, monkeypatch):
        """非流式 chat()：400 → 解析上限 → 钳制重发成功。"""
        calls = []

        def post(url, **kw):
            calls.append(kw['json']['max_tokens'])
            if len(calls) == 1:
                return _Resp400("Invalid 'max_tokens': integer exceeds the maximum "
                                "allowed value of 8192.")
            return _Resp200('正文')

        monkeypatch.setattr('llm_gateway.requests.post', post)
        gw = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-chat-model')
        result = gw.chat([{'role': 'user', 'content': 'hi'}], max_tokens=27000)
        assert result.ok
        assert result.content == '正文'
        assert calls == [27000, 8192]

    def test_chat_unrelated_400_fails_normally(self, monkeypatch):
        """非流式 chat()：与 max_tokens 无关的 400 正常失败（错误信息透传）。"""
        def post(url, **kw):
            return _Resp400("Model not found: unknown-chat-model")

        monkeypatch.setattr('llm_gateway.requests.post', post)
        gw = LLMGateway('https://x.com/v1', 'sk-x', 'unknown-chat-model')
        result = gw.chat([{'role': 'user', 'content': 'hi'}], max_tokens=2000)
        assert not result.ok
        assert 'Model not found' in result.error
