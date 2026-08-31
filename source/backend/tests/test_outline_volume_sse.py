"""节点设计 SSE 回归（方案C）：解决 502 → text/event-stream 流式。

验证范围：
  1. 登录/鉴权铁律：@login_required 拒绝未登录（401），非所有者无权改别人的书（403）。
  2. 协议头：text/event-stream + Cache-Control:no-cache + X-Accel-Buffering:no
     + Connection:keep-alive → Render Nginx 不缓冲 SSE。
  3. 事件流：请求不存在 book_id / 缺 API Key 时，后台线程架构经
     _run_blocking_with_heartbeat 最终产出 start / error / [DONE]。
  4. _ai_outline_volume_impl 在全部分支都 return (dict, status)、不直接
     return jsonify，保证 SSE 封装拿到可序列化数据。
  5. 心跳帧为合法 data:{...}\\n\\n 格式（之前裸字符串 bug 的回归兜底）。
"""
import ast

import pytest


@pytest.fixture()
def _seed_auth(app, client):
    """创建测试用户+书籍+有效 token，返回 dict{token/own_book_id/other_book_id/uid}。"""
    from datetime import datetime, timedelta, timezone
    from app import db, User, AuthToken, Book, generate_token
    with app.app_context():
        u1 = User(username='sse_tester', password_hash='x')
        u2 = User(username='sse_other',  password_hash='y')
        db.session.add_all([u1, u2]); db.session.flush()
        t1 = generate_token()
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        db.session.add(AuthToken(user_id=u1.id, token=t1, expires_at=expires))
        b_own   = Book(user_id=u1.id, title='我的书', genre='xuanhuan')
        b_other = Book(user_id=u2.id, title='他人的书', genre='xuanhuan')
        db.session.add_all([b_own, b_other]); db.session.commit()
        return {'token': t1, 'own_book_id': b_own.id, 'other_book_id': b_other.id, 'uid': u1.id}


