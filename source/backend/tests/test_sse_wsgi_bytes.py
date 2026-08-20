"""SSE WSGI 层回归：杜绝 direct_passthrough + str 帧在 Render 上崩溃复发。

背景（2026-08-20 线上 P0 事故）：7 处 SSE Response 带 direct_passthrough=True，
生成器 yield 的 str 帧跳过 Flask 编码层直接交给 WSGI，Render 的 Python 3.14 +
新版 werkzeug 对每个 chunk 断言 isinstance(data, bytes) → SSE 第一帧即
AssertionError: applications must write bytes → 前端收 0 帧 →
"AI 未返回任何内容"。智驾流式全线瘫痪多轮排查。

本测试在 WSGI 协议层直接调用 app，断言所有流式帧都是 bytes——
任何新端点再写 direct_passthrough=True 或 yield str 且绕过编码层，CI 立即拦截。
"""
import io
import sys

from flask import Flask, Response, stream_with_context


def _collect_wsgi_body_chunks(app, path):
    """以最小 WSGI environ 直接调用 app，返回响应体 chunk 列表（原样，不编码）。"""
    environ = {
        'REQUEST_METHOD': 'GET', 'PATH_INFO': path,
        'SERVER_NAME': 't', 'SERVER_PORT': '80', 'wsgi.url_scheme': 'http',
        'wsgi.input': io.BytesIO(), 'wsgi.errors': sys.stderr,
        'wsgi.version': (1, 0), 'wsgi.multithread': True,
        'wsgi.multiprocess': True, 'wsgi.run_once': False,
    }
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured['status'] = status

    body = app(environ, start_response)
    return captured['status'], list(body)


def _make_app(**resp_kwargs):
    app = Flask(__name__)

    @app.route('/sse')
    def sse():
        def gen():
            yield 'data: {"type":"delta","content":"正文"}\n\n'
            yield 'data: [DONE]\n\n'
        return Response(stream_with_context(gen()),
                        mimetype='text/event-stream', **resp_kwargs)

    return app


class TestSSEWsgiBytesContract:
    """契约：SSE 生成器 yield str，但经 Flask 编码后到达 WSGI 层必须是 bytes。"""

    def test_standard_sse_frames_are_bytes(self):
        """标准写法（无 direct_passthrough）：Flask 自动编码 str→bytes，werkzeug 断言通过。"""
        app = _make_app()
        status, chunks = _collect_wsgi_body_chunks(app, '/sse')
        assert status.startswith('200')
        assert chunks, 'SSE 流必须产出至少 1 帧'
        assert all(isinstance(c, bytes) for c in chunks), \
            'WSGI 层收到非 bytes 帧 → Render werkzeug 会抛 AssertionError'

    def test_direct_passthrough_would_break(self):
        """反例回归：direct_passthrough=True 会把 str 原样交给 WSGI（线上事故根因）。

        该写法在本地老版本 werkzeug 可能侥幸通过，但在 Render（Python 3.14）必崩。
        断言其帧类型为 str，证明此参数是事故根源；新代码禁止再写。
        """
        app = _make_app(direct_passthrough=True)
        _, chunks = _collect_wsgi_body_chunks(app, '/sse')
        assert all(isinstance(c, str) for c in chunks), \
            'direct_passthrough 预期会绕过编码层产出 str（这正是线上崩溃的原因）'

    def test_project_sse_endpoints_have_no_direct_passthrough(self):
        """扫描项目源码：任何 SSE Response 严禁再出现 direct_passthrough=True。"""
        from pathlib import Path
        backend = Path(__file__).resolve().parents[1]
        offenders = []
        for py in list(backend.glob('*.py')) + list((backend / 'blueprints').glob('*.py')):
            try:
                src = py.read_text(encoding='utf-8')
            except Exception:
                continue
            if 'text/event-stream' in src and 'direct_passthrough=True' in src:
                offenders.append(py.name)
        assert not offenders, f'SSE 端点禁用 direct_passthrough=True（线上事故根因）：{offenders}'
