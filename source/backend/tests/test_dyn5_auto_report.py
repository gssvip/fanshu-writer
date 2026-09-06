"""【dyn5 + P2】动态报告全自动生成链路测试。

覆盖：
- `_check_and_auto_generate_report` 区间数学：缺多区间时按顺序补齐（导入回填语义）；
  max_intervals=1 时只补最早的缺失区间（增量语义）；
- 报告落库字段（chapter_start/chapter_end/auto_generated）；
- 路由级：保存第5章自动落 1-5 报告（平台生成链路）；
- P2 逆向通道在无 LLM 配置时静默跳过、不抛异常。

不测真实 LLM 调用（_generate_dynamic_report_content 会被 monkeypatch 打桩）。
"""
from __future__ import annotations

import uuid

import pytest


from tests.test_book_chapter import _auth_client, _create_book


@pytest.fixture()
def ctx(app):
    """推送 app context，供直接操作模型层。"""
    with app.app_context():
        yield app


def _mk_chapters(book_id, n):
    """直接落库 n 个非卷章节（绕过路由钩子，专注测区间数学）。"""
    from app import db, Chapter
    for i in range(n):
        ch = Chapter(
            book_id=book_id, title=f'第{i + 1}章', content=f'第{i + 1}章正文内容。',
            order_index=i, is_volume=False, parent_id='',
        )
        db.session.add(ch)
    db.session.commit()


def test_backfill_all_intervals_in_order(client, ctx, monkeypatch):
    """导入场景：12 章应有 1-5、6-10 两份报告，max_intervals=None 全量按顺序补齐。"""
    from app import DynamicReport, _check_and_auto_generate_report
    headers = _auth_client(client)
    book = _create_book(client, headers, title="导入小说")
    _mk_chapters(book['id'], 12)

    calls = []

    def fake_gen(book_id, cs, ce, skill_pack_ids=None):
        calls.append((cs, ce))
        return f'第{cs}-{ce}章摘要', None

    monkeypatch.setattr('app._generate_dynamic_report_content', fake_gen)
    monkeypatch.setattr('app._revise_dimensions_from_chapters', lambda *a, **k: None)

    result = _check_and_auto_generate_report(book['id'], max_intervals=None)

    # 12 章 → 区间 1-5、6-10（11-12 不足5章不生成），按顺序生成
    assert calls == [(1, 5), (6, 10)]
    assert result is not None and 'report' in result
    reports = DynamicReport.query.filter_by(book_id=book['id']).order_by(DynamicReport.chapter_start).all()
    assert [(r.chapter_start, r.chapter_end) for r in reports] == [(1, 5), (6, 10)]
    assert all(r.auto_generated for r in reports)


def test_incremental_fills_earliest_gap(client, ctx, monkeypatch):
    """增量场景：缺两个区间时每次只补最早的那个（按顺序追平）。"""
    from app import _check_and_auto_generate_report
    headers = _auth_client(client)
    book = _create_book(client, headers, title="增量追平")
    _mk_chapters(book['id'], 12)

    calls = []

    def fake_gen(book_id, cs, ce, skill_pack_ids=None):
        calls.append((cs, ce))
        return f'第{cs}-{ce}章摘要', None

    monkeypatch.setattr('app._generate_dynamic_report_content', fake_gen)
    monkeypatch.setattr('app._revise_dimensions_from_chapters', lambda *a, **k: None)

    # 第一次保存：只补 1-5
    _check_and_auto_generate_report(book['id'])
    assert calls == [(1, 5)]
    # 第二次保存：补 6-10
    _check_and_auto_generate_report(book['id'])
    assert calls == [(1, 5), (6, 10)]
    # 第三次：无缺失区间，不再生成
    assert _check_and_auto_generate_report(book['id']) is None


def test_no_intervals_below_five(client, ctx, monkeypatch):
    """不足5章不生成。"""
    from app import DynamicReport, _check_and_auto_generate_report
    headers = _auth_client(client)
    book = _create_book(client, headers, title="四章书")
    _mk_chapters(book['id'], 4)

    def fail_gen(*a, **k):
        raise AssertionError('不应触发生成')

    monkeypatch.setattr('app._generate_dynamic_report_content', fail_gen)
    assert _check_and_auto_generate_report(book['id']) is None
    assert DynamicReport.query.filter_by(book_id=book['id']).count() == 0


def test_existing_reports_not_duplicated(client, ctx, monkeypatch):
    """已有区间的报告不重复生成（幂等）。"""
    from app import db, DynamicReport, _check_and_auto_generate_report
    headers = _auth_client(client)
    book = _create_book(client, headers, title="幂等书")
    _mk_chapters(book['id'], 5)
    db.session.add(DynamicReport(
        book_id=book['id'], title='动态-(1-5章)', content='已有',
        chapter_start=1, chapter_end=5, auto_generated=True,
    ))
    db.session.commit()

    def fail_gen(*a, **k):
        raise AssertionError('不应重复生成')

    monkeypatch.setattr('app._generate_dynamic_report_content', fail_gen)
    assert _check_and_auto_generate_report(book['id'], max_intervals=None) is None


def test_create_chapter_triggers_auto_report(client, ctx, monkeypatch):
    """路由级：保存第5章内容后自动落一份 1-5 报告（平台生成链路）。"""
    from app import _check_and_auto_generate_report  # noqa: F401  确认可导入
    headers = _auth_client(client)
    book = _create_book(client, headers, title="平台写作")

    monkeypatch.setattr('app._generate_dynamic_report_content', lambda bid, cs, ce, skill_pack_ids=None: (f'第{cs}-{ce}章摘要', None))
    # P2 修订线程在测试里直接短路（避免起线程访问测试回滚的会话）
    monkeypatch.setattr('app._revise_dimensions_from_chapters_async', lambda *a, **k: None)
    # 防遗忘检查同样短路（其内部会起线程调 LLM；路由内 from app import 在调用时取属性，patch app 层即可）
    monkeypatch.setattr('app._maybe_auto_trigger_anti_forget_check', lambda *a, **k: None)

    for i in range(4):
        resp = client.post(f"/api/books/{book['id']}/chapters", json={
            'title': f'第{i + 1}章', 'content': f'第{i + 1}章正文。',
        }, headers=headers)
        assert resp.status_code == 201, resp.get_json()
        assert 'auto_report' not in resp.get_json()

    resp = client.post(f"/api/books/{book['id']}/chapters", json={
        'title': '第5章', 'content': '第5章正文。',
    }, headers=headers)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    # 第5章保存触发自动生成
    assert 'auto_report' in body
    assert body['auto_report']['chapter_start'] == 1
    assert body['auto_report']['chapter_end'] == 5
    assert body['auto_report']['auto_generated'] is True


def test_revise_dimensions_no_llm_config(client, ctx):
    """P2 逆向通道：未配置 AI Key 时静默返回 None，不抛异常。"""
    from app import _revise_dimensions_from_chapters
    headers = _auth_client(client)
    book = _create_book(client, headers, title="无AI配置")
    _mk_chapters(book['id'], 5)
    # 测试环境未配置 AIConfig → 直接返回 None
    assert _revise_dimensions_from_chapters(book['id'], 1, 5) is None
