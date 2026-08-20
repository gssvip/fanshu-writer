"""聊天驱动创作模块单元测试。

覆盖 chat_collab_bp 的纯逻辑函数：
  - parse_cards / strip_cards：Action Card 解析与剥离
  - build_progress_map：创作进度地图
  - build_chat_system_prompt：维度感知系统提示词构建
  - build_context_messages：上下文滑窗管理
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _FakeBook:
    def __init__(self, title='测试书'):
        self.title = title


class _FakeBB:
    """模拟 BookBible，所有维度默认空字符串。"""
    def __init__(self, **kwargs):
        for field in ['worldbuilding', 'character_profiles', 'timeline',
                      'foreshadowing', 'style_guide', 'key_rules',
                      'locations', 'concept', 'plot_design']:
            setattr(self, field, kwargs.get(field, ''))


class TestParseCards:
    def test_parse_single_card(self):
        from blueprints.chat_collab_bp import parse_cards
        text = '我们讨论下主角\n[[CARD:SAVE_CHARACTER|林天|主角，剑修，性格坚毅]]'
        cards = parse_cards(text)
        assert len(cards) == 1
        c = cards[0]
        assert c['type'] == 'SAVE_CHARACTER'
        assert c['title'] == '林天'
        assert '剑修' in c['content']
        assert c['target'] == '人物'

    def test_parse_multiple_cards(self):
        from blueprints.chat_collab_bp import parse_cards
        text = ('[[CARD:SAVE_WORLDSETTING|灵石体系|灵石分九品]]\n'
                '中间一些讨论\n'
                '[[CARD:SAVE_FORESHADOW|主角身世|主角是天族后裔]]')
        cards = parse_cards(text)
        assert len(cards) == 2
        assert cards[0]['type'] == 'SAVE_WORLDSETTING'
        assert cards[1]['type'] == 'SAVE_FORESHADOW'

    def test_parse_invalid_card_type_ignored(self):
        from blueprints.chat_collab_bp import parse_cards
        text = '[[CARD:UNKNOWN_TYPE|标题|内容]]'
        cards = parse_cards(text)
        assert len(cards) == 0

    def test_parse_card_with_pipe_in_content(self):
        from blueprints.chat_collab_bp import parse_cards
        # 内容中含 | 应被最后一个 | 分隔捕获到内容里
        text = '[[CARD:SAVE_RULE|境界划分|炼气|筑基|金丹]]'
        cards = parse_cards(text)
        assert len(cards) == 1
        assert cards[0]['title'] == '境界划分'
        assert '炼气|筑基|金丹' in cards[0]['content']

    def test_parse_empty_text(self):
        from blueprints.chat_collab_bp import parse_cards
        assert parse_cards('') == []
        assert parse_cards('普通聊天，没有卡片') == []

    def test_card_has_unique_id(self):
        from blueprints.chat_collab_bp import parse_cards
        cards = parse_cards('[[CARD:SAVE_CONCEPT|故事核|一句话]]')
        assert len(cards) == 1
        assert cards[0]['id']  # 非空


class TestStripCards:
    def test_strip_single_card(self):
        from blueprints.chat_collab_bp import strip_cards
        text = '讨论内容\n[[CARD:SAVE_CHARACTER|林天|主角]]\n后续'
        result = strip_cards(text)
        assert '讨论内容' in result
        assert '后续' in result
        assert '[[CARD' not in result

    def test_strip_no_card_unchanged(self):
        from blueprints.chat_collab_bp import strip_cards
        text = '普通聊天'
        assert strip_cards(text) == '普通聊天'

    def test_strip_preserves_newlines(self):
        from blueprints.chat_collab_bp import strip_cards
        text = '第一段\n\n[[CARD:SAVE_RULE|x|y]]\n\n第二段'
        result = strip_cards(text)
        # 剥离后内容仍包含两段
        assert '第一段' in result
        assert '第二段' in result


class TestBuildProgressMap:
    def test_empty_bible(self):
        from blueprints.chat_collab_bp import build_progress_map
        m = build_progress_map(_FakeBB())
        assert m['overall'] == 0
        assert m['filled'] == 0
        assert m['total'] == 8
        assert len(m['dims']) == 8
        # next_step 应指向第一个非 solid 维度（按优先级顺序）
        assert m['next_step'] is not None
        assert m['next_step']['field'] == 'concept'

    def test_solid_dim_counts_filled(self):
        from blueprints.chat_collab_bp import build_progress_map
        bb = _FakeBB(concept='x' * 600)  # 长度 >= 500 视为 solid
        m = build_progress_map(bb)
        assert m['filled'] == 1
        assert m['overall'] == 12  # round(1/8 * 100)

    def test_partial_dim_not_filled(self):
        from blueprints.chat_collab_bp import build_progress_map
        bb = _FakeBB(concept='短构思')  # < 100 字符，sketch
        m = build_progress_map(bb)
        assert m['filled'] == 0
        dim = next(d for d in m['dims'] if d['field'] == 'concept')
        assert dim['status'] == 'sketch'
        assert dim['pct'] == 30

    def test_next_step_skips_solid(self):
        from blueprints.chat_collab_bp import build_progress_map
        # concept 已完善，next_step 应跳到 character_profiles
        bb = _FakeBB(concept='x' * 600)
        m = build_progress_map(bb)
        assert m['next_step']['field'] == 'character_profiles'


class TestBuildContextMessages:
    def test_basic_assembly(self):
        from blueprints.chat_collab_bp import build_context_messages
        history = [
            {'role': 'user', 'content': '你好'},
            {'role': 'assistant', 'content': '你好呀'},
        ]
        msgs = build_context_messages('SYS', history, '新问题')
        assert msgs[0] == {'role': 'system', 'content': 'SYS'}
        assert msgs[-1] == {'role': 'user', 'content': '新问题'}
        assert len(msgs) == 4

    def test_sliding_window_truncation(self):
        from blueprints.chat_collab_bp import build_context_messages, MAX_HISTORY_ROUNDS
        # 构造超出滑窗的历史
        history = []
        for i in range(MAX_HISTORY_ROUNDS + 5):
            history.append({'role': 'user', 'content': f'问题{i}'})
            history.append({'role': 'assistant', 'content': f'回答{i}'})
        msgs = build_context_messages('SYS', history, '最新问题')
        # 应只保留最近 MAX_HISTORY_ROUNDS*2 条历史 + 1 system + 1 user
        assert len(msgs) == MAX_HISTORY_ROUNDS * 2 + 2

    def test_message_truncation(self):
        from blueprints.chat_collab_bp import build_context_messages, MAX_MSG_CHARS
        long_content = 'x' * (MAX_MSG_CHARS + 100)
        history = [{'role': 'user', 'content': long_content}]
        msgs = build_context_messages('SYS', history, '问题')
        # 历史消息应被截断到 MAX_MSG_CHARS
        assert len(msgs[1]['content']) == MAX_MSG_CHARS


class TestBuildChatSystemPrompt:
    def test_includes_book_title(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        prompt = build_chat_system_prompt(_FakeBook(title='我的小说'), None)
        assert '我的小说' in prompt

    def test_includes_card_instructions(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        prompt = build_chat_system_prompt(_FakeBook(), None)
        assert '[[CARD' in prompt
        assert 'SAVE_CHARACTER' in prompt

    def test_includes_recent_chapters(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        chapters = [
            {'title': '第一章 觉醒', 'word_count': 2400, 'order_index': 1},
            {'title': '第二章 入门', 'word_count': 2350, 'order_index': 2},
        ]
        prompt = build_chat_system_prompt(_FakeBook(), None, recent_chapters=chapters)
        assert '第一章 觉醒' in prompt
        assert '第二章 入门' in prompt
        assert '最近章节' in prompt

    def test_includes_filled_dims(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        bb = _FakeBB(worldbuilding='灵石体系：九品灵石', concept='少年崛起')
        prompt = build_chat_system_prompt(_FakeBook(), bb)
        assert '灵石体系' in prompt
        assert '少年崛起' in prompt
        assert '已完成维度' in prompt

    def test_new_book_hint(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        prompt = build_chat_system_prompt(_FakeBook(), None)
        assert '新书' in prompt


class TestChapterCardAndSession:
    """章节正文卡 + 会话消息持久化相关纯逻辑测试。"""

    def test_chapter_card_in_registry(self):
        from blueprints.chat_collab_bp import CARD_REGISTRY
        assert 'SAVE_CHAPTER' in CARD_REGISTRY
        assert CARD_REGISTRY['SAVE_CHAPTER']['mode'] == 'chapter'
        assert CARD_REGISTRY['SAVE_CHAPTER']['label'] == '章节正文'

    def test_chapter_card_parsed(self):
        from blueprints.chat_collab_bp import parse_cards
        text = '[[CARD:SAVE_CHAPTER|第一章 觉醒|林天睁开眼，发现自己身处……]]'
        cards = parse_cards(text)
        assert len(cards) == 1
        assert cards[0]['type'] == 'SAVE_CHAPTER'
        assert cards[0]['title'] == '第一章 觉醒'
        assert '林天' in cards[0]['content']

    def test_chapter_card_instructions_in_prompt(self):
        from blueprints.chat_collab_bp import build_chat_system_prompt
        prompt = build_chat_system_prompt(_FakeBook(), None)
        assert 'SAVE_CHAPTER' in prompt
        assert '章节正文' in prompt

    def test_persisted_cards_structure(self):
        """模拟 generate() 持久化的 cards 结构，确保含 status 字段。"""
        from blueprints.chat_collab_bp import parse_cards
        text = '讨论\n[[CARD:SAVE_CHAPTER|第一章|正文内容]]'
        cards = parse_cards(text)
        persisted = [{'id': c['id'], 'type': c['type'], 'title': c['title'],
                      'content': c['content'], 'target': c['target'],
                      'status': 'pending'} for c in cards]
        assert len(persisted) == 1
        assert persisted[0]['status'] == 'pending'
        assert persisted[0]['type'] == 'SAVE_CHAPTER'

    def test_history_load_preserves_cards(self):
        """历史会话加载时，assistant 消息的 cards 字段应保留。"""
        import json
        from blueprints.chat_collab_bp import load_session_messages

        class _FakeSession:
            messages_json = json.dumps([
                {'role': 'user', 'content': '写一章'},
                {'role': 'assistant', 'content': '好的',
                 'cards': [{'id': 'abc', 'type': 'SAVE_CHAPTER', 'title': '第一章',
                            'content': '正文', 'target': '章节正文', 'status': 'pending'}]},
            ], ensure_ascii=False)
        msgs = load_session_messages(_FakeSession())
        assert len(msgs) == 2
        assert msgs[1]['role'] == 'assistant'
        assert 'cards' in msgs[1]
        assert len(msgs[1]['cards']) == 1
        assert msgs[1]['cards'][0]['type'] == 'SAVE_CHAPTER'


class TestGwStreamWithHb:
    """sse_keepalive.gw_stream_with_hb 心跳保活回归：思考型模型推理期不能被 30s idle 掐断。"""

    def test_heartbeat_during_slow_llm(self, monkeypatch):
        import time
        import sse_keepalive
        from sse_keepalive import gw_stream_with_hb, SSE_HEARTBEAT_COMMENT

        class _SlowGW:
            def chat_stream(self, msgs, **kw):
                time.sleep(0.15)  # 模拟思考型模型在首字节前的长阻塞（无任何输出）
                yield "正文A"
                yield "正文B"

        monkeypatch.setattr(sse_keepalive, "SSE_HB_INTERVAL_SEC", 0.05)
        out = list(gw_stream_with_hb(_SlowGW(), []))

        # 正文完整透传，且首字节前至少发过 1 帧心跳（防 Render 30s 掐断）
        assert "正文A" in out and "正文B" in out
        assert out[-1] == "正文B"
        assert SSE_HEARTBEAT_COMMENT in out
        assert out.index(SSE_HEARTBEAT_COMMENT) < out.index("正文A")

    def test_normal_chunks_pass_through(self, monkeypatch):
        import sse_keepalive
        from sse_keepalive import gw_stream_with_hb

        class _FastGW:
            def chat_stream(self, msgs, **kw):
                yield "一"
                yield "二"

        monkeypatch.setattr(sse_keepalive, "SSE_HB_INTERVAL_SEC", 5)
        out = list(gw_stream_with_hb(_FastGW(), []))
        assert out == ["一", "二"]

    def test_worker_exception_propagates(self, monkeypatch):
        import sse_keepalive
        from sse_keepalive import gw_stream_with_hb

        class _FailGW:
            def chat_stream(self, msgs, **kw):
                raise RuntimeError("LLM 炸了")
                yield "永远不会到"  # pragma: no cover

        monkeypatch.setattr(sse_keepalive, "SSE_HB_INTERVAL_SEC", 5)
        import pytest
        with pytest.raises(RuntimeError, match="LLM 炸了"):
            list(gw_stream_with_hb(_FailGW(), []))
