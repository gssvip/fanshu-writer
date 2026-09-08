"""ai-continue 指纹回归测试（P0：int(UUID) 崩溃）。

背景（2026-09-08 沙箱实测）：_build_continue_fingerprint_deps 假设整型主键，
对 BookBible.id / Book.id / Chapter.id 做 int()，但全库主键是 UUID 字符串
→ ValueError → /ai-continue* 三端点首次调用必 500（bible 行还会被先自动创建，
即任何书第一次续写就崩）。该路径此前无任何测试覆盖。

修复：指纹元组统一用 str() 保存字符串 id（只要求稳定可比，不要求数值）。

验证范围：
  1. 单元：指纹函数接受 UUID 实体不抛异常，且同输入同输出（缓存正确性前提）。
  2. 端到端：登录 → 建书 → 写圣经（UUID bible 行）→ POST /ai-continue/stream
     → 200 + text/event-stream + meta/delta 帧，绝无 500 或 invalid literal。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------- 单元级 ----
class TestFingerprintDeps:
    def test_accepts_uuid_ids(self, app):
        """Book/BookBible/章节 id 均为 UUID 字符串时指纹函数不抛异常。"""
        from datetime import datetime
        from app import Book, BookBible
        from app import _build_continue_fingerprint_deps

        with app.app_context():
            bb = BookBible(book_id='b1-uuid-0001')
            bb.concept = '东方玄幻：少年寻妹'
            bb.updated_at = datetime(2026, 9, 8, 12, 0, 0)
            book = Book(id='book-uuid-0002', user_id='u1', title='我的书', genre='xuanhuan')
            deps = _build_continue_fingerprint_deps(
                'b1-uuid-0001', bb, '续写第一章', ['sp1'], 1,
                '上一章内容……', ['xuanhuan_style'], True,
                False, book=book, recent_4ch_ids=[('ch-uuid-0003', 2400)],
            )
        assert isinstance(deps, tuple) and len(deps) == 4

    def test_none_bible_and_book(self, app):
        """bb/book 为 None 的兜底分支保持可用（无书/无圣经场景）。"""
        from app import _build_continue_fingerprint_deps
        with app.app_context():
            deps = _build_continue_fingerprint_deps(
                'b1', None, '', [], None, None, [], True, False,
                book=None, recent_4ch_ids=None,
            )
        assert isinstance(deps, tuple) and len(deps) == 4

    def test_same_input_same_output(self, app):
        """同输入同输出：指纹是缓存 Key 的正确性前提。"""
        from datetime import datetime
        from app import Book, BookBible
        from app import _build_continue_fingerprint_deps

        with app.app_context():
            bb = BookBible(book_id='b1-uuid-0001')
            bb.concept = '设定内容'
            bb.updated_at = datetime(2026, 9, 8, 12, 0, 0)
            book = Book(id='book-uuid-0002', user_id='u1', title='我的书', genre='xuanhuan')
            args = ('b1-uuid-0001', bb, '指令', ['sp1'], 2, '上文', [], True, False)
            kw = dict(book=book, recent_4ch_ids=[('ch-uuid-0003', 2400)])
            d1 = _build_continue_fingerprint_deps(*args, **kw)
            d2 = _build_continue_fingerprint_deps(*args, **kw)
        assert d1 == d2

    def test_bible_content_change_alters_fingerprint(self, app):
        """设定内容变化 → 指纹变化 → 缓存自动 MISS（零脏读语义保持）。"""
        from datetime import datetime
        from app import BookBible
        from app import _build_continue_fingerprint_deps

        with app.app_context():
            bb1 = BookBible(book_id='b1')
            bb1.concept = '旧设定'
            bb1.updated_at = datetime(2026, 9, 8, 12, 0, 0)
            bb2 = BookBible(book_id='b1')
            bb2.concept = '新设定（已改）'
            bb2.updated_at = datetime(2026, 9, 8, 12, 0, 0)
            d1 = _build_continue_fingerprint_deps('b1', bb1, '', [], None, None, [], True, False)
            d2 = _build_continue_fingerprint_deps('b1', bb2, '', [], None, None, [], True, False)
        assert d1 != d2


# ------------------------------------------------------------- 端到端级 ----
class _FakeStreamResp:
    """_post_llm_adaptive 的流式假响应（标准 OpenAI SSE 格式）。"""

    status_code = 200
    text = ''

    def __init__(self, content):
        self._lines = [
            'data: ' + json.dumps(
                {'choices': [{'delta': {'role': 'assistant', 'content': ''}}]},
                ensure_ascii=False),
            'data: ' + json.dumps(
                {'choices': [{'delta': {'content': content}}]},
                ensure_ascii=False),
            'data: ' + json.dumps(
                {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                ensure_ascii=False),
            'data: [DONE]',
        ]

    def iter_lines(self):
        for line in self._lines:
            yield line.encode('utf-8')


@pytest.fixture()
def _seed_auth(app):
    """测试用户 + 有效 token + 书 + 圣经行（全 UUID 主键，即 P0 触发条件）。"""
    from datetime import datetime, timedelta, timezone
    from app import db, User, AuthToken, Book, BookBible, Chapter, generate_token, hash_token
    with app.app_context():
        u = User(username='fp_reg_u1', password_hash='x')
        db.session.add(u)
        db.session.flush()
        token = generate_token()
        db.session.add(AuthToken(
            user_id=u.id, token=hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        book = Book(user_id=u.id, title='指纹回归之书', genre='xuanhuan')
        db.session.add(book)
        db.session.flush()
        # 圣经 + 章节一并就位：ai-continue/stream 会走指纹快路径（修复前的崩溃点）
        bb = BookBible(book_id=book.id, concept='东方玄幻：少年寻妹',
                       character_profiles='林昭|17岁|主角', key_rules='炼气→筑基→金丹')
        db.session.add(bb)
        db.session.add(Chapter(book_id=book.id, title='第一章 夜巷', order_index=1,
                               content='　　夜色覆在青石巷上头，林昭提灯前行。', word_count=20))
        db.session.commit()
        return {'token': token, 'book_id': book.id}


def _stream(client, book_id, token):
    return client.post(
        f'/api/books/{book_id}/ai-continue/stream',
        json={'instruction': '续写：林昭在巷子尽头遭遇伏击', 'word_count': 300,
              'target_chapter_num': 2},
        headers={'Authorization': f'Bearer {token}'})


@pytest.mark.usefixtures('app')
class TestAiContinueStreamE2E:

    def test_stream_succeeds_with_uuid_bible(self, app, client, monkeypatch, _seed_auth):
        """核心回归：存在 UUID 圣经行时续写流不再 500。"""
        import app as appmod

        monkeypatch.setattr(
            appmod, '_post_llm_adaptive',
            lambda *a, **kw: _FakeStreamResp('　　剑出鞘的那一瞬，风停了。不是风自己停的，是剑太快。'))
        r = _stream(client, _seed_auth['book_id'], _seed_auth['token'])
        assert r.status_code == 200, f'期望 200，实际 {r.status_code}：{r.get_data(as_text=True)[:300]}'
        assert 'text/event-stream' in (r.headers.get('Content-Type') or '')
        body = r.get_data(as_text=True)
        # meta 首帧 + delta 内容帧必须存在
        assert '"meta": true' in body
        assert '剑出鞘的那一瞬' in body
        # 绝不再出现指纹崩溃（ValueError 被顶层 except 转成 error 帧或直接 500）
        assert 'invalid literal' not in body
        assert '"error"' not in body.replace('"error_count"', '')

    def test_stream_second_call_cache_hit_path(self, app, client, monkeypatch, _seed_auth):
        """二次调用走 PromptContextCache 命中路径（同书同参数），同样不崩。"""
        import app as appmod

        monkeypatch.setattr(
            appmod, '_post_llm_adaptive',
            lambda *a, **kw: _FakeStreamResp('　　他没有回头。修行十年，回头的人命都不长。'))
        bid, tok = _seed_auth['book_id'], _seed_auth['token']
        r1 = _stream(client, bid, tok)
        r2 = _stream(client, bid, tok)
        assert r1.status_code == 200 and r2.status_code == 200
        assert '他没有回头' in r2.get_data(as_text=True)
        assert 'invalid literal' not in r2.get_data(as_text=True)
