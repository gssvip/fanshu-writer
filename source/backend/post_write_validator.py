"""
确定性后写校验器（P0-1）
纯 regex/统计检查，零 LLM 成本。章节生成后调用，检测 AI 痕迹和文本质量问题。

参考：Openwrite post_validator.py + InkOS post-write-validator
设计原则：
  - 只做 warning 提示，不阻断章节入库
  - critical 级问题才建议作者修订
  - 前端可展示报告，作者可选择"一键修订"
"""
import re
import os
import yaml
from typing import List, Dict, Any
from dataclasses import dataclass, field, asdict

# 配置文件路径
_PATTERNS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_patterns.yaml')
_patterns_cache: Dict[str, Any] = {}


def _load_patterns() -> Dict[str, Any]:
    """加载 AI 痕迹词库配置（带缓存）"""
    global _patterns_cache
    if _patterns_cache:
        return _patterns_cache
    try:
        with open(_PATTERNS_PATH, 'r', encoding='utf-8') as f:
            _patterns_cache = yaml.safe_load(f) or {}
    except Exception:
        # 配置加载失败时用最小默认值，保证不阻断
        _patterns_cache = {
            'forbidden_patterns': [],
            'fatigue_words': ['突然', '忽然', '猛地', '仿佛', '不禁', '竟然'],
            'fatigue_word_max_per_chapter': 2,
            'transition_word_max_per_3000': 1,
            'paragraph_max_chars': 300,
            'short_paragraph_max_ratio': 0.6,
            'short_paragraph_threshold': 30,
            'continuous_le_max': 5,
        }
    return _patterns_cache


@dataclass
class ValidationIssue:
    """单条校验问题"""
    severity: str            # critical / warning / info
    category: str            # 问题类别描述
    pattern: str             # 命中的模式/词
    count: int = 1           # 命中次数
    position: str = ''       # 位置描述
    suggestion: str = ''     # 修订建议


