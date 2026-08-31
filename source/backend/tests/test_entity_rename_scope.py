"""运行期验证：实体重命名覆盖"新章节生成"的全部数据来源。

用户诉求：实体注册表里把角色重命名后，既有的小说正文 与 新生成章节都不能再读到旧名。
正文生成的上下文注入来源包括：
  - Chapter.content（已覆盖）
  - DynamicReport 表（新覆盖）
  - EventLog（bb.event_log_json，新覆盖）
  - Character 表 name（新覆盖）
  - 实体注册表实体键（bb.entity_registry_json，新覆盖）

不依赖网络/LLM，仅用 SQLite 临时库做模型层断言。
"""
from __future__ import annotations

import json
import uuid

import pytest


def _make_data(app):
    """插入一个带旧名「顾晨」的运行环境，返回 (bb, chapters, business_ids)。"""
    from app import db, Book, BookBible, Chapter, DynamicReport, Character

    book_id = uuid.uuid4().hex
    bb = BookBible(
        book_id=book_id,
        concept='顾晨是主角。',
        event_log_json=json.dumps([
            {'id': 'e1', 'chapter_num': 1, 'type': 'turn',
             'actors': ['顾晨'], 'summary': '顾晨做出决定', 'location': '顾晨宅邸'},
        ], ensure_ascii=False),
        entity_registry_json=json.dumps({
            'characters': {'顾晨': {'aliases': [], 'refs': ['event'], 'last_seen_ch': 1}},
        }, ensure_ascii=False),
    )
    report = DynamicReport(book_id=book_id, title='动态-(1-5章)', content='顾晨与同伴商议对策。')
    char = Character(book_id=book_id, name='顾晨', role='protagonist',
                     relationships_json=json.dumps([{'other': '顾晨'}], ensure_ascii=False))
    ch = Chapter(book_id=book_id, title='第一章', content='顾晨走进顾晨宅邸。', order_index=1, is_volume=False)
    db.session.add(Book(id=book_id, title='测试书', synopsis=''))
    db.session.add(bb)
    db.session.add(report)
    db.session.add(char)
    db.session.add(ch)
    db.session.commit()
    return bb, [ch], {'report_id': report.id, 'char_id': char.id, 'book_id': book_id}


@pytest.mark.usefixtures("app")
class TestEntityRenameScope:
    def test_rename_covers_all_generation_sources(self, app):
        from app import db, DynamicReport, Character
        from entity_registry import rename_entity

        with app.app_context():
            bb, chapters, ids = _make_data(app)

            result = rename_entity(bb, chapters, '顾晨', '陈岩', 'character')

            db.session.commit()  # 模拟 HTTP 路由 / 智驾的提交

            assert result['success'] is True
            assert 'dynamic_reports' in result['fields_updated']
            assert 'event_log' in result['fields_updated']
            assert 'character_table' in result['fields_updated']
            assert 'entity_registry' in result['fields_updated']

            # 1. Chapter.content 正文替换
            assert '陈岩走进陈岩宅邸。' == chapters[0].content
            assert '顾晨' not in chapters[0].content

            # 2. DynamicReport 表
            report = db.session.get(DynamicReport, ids['report_id'])
            assert '顾晨' not in report.content
            assert '陈岩与同伴商议对策。' == report.content

            # 3. EventLog
            events = json.loads(bb.event_log_json)
            assert all('顾晨' not in e['summary'] and '顾晨' not in e['location']
                       and all('顾晨' not in a for a in e['actors']) for e in events)
            assert events[0]['actors'] == ['陈岩']

            # 4. Character 表 name + relationships
            char = db.session.get(Character, ids['char_id'])
            assert char.name == '陈岩'
            assert '顾晨' not in char.relationships_json
            assert db.session.query(Character).filter_by(name='顾晨').count() == 0

            # 5. 实体注册表键迁移
            reg = json.loads(bb.entity_registry_json)
            assert '陈岩' in reg['characters']
            assert '顾晨' not in reg['characters']