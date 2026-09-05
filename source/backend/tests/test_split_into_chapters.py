"""split_into_chapters 章节拆分器单测（blueprints/export_bp.py）。

覆盖历史高发的拆分边界：
1. 五种章节标题格式（中文/Markdown/英文/纯数字/括号）
2. 同章双标题（第20章 / 第二十章 紧挨出现）的空章合并
3. 无章节标记时的整体兜底
纯函数，直接函数级调用（无需 Flask 上下文）。
"""
import pytest

from blueprints.export_bp import split_into_chapters


class TestChineseChapterTitles:
    def test_中文数字章节(self):
        text = "第一章 开始\n内容一\n\n第二章 继续\n内容二"
        chs = split_into_chapters(text)
        assert [c['title'] for c in chs] == ['第一章 开始', '第二章 继续']
        assert chs[0]['content'] == '内容一'
        assert chs[1]['content'] == '内容二'

    def test_阿拉伯数字章节(self):
        text = "第1章 风起\nA\n第2章 云涌\nB"
        chs = split_into_chapters(text)
        assert len(chs) == 2
        assert chs[1]['title'] == '第2章 云涌'

    def test_三位数补零(self):
        text = "第001章 序\nx\n第002章 承\ny"
        chs = split_into_chapters(text)
        assert len(chs) == 2


class TestOtherFormats:
    def test_markdown标题(self):
        text = "# 开篇\n甲\n## 第二篇\n乙"
        chs = split_into_chapters(text)
        assert len(chs) == 2
        assert chs[0]['title'] == '开篇'  # strip_prefix='#' 生效

    def test_英文Chapter(self):
        text = "Chapter 1 Begin\nA\nChapter 2 End\nB"
        chs = split_into_chapters(text)
        assert len(chs) == 2

    def test_纯数字开头(self):
        text = "1、启程\nA\n2、遇险\nB"
        chs = split_into_chapters(text)
        assert len(chs) == 2
        assert chs[0]['title'] == '1、启程'

    def test_括号标题(self):
        text = "【第一幕】开端\nA\n【第二幕】转折\nB"
        chs = split_into_chapters(text)
        assert len(chs) == 2

    def test_无章节标记返回None(self):
        assert split_into_chapters('就是一整段没有标记的文本内容而已') is None


class TestMergeEmptyChapters:
    def test_同章双标题_阿拉伯与中文重复(self):
        # 历史事故：OCR/双格式导出产出「第20章」+「第二十章」紧挨，前者空内容
        text = "第19章 有内容\n上一章内容\n\n第20章\n\n第二十章 空章标题合并\n本章正文"
        chs = split_into_chapters(text)
        # 第20章（空）与第二十章（同号有内容）合并为一条：19章 + 合并章 = 2 章
        assert len(chs) == 2
        assert chs[1]['title'] == '第二十章 空章标题合并'  # 保留有内容章的标题
        assert chs[1]['content'] == '本章正文'
        # 不存在内容为空的章
        assert all(c['content'].strip() for c in chs)

    def test_空章后接有内容章_标题拼接副标题(self):
        text = "第一章\n\n第二章 实际内容\n正文"
        chs = split_into_chapters(text)
        # 第一章空 → 并入下一章，标题带副标题
        assert len(chs) == 1
        assert '第一章' in chs[0]['title']
        assert chs[0]['content'] == '正文'

    def test_全空章兜底保留第一章(self):
        text = "第一章\n\n第二章\n"
        chs = split_into_chapters(text)
        assert len(chs) >= 1  # 兜底至少保留 1 章，不返回空列表


class TestContentIntegrity:
    def test_标题超长截断到100字符(self):
        long_title = '第1章 ' + '很' * 200
        text = f"{long_title}\n内容"
        chs = split_into_chapters(text)
        assert len(chs[0]['title']) <= 100

    def test_内容不丢失(self):
        text = "第一章 A\n中间正文1\n\n第二章 B\n中间正文2\n\n第三章 C\n中间正文3"
        chs = split_into_chapters(text)
        joined = ''.join(c['content'] for c in chs)
        for i in (1, 2, 3):
            assert f'中间正文{i}' in joined
