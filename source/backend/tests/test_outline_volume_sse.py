"""节点设计 后台异步任务 + 短轮询 回归（最终架构，根治 502/network error）。

背景：mayi.chat 经 Cloudflare + Render 双层代理，对同一条连接有 ~100s 硬超时。
生成整卷大纲需 2-5 分钟，任何长连接（同步 POST、SSE+心跳）都会被中间层切掉
（浏览器抛 "TypeError: network error"）。故 POST 秒回 {job_id}，后台线程跑生成，
前端每 3s 轮询短 GET /status —— 每个 HTTP 请求都 <50ms，永不触发网关超时。

验证范围：
  1. 鉴权/归属：未登录 401；他人书 403（POST 同步秒回）；book 不存在 404。
  2. POST 语义：秒回 {job_id}，NOT SSE 流式；幂等去重回 202。
  3. 短轮询 GET /status：running → 终态(done/error)；未知 job_id 404；越权 403。
  4. _ai_outline_volume_impl 仍返回 (dict, status)，无 return jsonify 残留。
"""
import ast
import time

import pytest

# 端到端轮询：后台线程在 CI(无 AI Key)下走“请先配置 API Key”快速 error 终态
_POLL_DEADLINE = 8.0


@pytest.fixture()
def _clean_jobs(app):
    """每测试后清空模块级任务注册表，避免跨测试依赖/串扰。"""
    yield
    import app as _am
    with _am._job_lock:
        _am._jobs.clear()
        _am._job_active.clear()


@pytest.fixture()
def _seed_auth(app, client):
    """创建测试用户+书籍+有效 token，返回 dict{token/own_book_id/other_book_id}。"""
    from datetime import datetime, timedelta, timezone
    from app import db, User, AuthToken, Book, generate_token, hash_token
    with app.app_context():
        u1 = User(username='nodejob_u1', password_hash='x')
        u2 = User(username='nodejob_u2', password_hash='y')
        db.session.add_all([u1, u2]); db.session.flush()
        ex = datetime.now(timezone.utc) + timedelta(days=30)
        t1 = generate_token()
        # 库里存哈希（与 login_required 校验口径一致），客户端拿到明文 t1
        db.session.add(AuthToken(user_id=u1.id, token=hash_token(t1), expires_at=ex))
        b_own   = Book(user_id=u1.id, title='我的书', genre='xuanhuan')
        b_other = Book(user_id=u2.id, title='他人的书', genre='xuanhuan')
        db.session.add_all([b_own, b_other]); db.session.commit()
        return {'token': t1, 'own_book_id': b_own.id, 'other_book_id': b_other.id}


def _poll_status(client, book_id, job_id, token, deadline=_POLL_DEADLINE):
    """轮询至终态，返回 (state, body_dict)。"""
    t0 = time.time()
    while time.time() - t0 < deadline:
        r = client.get(f'/api/books/{book_id}/ai-outline-volume/status?job_id={job_id}',
                       headers={'Authorization': f'Bearer {token}'})
        j = r.get_json() or {}
        if j.get('state') in ('done', 'error'):
            return j.get('state'), j
        time.sleep(0.08)
    return j.get('state'), j


@pytest.mark.usefixtures("app", "_clean_jobs")
class TestOutlineVolumeJob:

    # ---------- 基础 impl ----------
    def test_impl_returns_tuple_not_jsonify(self, app):
        from app import _ai_outline_volume_impl
        with app.app_context():
            payload, status = _ai_outline_volume_impl('no-such-book', {})
        assert status == 404
        assert isinstance(payload, dict) and 'error' in payload

    def test_impl_no_return_jsonify_left(self, app):
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

    # ---------- 鉴权/归属（POST 应同步秒回错误） ----------
    def test_route_requires_login(self, app, client):
        r = client.post('/api/books/any-id/ai-outline-volume', json={'volume_index': 1})
        assert r.status_code == 401
        assert not r.is_json or ('error' in (r.get_json() or {})) or r.status_code == 401

    def test_post_forbids_other_users_book_sync(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['other_book_id']
        r = client.post(f'/api/books/{bid}/ai-outline-volume',
                        json={'volume_index': 1},
                        headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 403
        assert '无权' in (r.get_json() or {}).get('error', '')

    def test_post_not_found_404(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        r = client.post('/api/books/00000000-0000-0000-0000-000000000000/ai-outline-volume',
                        json={'volume_index': 1},
                        headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 404

    # ---------- POST 秒回 job_id（短请求，非 SSE） ----------
    def test_post_returns_job_id_immediately(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        r = client.post(f'/api/books/{bid}/ai-outline-volume',
                        json={'volume_index': 1, 'volume_title': '第1卷'},
                        headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code in (200, 202)
        assert r.is_json, f'POST 必须返回 JSON，而非 SSE 流：{r.headers.get("Content-Type")}'
        body = r.get_json()
        assert body.get('job_id')

    def test_post_dedupes_same_volume(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        h = {'Authorization': f'Bearer {tok}'}
        r1 = client.post(f'/api/books/{bid}/ai-outline-volume', json={'volume_index': 1}, headers=h)
        r2 = client.post(f'/api/books/{bid}/ai-outline-volume', json={'volume_index': 1}, headers=h)
        j1, j2 = r1.get_json(), r2.get_json()
        assert j1.get('job_id') == j2.get('job_id'), '同卷同 mode 应复用同一 job'
        assert r2.status_code == 202

    # ---------- 短轮询 status ----------
    def test_status_reaches_terminal(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        h = {'Authorization': f'Bearer {tok}'}
        r = client.post(f'/api/books/{bid}/ai-outline-volume', json={'volume_index': 1}, headers=h)
        job_id = r.get_json()['job_id']
        state, j = _poll_status(client, bid, job_id, tok)
        assert state in ('done', 'error'), f'任务应在超时内到达终态，实际 state={state} body={j}'
        if state == 'error':
            # CI 无 AI Key 时是“请先配置”等可读错误；必须字符串
            assert isinstance(j.get('error'), str) and j['error']

    def test_status_unknown_job_404(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        r = client.get(f'/api/books/{bid}/ai-outline-volume/status?job_id=deadbeef',
                       headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 404

    def test_status_forbids_other_user(self, app, client, _seed_auth):
        tok = _seed_auth['token']
        bid = _seed_auth['own_book_id']
        h = {'Authorization': f'Bearer {tok}'}
        r = client.post(f'/api/books/{bid}/ai-outline-volume', json={'volume_index': 1}, headers=h)
        job_id = r.get_json()['job_id']
        # 用一个“非 owner”的 token 试探：当前注册表只存了这一本书的 owner，
        # 用无效/他人 token 查询应被拒（403）或 401。这里用空 token → 401。
        r2 = client.get(f'/api/books/{bid}/ai-outline-volume/status?job_id={job_id}')
        assert r2.status_code in (401, 403)