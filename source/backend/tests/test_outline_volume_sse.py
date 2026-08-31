"""节点设计 SSE 回归（方案C）：解决 502 → text/event-stream 流式。

验证 3 点：
  1. __init__ 中该路由返回 text/event-stream（非同步 json）。
  2. 请求一个不存在的 book_id：后台线程架构经过 _run_blocking_with_heartbeat，
     最终流式产出 start / error / [DONE] 事件，错误以 data:{"error":...} 帧返回。
  3. _ai_outline_volume_impl 在全部分支都以 (dict, status) 返回、不直接 return jsonify，
     保证 SSE 封装能拿到可序列化数据而非 Response 对象。
"""
import io
import sys

import pytest


@pytest.mark.usefixtures("app")
class TestOutlineVolumeSse:
    def test_impl_returns_tuple_not_jsonify(self, app):
        from app import _ai_outline_volume_impl
        with app.app_context():
            payload, status = _ai_outline_volume_impl('no-such-book', {})
        assert status == 404
        assert isinstance(payload, dict)
        assert 'error' in payload

    def test_sse_route_streams_events(self, app):
        with app.test_client() as c:
            resp = c.post('/api/books/definitely-missing-book/ai-outline-volume', json={
                'volume_index': 1, 'volume_title': '第1卷', 'node_only': False,
            })
            assert resp.headers.get('Content-Type', '').startswith('text/event-stream')
            body = resp.data.decode('utf-8', 'replace')
        # 首帧 start
        assert '"type":"start"' in body
        # 错误事件（book 不存在）
        assert '"error"' in body
        # 尾帧 DONE
        assert '[DONE]' in body

    def test_impl_no_return_jsonify_left(self, app):
        """AST 断言：_ai_outline_volume_impl 函数体内不得残留 return jsonify。"""
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / 'app.py'
        tree = ast.parse(src.read_text(encoding='utf-8'))
        impl = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_ai_outline_volume_impl':
                impl = node
                break
        assert impl is not None
        offenders = [r.lineno for r in ast.walk(impl)
                     if isinstance(r, ast.Return) and isinstance(r.value, ast.Call)
                     and getattr(r.value.func, 'id', '') == 'jsonify']
        assert not offenders, f'_ai_outline_volume_impl 残留 return jsonify 于行号 {offenders}，SSE 封装将拿到 Response 对象而非可序列化数据'