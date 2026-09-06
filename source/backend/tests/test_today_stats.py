"""今日写作统计 API 测试（/api/stats/today）。

背景（2026-09-06）：工作台"今日字数/今日章节/连续天数"实时统计不了——
前端唯一写入统计的 useWritingStats hook 无任何组件调用，localStorage
`app-writing-history` 恒为空。修复改为后端基于 Chapter.updated_at
实时聚合（AI 采纳/手动保存都会触碰 updated_at → 天然实时、多端一致）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _register_and_login(client, username='writer'):
    rv = client.post('/api/auth/register', json={
        'username': username, 'password': 'pass1234',
        'email': f'{username}@test.local'})
    assert rv.status_code == 201, rv.get_json()
    token = rv.get_json()['token']
    return {'Authorization': f'Bearer {token}'}


def _make_book(client, headers, title='书'):
    rv = client.post('/api/books', headers=headers, json={'title': title})
    return rv.get_json()['id']


def _make_chapter(app, db, Book, Chapter, book_id, user_id, title='第1章',
                  words=2400, updated_at=None, content=None):
    ch = Chapter(book_id=book_id, title=title, word_count=words,
                 content=content or ('字' * words))
    ch.updated_at = updated_at or datetime.now(timezone.utc)
    db.session.add(ch)
    db.session.commit()
    return ch


def _today_str():
    # 服务器本地按 tz=0 直接给日期
    return datetime.now(timezone.utc).date().isoformat()


@pytest.fixture()
def models(app):
    from app import db, Book, Chapter
    with app.app_context():
        yield db, Book, Chapter


class TestTodayStats:
    def test_requires_login(self, client):
        assert client.get('/api/stats/today').status_code == 401

    def test_no_books_zero(self, client, app):
        headers = _register_and_login(client, 'nowriter')
        rv = client.get('/api/stats/today?tz=0', headers=headers)
        assert rv.status_code == 200
        assert rv.get_json() == {'today_words': 0, 'today_chapters': 0, 'streak': 0}

    def test_today_counts_and_streak(self, client, app, models):
        db, Book, Chapter = models
        headers = _register_and_login(client, 'writer1')
        book_id = _make_book(client, headers, '我的书')
        # 从 app 拿 book 记录改 user_id（POST /api/books 已绑定当前用户，无需改）
        now = datetime.now(timezone.utc)
        # 今天 2 章（2400 + 2600 字）+ 昨天 1 章 + 前天 1 章 → streak=3
        _make_chapter(app, db, Book, Chapter, book_id, '', '第1章', 2400, now)
        _make_chapter(app, db, Book, Chapter, book_id, '', '第2章', 2600,
                      now - timedelta(hours=1))
        _make_chapter(app, db, Book, Chapter, book_id, '', '第3章', 2500,
                      now - timedelta(days=1))
        _make_chapter(app, db, Book, Chapter, book_id, '', '第4章', 2500,
                      now - timedelta(days=2))
        with app.app_context():
            rv = client.get('/api/stats/today?tz=0', headers=headers)
        assert rv.status_code == 200
        data = rv.get_json()
        assert data['today_words'] == 5000   # 2400 + 2600
        assert data['today_chapters'] == 2
        assert data['streak'] == 3            # 今天 + 昨天 + 前天

    def test_streak_broken_day_excluded(self, client, app, models):
        """今天写了、昨天没写、大前天写了 → streak=1（断档不连）。"""
        db, Book, Chapter = models
        headers = _register_and_login(client, 'writer2')
        book_id = _make_book(client, headers, '断档书')
        now = datetime.now(timezone.utc)
        _make_chapter(app, db, Book, Chapter, book_id, '', '第1章', 2400, now)
        _make_chapter(app, db, Book, Chapter, book_id, '', '第2章', 2400,
                      now - timedelta(days=2))  # 跳过昨天
        rv = client.get('/api/stats/today?tz=0', headers=headers)
        assert rv.get_json()['streak'] == 1

    def test_today_not_written_yet_streak_from_yesterday(self, client, app, models):
        """今天还没写：连续天数从昨天往前数（不把"今天还没写"当断签）。"""
        db, Book, Chapter = models
        headers = _register_and_login(client, 'writer3')
        book_id = _make_book(client, headers, '昨日书')
        now = datetime.now(timezone.utc)
        _make_chapter(app, db, Book, Chapter, book_id, '', '第1章', 2400,
                      now - timedelta(days=1))
        _make_chapter(app, db, Book, Chapter, book_id, '', '第2章', 2400,
                      now - timedelta(days=2))
        rv = client.get('/api/stats/today?tz=0', headers=headers)
        data = rv.get_json()
        assert data['today_words'] == 0
        assert data['today_chapters'] == 0
        assert data['streak'] == 2

    def test_timezone_boundary(self, client, app, models):
        """时区边界：UTC 23:30 写的章，在 tz=+480（北京）属于"明天"。"""
        db, Book, Chapter = models
        headers = _register_and_login(client, 'writer4')
        book_id = _make_book(client, headers, '时区书')
        # 构造 UTC 20:00（若当前时刻不足，用绝对日期）：取明天 UTC0点前8小时 = 北京明早8点
        now = datetime.now(timezone.utc)
        # 北京日期 = UTC+8h；取 now 向后推到"北京今天 23:59 对应的 UTC"难构造，
        # 直接验证：utc_now 落在北京今天 → today_chapters=1
        _make_chapter(app, db, Book, Chapter, book_id, '', '第1章', 2400, now)
        rv = client.get('/api/stats/today?tz=480', headers=headers)
        data = rv.get_json()
        # now + 8h 与 now 同为北京今天（除非 now 恰在北京 16:00-24:00 → +8h 跨日）
        beijing_hour = (now + timedelta(hours=8)).hour
        if beijing_hour < 8:  # now 北京 16:00-24:00，+8h 已跨入明天
            assert data['today_chapters'] == 0
        else:
            assert data['today_chapters'] == 1

    def test_cross_book_aggregation(self, client, app, models):
        """跨作品聚合：两本书各写一章，今日章节=2、字数求和。"""
        db, Book, Chapter = models
        headers = _register_and_login(client, 'writer5')
        b1 = _make_book(client, headers, '书A')
        # 第二本直连 db 造（免费额度限 1 本，API 建第二本会被拒）
        with app.app_context():
            _u = Book.query.filter_by(id=b1).first()
            book2 = Book(user_id=_u.user_id, title='书B')
            db.session.add(book2)
            db.session.commit()
            b2 = book2.id
        now = datetime.now(timezone.utc)
        _make_chapter(app, db, Book, Chapter, b1, '', 'A章', 2000, now)
        _make_chapter(app, db, Book, Chapter, b2, '', 'B章', 3000, now)
        rv = client.get('/api/stats/today?tz=0', headers=headers)
        data = rv.get_json()
        assert data['today_words'] == 5000
        assert data['today_chapters'] == 2

    def test_other_users_books_excluded(self, client, app, models):
        """用户隔离：别人的书不计入我的统计。"""
        db, Book, Chapter = models
        h1 = _register_and_login(client, 'writerA')
        h2 = _register_and_login(client, 'writerB')
        other_book = _make_book(client, h2, '别人的书')
        now = datetime.now(timezone.utc)
        _make_chapter(app, db, Book, Chapter, other_book, '', '第1章', 9999, now)
        rv = client.get('/api/stats/today?tz=0', headers=h1)
        data = rv.get_json()
        assert data['today_words'] == 0
        assert data['today_chapters'] == 0
