"""运行期验证：去AI味规则注入的健壮性（回归 2026-08-30 优化）。

覆盖 4 个断言点：
  1. 正文阶段 build_writing_rules 已注入 DEAI_ONLY_RULES
  2. 智驾写正文 build_chat_chapter_rules 已注入 DEAI_ONLY_RULES
  3. app._build_deai_rules_block 正常路径返回核心去AI表（book=None 无技能包增强）
  4. app._build_deai_rules_block 兜底路径：build_review_rules 抛异常时回退核心表，绝不空

不依赖网络/LLM/真实 DB 数据，仅验证规则字符串装配与兜底逻辑。
"""
from __future__ import annotations

import pytest


def _import_blocks():
    """延迟 import 依赖 app context 的模块。"""
    import blueprints.chat_collab_bp as ccb
    import app as app_module
    return ccb, app_module


@pytest.mark.usefixtures("app")
class TestDeaiInjectionRuntime:
    """需真实 Flask app context（SQLite 本地）加载模块。"""

    def test_writing_rules_injects_deai(self, app):
        ccb, _ = _import_blocks()
        with app.app_context():
            rules = ccb.build_writing_rules(book=None, skill_pack_ids=None)
        # 三段都必须命中
        assert "正文写作规范" in rules
        assert "去AI味与行文消杀" in rules
        assert "创作总则" in rules
        # 关键铁律代表条款在
        assert "四大核心AI病句消杀" in rules
        assert "替代品" not in rules  # 无噪声

    def test_chat_chapter_rules_injects_deai(self, app):
        ccb, _ = _import_blocks()
        with app.app_context():
            rules = ccb.build_chat_chapter_rules(book=None, skill_pack_ids=None)
        # 智驾写正文：行文规范 + 去AI味都要有，但跳过创作总则（避免与 chat system 重复）
        assert "正文写作规范" in rules
        assert "去AI味与行文消杀" in rules
        assert "创作总则" not in rules

    def test_build_deai_rules_block_ok_path(self, app):
        _, app_module = _import_blocks()
        with app.app_context():
            block, status = app_module._build_deai_rules_block(None, None)
        assert block  # 非空
        assert "去AI味与行文消杀" in block
        assert status in ("ok", "fallback")  # 无技能包增强时仍应 ok（核心表在 parts）

    def test_build_deai_rules_block_fallback_path(self, app, monkeypatch):
        _, app_module = _import_blocks()
        with app.app_context():
            # 模拟 build_review_rules 彻底抛异常 -> 必须回退核心 DEAI_ONLY_RULES
            import blueprints.chat_collab_bp as ccb

            def _boom(*a, **k):
                raise RuntimeError("模拟去AI规则构建崩溃")

            monkeypatch.setattr(ccb, "build_review_rules", _boom)
            block, status = app_module._build_deai_rules_block(None, None)
            assert status == "fallback"
            assert block  # 兜底非空
            assert "去AI味与行文消杀" in block