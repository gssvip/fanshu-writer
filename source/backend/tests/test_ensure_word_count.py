"""_ensure_word_count 多轮收敛回归测试（字数铁律）。

背景（2026-09-06 实测）：3018 字初稿一轮"精简"被压成 1189 字（思考型模型把 token
烧在推理上致正文被 max_tokens 截断 + 概述式压缩），旧版单轮失败即保留初稿 →
3018 字照常输出，铁律形同虚设。新版改为最多 3 轮收敛：

  ① 截断（finish_reason=length）→ 该轮作废、输出预算翻倍再来
  ② 未命中区间 → 上轮实际字数写进反馈再试
  ③ GLM 重写发 thinking=disabled（防思考烧 token 截断正文）
  ④ 轮次耗尽 → {初稿+各轮候选} 择 |字数-2400| 最小者
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _FakeResult:
    def __init__(self, content, finish_reason='stop', ok=True):
        self.content = content
        self.finish_reason = finish_reason
        self.ok = ok


class _FakeGW:
    """按脚本回放 chat() 结果，记录每轮 system 提示 / max_tokens / extra。"""

    calls = []
    script = []

    def __init__(self, base_url, api_key, model, timeout=180, max_retries=2):
        pass

    def chat(self, messages, temperature=0.7, max_tokens=4096, **extra):
        cls = type(self)
        cls.calls.append({'system': messages[0]['content'],
                          'max_tokens': max_tokens,
                          'extra': extra})
        idx = len(cls.calls) - 1
        if idx < len(cls.script):
            return cls.script[idx]
        return _FakeResult('', ok=False)


def _text(n):
    """n 字中文正文（_count_cn_chars 按非空白字符计，恰为 n）。"""
    return '字' * n


@pytest.fixture()
def appmod(app):
    """conftest 的 app fixture 已注入 FANSHU_DATA_DIR 等环境变量并完成 import。"""
    return sys.modules['app']


@pytest.fixture()
def fake_gw(appmod, monkeypatch):
    _FakeGW.calls = []
    _FakeGW.script = []
    monkeypatch.setattr(appmod, 'LLMGateway', _FakeGW)
    return _FakeGW


def _run(appmod, draft, model='glm-5.3', max_tokens=4096):
    return appmod._ensure_word_count(
        draft, 'sk-x', 'https://x.com/v1', model, max_tokens)


class TestInRange:
    def test_draft_in_range_no_call(self, appmod, fake_gw):
        """初稿已在 2300-2500：不触发任何重写。"""
        content, note = _run(appmod, _text(2400))
        assert content == _text(2400)
        assert note == ''
        assert fake_gw.calls == []

    def test_draft_too_short_suspect_failure(self, appmod, fake_gw):
        """初稿 <200 字疑似生成失败：不浪费 token 重写。"""
        content, note = _run(appmod, _text(100))
        assert content == _text(100)
        assert '疑似生成失败' in note
        assert fake_gw.calls == []


class TestUserCase:
    """复现实测故障：3018 → 一轮修正 1189 → 最终仍应命中区间。"""

    def test_truncated_round_then_success(self, appmod, fake_gw):
        """1189 字是 finish_reason=length 截断（思考烧 token）：翻倍预算后第二轮命中。"""
        fake_gw.script = [
            _FakeResult(_text(1189), finish_reason='length'),
            _FakeResult(_text(2400)),
        ]
        content, note = _run(appmod, _text(3018))
        assert content == _text(2400)
        assert '经2轮修正至2400字' in note
        # 截断轮翻倍输出预算：4096 → 8192
        assert fake_gw.calls[0]['max_tokens'] == 4096
        assert fake_gw.calls[1]['max_tokens'] == 8192

    def test_overcompressed_round_then_success(self, appmod, fake_gw):
        """1189 字是概述式压缩（未截断）：反馈后基于初稿重修，第二轮命中。"""
        fake_gw.script = [
            _FakeResult(_text(1189)),   # finish_reason=stop，真·压缩
            _FakeResult(_text(2350)),
        ]
        content, note = _run(appmod, _text(3018))
        assert content == _text(2350)
        assert '经2轮修正至2350字' in note
        # 1189 比 3018 更偏离 → 底稿回退初稿，反馈要求重修
        assert '回退原稿' in fake_gw.calls[1]['system']

    def test_progressive_convergence(self, appmod, fake_gw):
        """3018 → 2600（更近，作新底稿）→ 2450 命中：预算随底稿更新。"""
        fake_gw.script = [
            _FakeResult(_text(2600)),
            _FakeResult(_text(2450)),
        ]
        content, note = _run(appmod, _text(3018))
        assert content == _text(2450)
        assert '经2轮修正至2450字' in note
        # 第二轮底稿是 2600：提示词按当前 2600 字给删减预算
        assert '当前2600字' in fake_gw.calls[1]['system']


class TestFallback:
    def test_never_converges_picks_closest(self, appmod, fake_gw):
        """三轮均未命中：择 |字数-2400| 最小候选（2700 优于初稿 3018）。"""
        fake_gw.script = [
            _FakeResult(_text(2900)),
            _FakeResult(_text(2800)),
            _FakeResult(_text(2700)),
        ]
        content, note = _run(appmod, _text(3018))
        assert content == _text(2700)
        assert '已采纳最接近目标版本（2700字）' in note
        assert len(fake_gw.calls) == 3

    def test_all_rounds_fail_keep_draft(self, appmod, fake_gw):
        """三轮全未返回有效内容：保留初稿，备注含轨迹。"""
        fake_gw.script = []
        content, note = _run(appmod, _text(3018))
        assert content == _text(3018)
        assert '保留初稿' in note
        assert '未返回' in note
        assert len(fake_gw.calls) == 3


class TestThinkingDisabled:
    def test_glm_sends_thinking_disabled(self, appmod, fake_gw):
        """GLM 系模型重写时发 thinking=disabled（机械编辑不吃推理红利）。"""
        fake_gw.script = [_FakeResult(_text(2400))]
        _run(appmod, _text(3018), model='glm-5.3')
        assert fake_gw.calls[0]['extra'] == {'thinking': {'type': 'disabled'}}

    def test_non_glm_no_thinking_param(self, appmod, fake_gw):
        """非 GLM 模型不注入 thinking，防参数报错。"""
        fake_gw.script = [_FakeResult(_text(2400))]
        _run(appmod, _text(3018), model='deepseek-chat')
        assert fake_gw.calls[0]['extra'] == {}