@dataclass
class ValidationResult:
    """校验结果"""
    passed: bool = True  # 无 critical 即 passed
    score: int = 100     # 0-100，初始满分，按问题扣分
    issues: List[ValidationIssue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add(self, issue: ValidationIssue):
        self.issues.append(issue)
        if issue.severity == 'critical':
            self.passed = False
            self.score = max(0, self.score - 20)
        elif issue.severity == 'warning':
            self.score = max(0, self.score - 5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'passed': self.passed,
            'score': self.score,
            'issue_count': len(self.issues),
            'critical_count': sum(1 for i in self.issues if i.severity == 'critical'),
            'warning_count': sum(1 for i in self.issues if i.severity == 'warning'),
            'issues': [asdict(i) for i in self.issues],
            'stats': self.stats,
        }


def validate_chapter(content: str) -> ValidationResult:
    """校验章节正文，返回结果。主入口。"""
    result = ValidationResult()
    if not content or not content.strip():
        result.stats['char_count'] = 0
        return result

    cfg = _load_patterns()
    text = content.strip()
    char_count = len(text)
    result.stats['char_count'] = char_count

    # 1. 禁止句式检查（critical）
    _check_forbidden_patterns(text, cfg, result)

    # 2. 高疲劳词密度检查（warning）
    _check_fatigue_words(text, cfg, result)

    # 3. 段落结构检查（warning）
    _check_paragraph_structure(text, cfg, result)

    # 4. 连续「了」字检查（warning）
    _check_continuous_le(text, cfg, result)

    # 5. 转折词密度检查（warning）
    _check_transition_density(text, cfg, result)

    return result


def _check_forbidden_patterns(text: str, cfg: Dict, result: ValidationResult):
    """禁止句式检查（critical 级）"""
    for item in cfg.get('forbidden_patterns', []):
        pattern = item.get('pattern', '')
        reason = item.get('reason', '')
        if not pattern:
            continue
        try:
            matches = re.findall(pattern, text)
            if matches:
                result.add(ValidationIssue(
                    severity='critical',
                    category='禁止句式',
                    pattern=pattern,
                    count=len(matches),
                    position=f'共 {len(matches)} 处',
                    suggestion=reason,
                ))
        except re.error:
            continue  # 正则编译失败跳过


def _check_fatigue_words(text: str, cfg: Dict, result: ValidationResult):
    """高疲劳词密度检查（warning 级）"""
    max_per_chapter = cfg.get('fatigue_word_max_per_chapter', 2)
    for word in cfg.get('fatigue_words', []):
        count = text.count(word)
        if count > max_per_chapter:
            result.add(ValidationIssue(
                severity='warning',
                category='高疲劳词',
                pattern=word,
                count=count,
                position=f'全章出现 {count} 次',
                suggestion=f'「{word}」出现过多（{count}次，建议≤{max_per_chapter}次），替换部分表达',
            ))


def _check_paragraph_structure(text: str, cfg: Dict, result: ValidationResult):
    """段落结构检查"""
    max_chars = cfg.get('paragraph_max_chars', 300)
    short_threshold = cfg.get('short_paragraph_threshold', 30)
    short_max_ratio = cfg.get('short_paragraph_max_ratio', 0.6)

    # 按空行分段
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        return

    result.stats['paragraph_count'] = len(paragraphs)

    # 过长段落
    for i, p in enumerate(paragraphs):
        if len(p) > max_chars:
            result.add(ValidationIssue(
                severity='warning',
                category='段落过长',
                pattern=f'第{i+1}段',
                count=1,
                position=f'第{i+1}段（{len(p)}字）',
                suggestion=f'该段 {len(p)} 字超过 {max_chars} 字上限，建议拆分',
            ))

    # 短段占比
    short_count = sum(1 for p in paragraphs if len(p) < short_threshold)
    short_ratio = short_count / len(paragraphs) if paragraphs else 0
    result.stats['short_paragraph_ratio'] = round(short_ratio, 2)
    if short_ratio > short_max_ratio:
        result.add(ValidationIssue(
            severity='warning',
            category='短段堆砌',
            pattern='短段占比',
            count=short_count,
            position=f'{short_count}/{len(paragraphs)} 段为短段',
            suggestion=f'短段占比 {short_ratio:.0%} 过高（>{short_max_ratio:.0%}），AI 常用短段堆砌，建议合并部分段落',
        ))


def _check_continuous_le(text: str, cfg: Dict, result: ValidationResult):
    """连续「了」字检查"""
    max_le = cfg.get('continuous_le_max', 5)
    # 匹配连续 6 个及以上「了」
    pattern = r'了{' + str(max_le + 1) + r',}'
    matches = re.findall(pattern, text)
    if matches:
        result.add(ValidationIssue(
            severity='warning',
            category='连续了字',
            pattern='了' * (max_le + 1),
            count=len(matches),
            position=f'共 {len(matches)} 处连续{max_le+1}个以上「了」',
            suggestion=f'连续「了」字过多（≥{max_le+1}），减少重复助词',
        ))


def _check_transition_density(text: str, cfg: Dict, result: ValidationResult):
    """转折类词密度检查（每 3000 字上限）"""
    max_per_3000 = cfg.get('transition_word_max_per_3000', 1)
    transition_words = ['突然', '忽然', '猛地', '骤然', '旋即', '顿时', '倏地']
    total_count = sum(text.count(w) for w in transition_words)
    char_count = len(text)
    # 计算每 3000 字密度
    density = total_count / (char_count / 3000) if char_count > 0 else 0
    result.stats['transition_density'] = round(density, 2)
    if density > max_per_3000:
        result.add(ValidationIssue(
            severity='warning',
            category='转折密度',
            pattern='转折词总和',
            count=total_count,
            position=f'每 3000 字 {density:.1f} 次',
            suggestion=f'转折词密度过高（{density:.1f}次/3000字，建议≤{max_per_3000}），AI 常滥用转折',
        ))


def get_repair_hints(result: ValidationResult) -> List[Dict[str, str]]:
    """从校验结果提取修订提示（供 Spot-Fix 用）"""
    hints = []
    for issue in result.issues:
        if issue.severity == 'critical':
            hints.append({
                'target': issue.pattern,
                'suggestion': issue.suggestion,
                'scope': 'local',  # critical 句式通常是局部问题
            })
    return hints
