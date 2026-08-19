"""LLMGateway.chat_stream SSE 解析回归测试。

覆盖 OpenAI 兼容 SSE 的常见变体，防"AI 未返回任何内容"回归：
  - 标准 \n\n 分隔
  - \r\n\r\n 分隔（智谱 GLM / 部分中转，历史根因）
  - 分隔符被随机块切断
  - data: 无空格
  - 多行 data 拼接
  - 服务端忽略 stream 返回非流式 message
  - 思考 + 正文
  - 空流 → 非流式兜底
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_gateway import LLMGateway  # noqa: E402


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, chunks):
        self._chunks = chunks

    def json(self):
        return {}

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


def _mk_nonstream(content):
    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}

    return R()


def _run(monkeypatch, stream_chunks, nonstream_content=None):
    """模拟 requests.post：第一次返回流，再次调用返回非流式兜底。"""
    calls = {"n": 0}

    def post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(stream_chunks)
        return _mk_nonstream(nonstream_content)

    monkeypatch.setattr("llm_gateway.requests.post", post)
    gw = LLMGateway("https://x.com/v1", "sk-x", "test-model")
    out = []
    for c in gw.chat_stream([{"role": "user", "content": "hi"}], max_tokens=2000):
        out.append(c)
    return "".join(out), calls["n"]


def _b(s):
    return s.encode()


def _chunkify(data: bytes, size: int = 7):
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_standard_stream(monkeypatch):
    c, n = _run(monkeypatch, [_b('data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'),
                               _b('data: [DONE]\n\n')])
    assert c == "你好" and n == 1


def test_crlf_crlf_delimiter(monkeypatch):
    # 历史根因：智谱 GLM 用 \r\n\r\n 分隔，旧实现只认 \n\n
    c, n = _run(monkeypatch, [_b('data: {"choices":[{"delta":{"content":"A"}}]}\r\n\r\n'),
                               _b('data: {"choices":[{"delta":{"content":"B"}}]}\r\n\r\n'),
                               _b('data: [DONE]\r\n\r\n')])
    assert c == "AB" and n == 1


def test_crlf_split_across_chunks(monkeypatch):
    raw = _b('data: {"choices":[{"delta":{"content":"AB"}}]}\r\n\r\n'
             'data: {"choices":[{"delta":{"content":"CD"}}]}\r\n\r\n'
             'data: [DONE]\r\n\r\n')
    c, n = _run(monkeypatch, _chunkify(raw, size=7))
    assert c == "ABCD" and n == 1


def test_data_no_space(monkeypatch):
    c, n = _run(monkeypatch, [_b('data:{"choices":[{"delta":{"content":"无空格"}}]}\n\n'),
                               _b('data:[DONE]\n\n')])
    assert c == "无空格" and n == 1


def test_multiline_data(monkeypatch):
    c, n = _run(monkeypatch, [_b('data: {"choices":[{"delta":{"content":"第一行'),
                               _b('第二行"}}]}\n\n'), _b('data: [DONE]\n\n')])
    assert c == "第一行第二行" and n == 1


def test_nonstream_message_fallback_in_stream(monkeypatch):
    c, n = _run(monkeypatch, [_b('{"choices":[{"message":{"content":"非流式正文"},"finish_reason":"stop"}]}\n\n')])
    assert c == "非流式正文" and n == 1


def test_reasoning_then_content(monkeypatch):
    c, n = _run(monkeypatch, [_b('data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n'),
                               _b('data: {"choices":[{"delta":{"content":"正文"}}]}\n\n'),
                               _b('data: [DONE]\n\n')])
    assert c == "正文" and n == 1


def test_empty_stream_nonstream_fallback(monkeypatch):
    c, n = _run(monkeypatch, [_b('data: [DONE]\n\n')], nonstream_content="兜底正文")
    assert c == "兜底正文" and n == 2