@pytest.mark.usefixtures("app")
class TestOutlineVolumeSse:
    # ---------- 基础 impl 不变 ----------
    def test_impl_returns_tuple_not_jsonify(self, app):
        from app import _ai_outline_volume_impl
        with app.app_context():
            payload, status = _ai_outline_volume_impl('no-such-book', {})
        assert status == 404
        assert isinstance(payload, dict) and 'error' in payload

    def test_impl_no_return_jsonify_left(self, app):
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / 'app.py'
        tree = ast.parse(src.read_text(encoding='utf-8'))
        impl = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_ai_outline_volume_impl'), None)
        assert impl is not None
        offenders = [r.lineno for r in ast.walk(impl)
                     if isinstance(r, ast.Return) and isinstance(r.value, ast.Call)
                     and getattr(r.value.func, 'id', '') == 'jsonify']
        assert not offenders, f'残留 return jsonify 于 {offenders}'

    def test_impl_ownership_check(self, app):
        from datetime import datetime, timedelta, timezone
        from app import _ai_outline_volume_impl, db, User, AuthToken, Book, generate_token
        with app.app_context():
            u1 = User(username='impl_own1', password_hash='x')
            u2 = User(username='impl_own2', password_hash='y')
            db.session.add_all([u1, u2]); db.session.flush()
            # 生成 token 保证和真实路径一致（虽不直接用于 impl，但用于防止缺字段报错）
            expires = datetime.now(timezone.utc) + timedelta(days=30)
            db.session.add_all([
                AuthToken(user_id=u1.id, token=generate_token(), expires_at=expires),
                AuthToken(user_id=u2.id, token=generate_token(), expires_at=expires),
            ])
            b = Book(user_id=u1.id, title='b', genre='xuanhuan')
            db.session.add(b); db.session.commit()
            bid = b.id
            # 所有者本人 → 404/500 流程但非 403
            _, s = _ai_outline_volume_impl(bid, {}, user_id=u1.id)
            assert s != 403
            # 他人 → 403 拒绝
            payload, s = _ai_outline_volume_impl(bid, {}, user_id=u2.id)
            assert s == 403
            assert '无权' in payload.get('error', '')

    # ---------- 登录/权限 ----------
    def test_route_requires_login(self, app, client):
        """未携带 Authorization：@login_required 返回 401 + JSON，非 SSE。"""
        resp = client.post('/api/books/any-id/ai-outline-volume', json={'volume_index': 1})
        assert resp.status_code == 401
        assert not resp.headers.get('Content-Type', '').startswith('text/event-stream')
        assert resp.json and 'error' in resp.json

    def test_route_forbids_other_users_book(self, app, client, _seed_auth):
        """访问他人书籍：SSE 流内含 403 error 事件。"""
        tok = _seed_auth['token']
        bid = _seed_auth['other_book_id']
        with app.test_client() as c:
            resp = c.post(f'/api/books/{bid}/ai-outline-volume',
                          json={'volume_index': 1},
                          headers={'Authorization': f'Bearer {tok}'})
            assert resp.status_code == 200
            assert resp.headers.get('Content-Type', '').startswith('text/event-stream')
            body = resp.data.decode('utf-8', 'replace')
        # 403 error 事件被包裹在 data:{"error":...} 里返回
        assert '无权' in body

    # ---------- 协议头 ----------
    def test_sse_anti_buffering_headers(self, app, client, _seed_auth):
        """Render/Cloudflare 反缓冲头必须存在，否则代理攒完整段才发首字节。"""
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        with app.test_client() as c:
            resp = c.post(f'/api/books/{bid}/ai-outline-volume',
                          json={'volume_index': 1},
                          headers={'Authorization': f'Bearer {tok}'})
            ct = resp.headers.get('Content-Type', '')
            cc = resp.headers.get('Cache-Control', '')
            xab = resp.headers.get('X-Accel-Buffering', '')
            conn = resp.headers.get('Connection', '')
        assert ct.startswith('text/event-stream'), f'Content-Type={ct}'
        assert 'no-cache' in cc.lower(),    f'Cache-Control={cc}'
        assert xab.lower() == 'no',          f'X-Accel-Buffering={xab}'
        assert 'keep-alive' in conn.lower(),f'Connection={conn}'

    # ---------- 事件流 ----------
    def test_sse_events_well_formed(self, app, client, _seed_auth):
        """start / heartbeat 兼容帧 / error(或 result) / [DONE] 事件格式正确。"""
        tok = _seed_auth['token']
        # 用不存在 book 触发 404 错误路径；LLM 不会被调用，适合 CI 断网环境
        bid = '00000000-0000-0000-0000-000000000000'
        with app.test_client() as c:
            resp = c.post(f'/api/books/{bid}/ai-outline-volume',
                          json={'volume_index': 1, 'volume_title': '第1卷'},
                          headers={'Authorization': f'Bearer {tok}'})
            assert resp.status_code == 200
            body = resp.data.decode('utf-8', 'replace')

        frames = [f for f in body.split('\n\n') if f.strip()]
        assert frames, '至少产出 1 帧 SSE 事件'
        # 每帧都必须以 data: 开头（之前裸字符串心跳的回归）
        for f in frames:
            assert f.startswith('data:'), f'发现非法帧（无 data: 前缀）：{f[:100]!r}'
        # 最后一帧必须是 [DONE]
        last_raw = frames[-1]
        last_data = last_raw.split('\n', 1)[0][5:].strip()
        assert last_data == '[DONE]', f'尾帧应为 data: [DONE]，实际：{last_data!r}'
        # 第一帧：start
        import json
        first_data = frames[0].split('\n', 1)[0][5:].strip()
        first_json = json.loads(first_data)
        assert first_json.get('type') == 'start'
        # 中间帧：至少包含 1 个 error（book 不存在）
        has_err = False
        for f in frames[1:-1]:
            raw = f.split('\n', 1)[0]
            if raw.startswith('data:'):
                try:
                    d = json.loads(raw[5:].strip())
                except Exception:
                    continue
                if 'error' in d:
                    has_err = True
                    assert isinstance(d['error'], str)
                    break
        assert has_err, '错误路径未在 SSE 事件流中返回 error'

    def test_view_func_decorated_with_login_required(self, app):
        """AST 级断言：ai_outline_volume 视图上方必须有 @login_required。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8')
        # 找 "def ai_outline_volume(" 前最近的装饰器
        lines = src.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith('def ai_outline_volume('):
                # 向上检索装饰器（跳过空/注释，找 @ 开头行）
                decorators = []
                for j in range(i - 1, -1, -1):
                    s = lines[j].strip()
                    if not s:
                        continue
                    if s.startswith('@'):
                        decorators.append(s)
                        continue
                    break
                assert any('@login_required' in d for d in decorators), \
                    f'ai_outline_volume 缺 @login_required 装饰器，邻近装饰器：{decorators}'
                return
        pytest.fail('未找到 ai_outline_volume 视图定义')
