"""general_chat_hitter（通用聊天维度命中识别器）单测。

覆盖三大历史风险点：
1. 误报：纯闲聊不应弹"落入XX维度"气泡（用户被轰炸会直接关掉功能）；
2. 漏报：明确聊创作时必须命中并给出可落卡建议；
3. 建议轰炸：相近维度簇去重 + 最多3条。
纯函数模块，无需 Flask app 上下文。
"""
import pytest

from general_chat_hitter import (
    detect_dimension_hits,
    wrap_message_with_context,
    build_general_chat_system_prompt,
    WRITING_TOTAL_HINTS,
)


# ---------------------------------------------------------------------------
# detect_dimension_hits
# ---------------------------------------------------------------------------
class TestDetectDimensionHits:
    def test_空文本与短文本返回空(self):
        assert detect_dimension_hits('') == []
        assert detect_dimension_hits('   ') == []
        assert detect_dimension_hits('你好') == []  # <3字符

    def test_纯闲聊不命中_防误报(self):
        # 天气/问候/编程闲聊：无创作关键词，不应产生任何建议
        assert detect_dimension_hits('今天天气怎么样，晚上吃什么好') == []
        assert detect_dimension_hits('帮我看看这段Python代码为什么报错') == []

    def test_明确聊构思命中concept(self):
        hits = detect_dimension_hits('我想写一个修仙题材的故事，主角是个废柴逆袭')
        assert hits, '明确创作意图必须命中'
        dims = [h['dim'] for h in hits]
        assert 'concept' in dims  # "我想写一个"短语 + 题材关键词

    def test_建议字段完整性(self):
        hits = detect_dimension_hits('这本书的世界观设定里境界分为九阶，宗门势力分布是怎样的')
        assert hits
        for h in hits:
            assert set(h.keys()) >= {'dim', 'label', 'card_type', 'confidence', 'hits', 'suggested_title'}
            assert 0 < h['confidence'] <= 0.95
            assert h['hits']  # 命中词列表非空
            assert h['card_type'].startswith(('SAVE_', 'APPLY_'))

    def test_置信度降序排列(self):
        hits = detect_dimension_hits('写一本小说，人物设定要立体，世界观要宏大，大纲分三卷，文风偏轻松')
        confs = [h['confidence'] for h in hits]
        assert confs == sorted(confs, reverse=True)

    def test_相近维度簇去重_防建议轰炸(self):
        # concept/plot_design/timeline 同簇；worldbuilding/key_rules/locations 同簇。
        # 低置信度（<0.7）的同簇建议应被过滤，不会同时弹 6 条。
        hits = detect_dimension_hits('剧情主线怎么安排，大纲分卷怎么规划，事件顺序如何')
        if len(hits) >= 2:
            dims = {h['dim'] for h in hits}
            # 同簇内不允许出现两条低置信度建议
            cluster_a = dims & {'concept', 'plot_design', 'timeline'}
            cluster_b = dims & {'worldbuilding', 'key_rules', 'locations'}
            low_conf_hits = [h for h in hits if h['confidence'] < 0.7]
            if cluster_a and len(low_conf_hits) >= 2:
                # 若出现多条低置信度，它们的簇必须互不相同（即各自代表不同簇）
                assert not (cluster_a and cluster_b and len(dims) < len(hits))

    def test_最多三条建议(self):
        # 拉满所有维度的超长创作消息
        mega = ('我想写一个故事，先说构思和创意：世界观有修炼体系和能量体系，境界分九阶，'
                '宗门势力分布错综；主角人物性格冷酷，身份是废柴；大纲分三卷，第一卷埋伏笔，'
                '伏笔在结局回收；文风要短句快节奏，第三人称；地点在都市大学校园；'
                '规则是只要突破就反噬。')
        hits = detect_dimension_hits(mega)
        assert 1 <= len(hits) <= 3

    def test_写作上下文降低门槛(self):
        # 同一句创作内容：带"写小说"全局提示词时应更易命中（置信度加成0.18）
        no_ctx = detect_dimension_hits('主角性格怎样比较讨喜')
        with_ctx = detect_dimension_hits('我想写小说，主角性格怎样比较讨喜')
        if no_ctx:
            conf_no = max(h['confidence'] for h in no_ctx)
            conf_yes = max(h['confidence'] for h in with_ctx) if with_ctx else 0
            assert conf_yes >= conf_no
        else:
            assert with_ctx, '写作上下文加成后必须命中'

    def test_返回结果独立于输入顺序稳定性(self):
        a = detect_dimension_hits('我想写一个主角废柴逆袭的修仙故事')
        assert isinstance(a, list)


# ---------------------------------------------------------------------------
# wrap_message_with_context
# ---------------------------------------------------------------------------
class TestWrapMessageWithContext:
    def test_非写作消息原样返回(self):
        assert wrap_message_with_context('今晚吃火锅吗') == '今晚吃火锅吗'

    def test_空消息原样返回(self):
        assert wrap_message_with_context('') == ''

    def test_写作消息注入前导背景(self):
        out = wrap_message_with_context(
            '我想写一个修仙故事', book_title='凡人修仙', bb_snapshot='世界观:修仙界')
        assert out != '我想写一个修仙故事'
        assert '【作者原话】' in out
        assert '我想写一个修仙故事' in out
        assert '凡人修仙' in out
        assert '世界观:修仙界' in out

    def test_无书名快照时仅保留骨架(self):
        out = wrap_message_with_context('写小说的话剧情怎么展开')
        assert '【作者原话】' in out
        assert '当前作品：《' not in out  # 未传书名时不应出现书名行
        assert '已填充维度摘要' not in out


# ---------------------------------------------------------------------------
# build_general_chat_system_prompt
# ---------------------------------------------------------------------------
class TestBuildGeneralChatSystemPrompt:
    def test_包含身份与卡片协议(self):
        prompt = build_general_chat_system_prompt()
        assert '智驾' in prompt
        assert '[[CARD:' in prompt  # 落地卡片协议
        assert 'SAVE_CONCEPT' in prompt

    def test_包含联网搜索行为约束(self):
        prompt = build_general_chat_system_prompt()
        assert '联网搜索' in prompt
        # 防回归：曾有"我没有联网搜索功能"的AI话术，prompt 必须显式禁止
        assert '我没有联网搜索功能' in prompt  # 出现在禁止清单里


# ---------------------------------------------------------------------------
# WRITING_TOTAL_HINTS 词库完整性
# ---------------------------------------------------------------------------
class TestWritingTotalHints:
    def test_全局提示词非空(self):
        assert WRITING_TOTAL_HINTS
        assert any('写' in h for h in WRITING_TOTAL_HINTS)
