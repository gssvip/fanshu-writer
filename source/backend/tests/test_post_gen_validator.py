"""post_gen_validator 缺节自动补写（增强）单元测试。

覆盖：
  - sections_missing_issues：只筛 warn 级 *_SECTIONS_MISSING
  - build_sections_retry_hint：生成含缺失分节清单的补写提示
  - validate → hint 端到端：设定内容够长但分节命中 5/11 → warn（不 error）→ 可生成补写提示
  - chat_collab_bp._dim_max_tokens：按维度给足 token（防 finish_reason=length 截断）
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from blueprints.post_gen_validator import PostGenValidator, ValidationIssue


def _v():
    return PostGenValidator(3, 50, max_retries=1)


class TestSectionsMissingIssues:
    def test_filters_warn_level_sections_issues_only(self):
        issues = [
            ValidationIssue('KEY_RULES_TOO_SHORT', 'error', '过短'),
            ValidationIssue('KEY_RULES_SECTIONS_MISSING', 'warn', '缺节'),
            ValidationIssue('STYLE_VAGUE', 'warn', '太虚'),
        ]
        got = PostGenValidator.sections_missing_issues(issues)
        assert len(got) == 1
        assert got[0].code == 'KEY_RULES_SECTIONS_MISSING'

    def test_empty_when_no_sections_issue(self):
        issues = [ValidationIssue('KEY_RULES_TOO_SHORT', 'error', '过短')]
        assert PostGenValidator.sections_missing_issues(issues) == []

    def test_realistic_validate_hit_5_of_11_is_warn(self):
        """线上场景：设定 ≥1500 字但只命中 5/11 节（token 截断/跳节）→ warn 级缺节。"""
        v = _v()
        # 只覆盖力量体系/等级/提升路径/技能/装备 5 个关键词，其余 6 节缺失
        content = ('力量总体系：\n' + '灵气充盈大地。' * 60 + '\n等级阶梯：\n' + '从炼气到金丹。' * 60
                   + '\n提升路径：\n' + '打坐吸收灵气。' * 60 + '\n技能树：\n' + '剑修有剑意。' * 60
                   + '\n装备：\n' + '法宝分九品。' * 60)
        assert len(content) >= 1500
        issues = v.validate('key_rules', content)
        secs = PostGenValidator.sections_missing_issues(issues)
        assert len(secs) == 1
        # warn 级：不触发 error 重试（should_retry 为 False），但可触发缺节补写
        assert not v.should_retry(issues)
        assert secs[0].severity == 'warn'


class TestBuildSectionsRetryHint:
    def test_hint_contains_missing_keywords_and_keep_rule(self):
        v = _v()
        issues = [ValidationIssue(
            'KEY_RULES_SECTIONS_MISSING', 'warn',
            '【设定】缺少明显的分节细项：命中 5/11 个。疑似缺项关键词：资源与货币、副职业',
            auto_fix='按【设定】分节铁律依次输出每一节。',
        )]
        hint = v.build_sections_retry_hint(issues)
        assert '资源与货币' in hint
        assert '副职业' in hint
        assert '保留上一版已写好的分节内容' in hint

    def test_hint_empty_without_sections_issues(self):
        v = _v()
        assert v.build_sections_retry_hint([]) == ''
        assert v.build_sections_retry_hint(
            [ValidationIssue('KEY_RULES_TOO_SHORT', 'error', '过短')]) == ''

    def test_error_hint_takes_priority_over_sections_hint(self):
        """error 级重试提示优先于缺节补写提示（调用方用 or 串联）。"""
        v = _v()
        issues = [
            ValidationIssue('KEY_RULES_TOO_SHORT', 'error', '过短'),
            ValidationIssue('KEY_RULES_SECTIONS_MISSING', 'warn', '缺节'),
        ]
        hint = v.build_retry_hint(issues) or v.build_sections_retry_hint(issues)
        assert '重新生成' in hint
        assert '补全' not in hint


class TestDimMaxTokens:
    def test_per_dimension_quotas(self):
        """配额统一给足 _DIM_MAX_TOKENS（按模型能力防任何维度截断），不再分档。"""
        from blueprints.chat_collab_bp import _dim_max_tokens
        assert _dim_max_tokens('timeline') == 131072
        assert _dim_max_tokens('worldbuilding') == 131072
        assert _dim_max_tokens('character_profiles') == 131072
        assert _dim_max_tokens('key_rules') == 131072
        assert _dim_max_tokens('concept') == 131072

    def test_default_for_unknown_dim(self):
        from blueprints.chat_collab_bp import _dim_max_tokens
        assert _dim_max_tokens('style_guide') == 131072
        assert _dim_max_tokens('unknown_dim') == 131072

    def test_quota_covers_validator_min_chars(self):
        """配额必须 ≥ 校验器字数下限（中文 ≈1.0-1.5 token/字，取 2.5x 安全系数防截断）。"""
        from blueprints.chat_collab_bp import _dim_max_tokens
        # key_rules 下限 1500 字 → 6000 token；worldbuilding 下限 2000 字 → 8000 token
        assert _dim_max_tokens('key_rules') >= 1500 * 2.5
        assert _dim_max_tokens('worldbuilding') >= 2000 * 2.5
        assert _dim_max_tokens('concept') >= 1200 * 2.5
