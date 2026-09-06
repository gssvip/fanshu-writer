"""_post_llm_adaptive 直连 LLM POST 的输出上限自愈测试。

背景（2026-09-06）：正文/审校链路 max_tokens 改为"按模型能力不限"（给足 131072，
网关按已知/自学习上限钳制）后，app.py 里未走网关的直连 requests.post 需要同样的
400/422/429 → 解析真实上限 → 钳制重发自愈，否则未知模型首次调用会直接失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _Resp:
    def __init__(self, status_code, error_msg=None):
        self.status_code = status_code
        self.text = error_msg or ''

    def json(self):
        if self.status_code == 200:
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        return {"error": {"message": self.text}}


@pytest.fixture()
def appmod(app):
    return sys.modules['app']


def test_400_clamps_and_resends(appmod, monkeypatch):
    """未知模型 400 报 max_tokens 超限 → 学到真实上限 8192、钳制重发成功。"""
    calls = []

    def post(url, **kw):
        calls.append(kw['json']['max_tokens'])
        if len(calls) == 1:
            return _Resp(400, "Invalid 'max_tokens': maximum allowed value of 8192")
        return _Resp(200)

    monkeypatch.setattr(appmod.requests, 'post', post)
    resp = appmod._post_llm_adaptive('sk-x', 'https://x.com/v1', 'unknown-m',
                                     {'model': 'unknown-m', 'messages': [],
                                      'max_tokens': 131072})
    assert resp.status_code == 200
    assert calls == [131072, 8192]


def test_429_glm_style_clamps(appmod, monkeypatch):
    """GLM 式 429（max_output_tokens 上限文案）同样触发钳制重发。"""
    calls = []

    def post(url, **kw):
        calls.append(kw['json']['max_tokens'])
        if len(calls) == 1:
            return _Resp(429, "Field 'max_output_tokens' must be at most 128000")
        return _Resp(200)

    monkeypatch.setattr(appmod.requests, 'post', post)
    resp = appmod._post_llm_adaptive('sk-x', 'https://x.com/v1', 'unknown-glm',
                                     {'model': 'unknown-glm', 'messages': [],
                                      'max_tokens': 131072})
    assert resp.status_code == 200
    assert calls == [131072, 128000]


def test_non_max_tokens_error_no_retry(appmod, monkeypatch):
    """与 max_tokens 无关的 400（如鉴权失败）不重发，原样返回。"""
    calls = []

    def post(url, **kw):
        calls.append(kw['json'])
        return _Resp(400, "Invalid API key")

    monkeypatch.setattr(appmod.requests, 'post', post)
    resp = appmod._post_llm_adaptive('sk-bad', 'https://x.com/v1', 'unknown-m',
                                     {'model': 'unknown-m', 'messages': [],
                                      'max_tokens': 131072})
    assert resp.status_code == 400
    assert len(calls) == 1


def test_200_pass_through(appmod, monkeypatch):
    """成功响应直接透传，不触发重发。"""
    calls = []

    def post(url, **kw):
        calls.append(kw['json'])
        return _Resp(200)

    monkeypatch.setattr(appmod.requests, 'post', post)
    resp = appmod._post_llm_adaptive('sk-x', 'https://x.com/v1', 'glm-5.3',
                                     {'model': 'glm-5.3', 'messages': [],
                                      'max_tokens': 128000})
    assert resp.status_code == 200
    assert len(calls) == 1


def test_learned_limit_cached_for_next_call(appmod, monkeypatch):
    """首调用学到的上限写入 llm_gateway 缓存：后续 ctx 构建即钳制，不再报错。"""
    import llm_gateway as lg
    calls = []

    def post(url, **kw):
        calls.append(kw['json']['max_tokens'])
        if len(calls) == 1:
            return _Resp(400, "max_tokens 不能超过 4096")
        return _Resp(200)

    monkeypatch.setattr(appmod.requests, 'post', post)
    lg._LEARNED_OUTPUT_LIMITS.pop(('https://x.com/v1', 'cache-m'), None)
    appmod._post_llm_adaptive('sk-x', 'https://x.com/v1', 'cache-m',
                              {'model': 'cache-m', 'messages': [], 'max_tokens': 131072})
    assert calls == [131072, 4096]
    assert lg.get_output_limit('https://x.com/v1', 'cache-m') == 4096
    lg._LEARNED_OUTPUT_LIMITS.pop(('https://x.com/v1', 'cache-m'), None)
