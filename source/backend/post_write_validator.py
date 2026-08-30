"""
确定性后写校验器（P0-1）
纯 regex/统计检查，零 LLM 成本。章节生成后调用，检测 AI 痕迹和文本质量问题。

参考：Openwrite post_validator.py + InkOS post-write-validator
设计原则：
  - 只做 warning 提示，不阻断章节入库
  - critical 级问题才建议作者修订
  - 前端可展示报告，作者可选择"一键修订"

P2 扩展：validate_chapter_with_bible 增加"确定性硬伤"校验
  - 死亡角色复活检测（critical）：chapter_changes_log 中已死亡角色在本章说话/行动
  - 境界回退检测（critical）：角色已记录境界，本章出现明显更低的境界
  - 角色名一致性的近似错写检测（warning）：误报率高，仅做提示
"""
import re
import os
import json
import yaml
from typing import List, Dict, Any, Optional
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
            'paragraph_max_chars': 100,
            # 段落结构：真人金标准校准阈值
            'long_paragraph_threshold': 100,
            'long_paragraph_max_ratio': 0.03,      # >100字 占比>3% → red
            'light_long_threshold': 70,
            'light_long_max_ratio': 0.05,          # >70字 占比>5% → yellow
            'ultra_short_threshold': 10,
            'ultra_short_max_ratio': 0.60,         # ≤10字 占比>60% → red
            'main_zone_low': 11,
            'main_zone_high': 35,
            'main_zone_min_ratio': 0.30,           # 11-35字 占比<30% → red
            'cv_min_healthy': 0.30,
            'cv_max_healthy': 1.60,                # CV<0.30 或 CV>1.60 → red
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

    # 4. 连续“了”字检查（warning）
    _check_continuous_le(text, cfg, result)

    # 5. 转折词密度检查（warning）
    _check_transition_density(text, cfg, result)

    # 6-11. AI 痕迹扩展检测（审校评分制配套，均为 warning）
    _check_smooth_feeling(text, result)
    _check_transition_cliche(text, result)
    _check_perfect_cliche(text, result)
    _check_long_sentence(text, result)
    _check_repetition(text, result)
    _check_monotone_syntax(text, result)

    # 12. 张力评分（借鉴 PlotPilot 张力心电图，量化叙事节奏）
    _check_tension_score(text, result)

    # 13. 标准文风禁词扫描（critical，对接 STANDARD_WRITING_STYLE_PROMPT 禁词清单）
    _check_style_forbidden_words(text, result)

    # 14. 上帝视角/剧透式叙述/伏笔明写检测（critical，对接视角与信息控制铁律）
    _check_god_view_and_foreshadow_leak(text, result)

    # 15. 风格对齐度 12 维评分（B1·文风对齐包配套）
    #     输出 stats.style_alignment = {维度key: {name, score 0-100, note}} 字典；
    #     若有维度 <60 分则追加 warning，给具体改法，不扣现有总分（独立维度）。
    _check_style_alignment_score(text, cfg, result)

    # 16. 硬卡4·整章量化双轨自检（每自然段≤2句号，一般以每自然段1个句号为主 / 段均字数比例 / 句均字数 / 段均句数）
    #     对接 app.py 文风铁律硬卡4；任何一条严重违规则升级为 critical（作者必须修订）。
    _check_quantitative_hardcards(text, cfg, result)

    # 17. Humanizer·去AI痕迹铁律5.2/5.3/5.4 专项检测（虽然但是/不仅而且/列举腔/连续了/被字句/X地副词）
    _check_humanizer_patterns(text, result)

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
                suggestion=f'“{word}”出现过多（{count}次，建议≤{max_per_chapter}次），替换部分表达',
            ))


def _check_paragraph_structure(text: str, cfg: Dict, result: ValidationResult):
    """段落结构检查（基于三份真人样本·3601段金标准校准的5项限制）
    真人金标准参考：均长23.7字，CV=0.67，极短句≤10字占23.5%，
      主力11-35字占54.8%，>70字占0.9%，>100字占0.1%。
    告警线=真人值的3~5倍放宽，且分级：critical=明显矿道病AI味，warning=轻度偏差。
    """
    import math

    max_chars = cfg.get('paragraph_max_chars', 100)

    # 5 项阈值（读配置，缺省=用户确认值）
    heavy_thr     = cfg.get('long_paragraph_threshold', 100)
    heavy_max_r   = cfg.get('long_paragraph_max_ratio', 0.03)   # >100字 >3% → critical
    light_thr     = cfg.get('light_long_threshold', 70)
    light_max_r   = cfg.get('light_long_max_ratio', 0.08)       # >70字  >8% → warning
    ushort_thr    = cfg.get('ultra_short_threshold', 10)
    ushort_max_r  = cfg.get('ultra_short_max_ratio', 0.60)      # ≤10字 >60% → critical
    main_lo       = cfg.get('main_zone_low', 11)
    main_hi       = cfg.get('main_zone_high', 35)
    main_min_r    = cfg.get('main_zone_min_ratio', 0.30)        # 11-35 <30% → critical
    cv_min        = cfg.get('cv_min_healthy', 0.30)
    cv_max        = cfg.get('cv_max_healthy', 1.60)             # CV 越界 → critical

    # 按空行分段
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        return
    total = len(paragraphs)
    lens = [len(p) for p in paragraphs]

    result.stats['paragraph_count'] = total

    # --- 统计各维度 ---
    heavy_cnt  = sum(1 for l in lens if l > heavy_thr)      # >100
    light_cnt  = sum(1 for l in lens if light_thr < l <= heavy_thr)  # 71-100
    light_total = heavy_cnt + light_cnt                    # >70 总体
    ushort_cnt = sum(1 for l in lens if l <= ushort_thr)   # ≤10
    main_cnt   = sum(1 for l in lens if main_lo <= l <= main_hi)  # 11-35

    heavy_r  = heavy_cnt / total
    light_r  = light_total / total
    ushort_r = ushort_cnt / total
    main_r   = main_cnt / total

    # 变异系数 CV
    if total >= 4:
        mean = sum(lens) / total
        var = sum((x - mean) ** 2 for x in lens) / total
        std = math.sqrt(var)
        cv = (std / mean) if mean > 0 else 0
    else:
        cv = 0.67  # 段太少不评，取健康中值占位

    result.stats['para_stats'] = {
        'heavy_ratio':   round(heavy_r, 3),    # >100
        'light_ratio':   round(light_r, 3),    # >70
        'ushort_ratio':  round(ushort_r, 3),   # ≤10
        'main_ratio':    round(main_r, 3),     # 11-35
        'cv':            round(cv, 2),
        'avg_chars':     round(sum(lens)/total, 1) if total else 0,
        'total_count':   total,                # Q3 新增：段落数，前端可直接展示
    }
    avg_chars = sum(lens) / total if total else 0

    # Q3 新增：漫画分镜脚本化 warning（均长<18字 + 段数>120段 → yellow warning）
    # 真人：40-80段/章，均长23.7字，贺平生均长17.6但仅40-50段 → 双条件齐发才命中，不误伤
    COMIC_AVG_MAX = cfg.get('comic_script_avg_max', 18.0)
    COMIC_COUNT_MIN = cfg.get('comic_script_count_min', 120)
    if avg_chars < COMIC_AVG_MAX and total >= COMIC_COUNT_MIN:
        result.add(ValidationIssue(
            severity='warning',
            category='漫画分镜脚本化',
            pattern='均长短+段落数过多',
            count=total,
            position=f'均长 {avg_chars:.1f} 字/段，共 {total} 段',
            suggestion=f'段均长 {avg_chars:.1f} 字 + {total} 段 = 典型漫画分镜脚本化（真人章节 40-80 段、均长 23.7 字）。合并相邻 2-4 句同场景、同 POV、同镜头的叙述/动作/对白为一段；不要把 1-2 字的动作、拟声词、对白每个都拆一段。',
        ))

    # (0) 单段硬上限：>max_chars 即点名（warning）——如果 max_chars=100，和 heavy_thr 一致
    for i, p in enumerate(paragraphs):
        if len(p) > max_chars:
            result.add(ValidationIssue(
                severity='warning',
                category='段落过长',
                pattern=f'第{i+1}段',
                count=1,
                position=f'第{i+1}段（{len(p)}字）',
                suggestion=f'该段 {len(p)} 字超过 {max_chars} 字硬上限（真人样本最长=110字），按对白/动作/环境切分',
            ))

    # (1) 臃肿段>100字 占比 >3% → critical
    if heavy_r > heavy_max_r and total >= 10:
        result.add(ValidationIssue(
            severity='critical',
            category='段落臃肿',
            pattern=f'>{heavy_thr}字臃肿段',
            count=heavy_cnt,
            position=f'{heavy_cnt}/{total} 段（占比 {heavy_r:.0%}）',
            suggestion=f'臃肿段占比 {heavy_r:.0%} 超限（≤{heavy_max_r:.0%}）。参考：真人写作中>{heavy_thr}字的段仅 0.1%。超过70字的说明段必须按对白/动作/环境拆成独立小段，冲突场景段长应<50字。',
        ))

    # (2) 轻臃肿段>70字 占比 >5% → warning（yellow级）
    if light_r > light_max_r and total >= 10:
        result.add(ValidationIssue(
            severity='warning',
            category='轻臃肿段过多',
            pattern=f'>{light_thr}字段',
            count=light_total,
            position=f'{light_total}/{total} 段（占比 {light_r:.0%}）',
            suggestion=f'>{light_thr}字段占比 {light_r:.0%} 偏高（≤{light_max_r:.0%}）。真人写作中>{light_thr}字仅占 0.9%。把"递进比较链长段+1-4字短句收尾"作为复合长段的唯一豁免，其余都切开。',
        ))

    # (3) 极短句≤10字 占比 >60% → critical
    if ushort_r > ushort_max_r and total >= 10:
        result.add(ValidationIssue(
            severity='critical',
            category='段落太碎',
            pattern=f'≤{ushort_thr}字极短句',
            count=ushort_cnt,
            position=f'{ushort_cnt}/{total} 段（占比 {ushort_r:.0%}）',
            suggestion=f'极短句占比 {ushort_r:.0%} 超限（≤{ushort_max_r:.0%}）。"他怒。她笑。风急。"这种1个字1段的节奏要穿插11-35字的叙述支撑段，不能整章全堆1字句——真人仅 23.5%，AI别写太碎。',
        ))

    # (4) 主力区间 11-35字 占比 <30% → critical（要么全长要么全碎，都异常）
    if main_r < main_min_r and total >= 10:
        result.add(ValidationIssue(
            severity='critical',
            category='主力段长偏离',
            pattern=f'{main_lo}-{main_hi}字主力段',
            count=main_cnt,
            position=f'{main_cnt}/{total} 段（占比 {main_r:.0%}）',
            suggestion=f'真人写作中 {main_lo}-{main_hi} 字的段落占 54.8%（核心主力区），本章仅 {main_r:.0%}（<{main_min_r:.0%}）。要么全臃肿长段，要么全碎短段，分布严重偏离。',
        ))

    # (5) 段长变异系数 CV 越界 → critical
    if total >= 10:
        if cv < cv_min:
            result.add(ValidationIssue(
                severity='critical',
                category='AI机械网格',
                pattern=f'段长变异系数 CV={cv:.2f}',
                count=0,
                position=f'CV={cv:.2f}（健康区间0.50-1.00，真人金标准=0.67）',
                suggestion=f'段长过于均匀（CV<{cv_min}）=典型AI网格病——段段 20±2 字机械排列，没有长短交替。解决：递进比较链写 70-100 字长段 + 1-4 字短句收尾穿插，把 CV 拉回 0.5-1.0。',
            ))
        elif cv > cv_max:
            result.add(ValidationIssue(
                severity='critical',
                category='段落节奏异常零碎',
                pattern=f'段长变异系数 CV={cv:.2f}',
                count=0,
                position=f'CV={cv:.2f}（健康区间0.50-1.00，真人金标准=0.67）',
                suggestion=f'段长忽短忽长过于零碎（CV>{cv_max}）。参考："对白→小动作→环境"稳定三拍交替，避免 2 字和 100 字的极端段连续出现。',
            ))


def _check_continuous_le(text: str, cfg: Dict, result: ValidationResult):
    """连续“了”字检查"""
    max_le = cfg.get('continuous_le_max', 5)
    # 匹配连续 6 个及以上“了”
    pattern = r'了{' + str(max_le + 1) + r',}'
    matches = re.findall(pattern, text)
    if matches:
        result.add(ValidationIssue(
            severity='warning',
            category='连续了字',
            pattern='了' * (max_le + 1),
            count=len(matches),
            position=f'共 {len(matches)} 处连续{max_le+1}个以上“了”',
            suggestion=f'连续“了”字过多（≥{max_le+1}），减少重复助词',
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


# ====================================================================
# AI 痕迹确定性检测扩展（审校评分制配套）
# 6 条新规则：顺滑感 / 工整过渡 / 完美套话 / 长句过长 / 重复表达 / 单一句式
# ====================================================================

# 顺滑副词 + 情绪/顿悟/决策词 组合（禁止情绪/顿悟/决策顺滑）
_SMOOTH_ADVERBS = ['瞬间', '立刻', '马上', '顿时', '旋即', '霎时', '一下子', '顷刻', '刹那', '转瞬', '迅即', '倏然']
_SMOOTH_TARGETS = ['顿悟', '释怀', '想通', '接受', '决定', '释然', '平复', '振作', '想明白', '看开', '放下', '振作起来', '冷静下来', '恢复平静']

# AI 工整过渡套话（杜绝工整过渡）
_TRANSITION_CLICHES = [
    '在这一刻', '在这一瞬间', '仿佛之间', '一切尽在不言中', '时间仿佛静止', '空气中弥漫着',
    '在这一刹那', '空气中凝固', '时间停止', '世界安静', '万物归寂', '仿佛整个世界',
]

# 完美套话词（杜绝完美/标准套话）
_PERFECT_CLICHES = ['完美', '无懈可击', '恰到好处', '天衣无缝', '浑然天成', '相得益彰', '美轮美奂', '尽善尽美']


def _check_smooth_feeling(text: str, result: ValidationResult):
    """顺滑感检测（warning）：顺滑副词+情绪/顿悟/决策词组合，禁止情绪/顿悟/决策顺滑。"""
    hits = []
    for adv in _SMOOTH_ADVERBS:
        for tgt in _SMOOTH_TARGETS:
            # 顺滑副词与目标词间距 ≤4 字视为组合命中
            for m in re.finditer(re.escape(adv), text):
                window = text[m.end():m.end() + 4]
                if tgt in window:
                    hits.append(f'{adv}{tgt}')
    if hits:
        result.add(ValidationIssue(
            severity='warning',
            category='顺滑感',
            pattern='、'.join(sorted(set(hits)))[:80],
            count=len(hits),
            position=f'共 {len(hits)} 处情绪/顿悟/决策顺滑',
            suggestion='禁止情绪顺滑/顿悟顺滑/决策顺滑：人物应有矛盾心理、纠结、自我怀疑，不可瞬间释怀/顿悟/决定。',
        ))


def _check_transition_cliche(text: str, result: ValidationResult):
    """工整过渡套话检测（warning）：杜绝工整过渡与AI结尾套话。"""
    hits = [c for c in _TRANSITION_CLICHES if c in text]
    # 排比过度：连续 3 个以上结构相同短句（如"他A，他B，他C，他D"）
    parallel = re.findall(r'(?:他|她|它)[\u4e00-\u9fa5]{1,6}[，,](?:他|她|它)[\u4e00-\u9fa5]{1,6}[，,](?:他|她|它)[\u4e00-\u9fa5]{1,6}[，,]', text)
    if len(parallel) >= 1:
        hits.append('连续排比短句')
    if hits:
        result.add(ValidationIssue(
            severity='warning',
            category='工整过渡',
            pattern='、'.join(hits)[:80],
            count=len(hits),
            position=f'共 {len(hits)} 处工整过渡/套话',
            suggestion='杜绝工整过渡、AI结尾套话、过度排比，让文字自然流畅。',
        ))


def _check_perfect_cliche(text: str, result: ValidationResult):
    """完美套话检测（warning）：杜绝完美/标准套话。"""
    hits = [c for c in _PERFECT_CLICHES if c in text]
    if hits:
        result.add(ValidationIssue(
            severity='warning',
            category='完美套话',
            pattern='、'.join(hits)[:80],
            count=len(hits),
            position=f'共 {len(hits)} 处完美/标准套话',
            suggestion='杜绝完美、无懈可击、恰到好处等标准套话，删掉细碎修饰和刻板结构。',
        ))


def _check_long_sentence(text: str, result: ValidationResult):
    """长句过长检测（warning）：拆分长句，单句超过 80 字（含标点）。"""
    # 按句末标点切分
    sentences = re.split(r'[。！？!?]', text)
    long_count = 0
    samples = []
    for s in sentences:
        s = s.strip()
        if len(s) > 80:
            long_count += 1
            if len(samples) < 3:
                samples.append(s[:30] + '...')
    if long_count > 0:
        result.add(ValidationIssue(
            severity='warning',
            category='长句过长',
            pattern=f'{long_count}处长句',
            count=long_count,
            position=f'共 {long_count} 处单句>80字（{"、".join(samples)}）',
            suggestion='拆分长句，每段一个叙事重点，提升内容可读性。',
        ))


def _check_repetition(text: str, result: ValidationResult):
    """重复表达检测（warning）：合并重复观点，连续3句含相同关键词。"""
    sentences = re.split(r'[。！？!?]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 4]
    hits = 0
    # 滑动窗口检查连续3句是否有共同关键词（4字子串）
    for i in range(len(sentences) - 2):
        s1, s2, s3 = sentences[i], sentences[i + 1], sentences[i + 2]
        # 提取每句的4字子串集合
        def _ngrams(s):
            return {s[j:j + 4] for j in range(len(s) - 3)} if len(s) >= 4 else set()
        n1, n2, n3 = _ngrams(s1), _ngrams(s2), _ngrams(s3)
        common = n1 & n2 & n3
        # 排除纯标点/虚词子串
        common = {c for c in common if re.search(r'[\u4e00-\u9fa5]', c)}
        if common:
            hits += 1
    if hits > 0:
        result.add(ValidationIssue(
            severity='warning',
            category='重复表达',
            pattern=f'{hits}处重复',
            count=hits,
            position=f'共 {hits} 组连续3句含相同关键词',
            suggestion='保留核心信息，合并重复观点，删掉细碎修饰，让文字自然流畅。',
        ))


def _check_monotone_syntax(text: str, result: ValidationResult):
    """单一句式检测（warning）：避免单一句式，连续5句以相同主语开头。"""
    sentences = re.split(r'[。！？!?]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 2]
    if len(sentences) < 5:
        return
    hits = 0
    for i in range(len(sentences) - 4):
        # 取每句开头2字作为主语指纹
        heads = [sentences[i + j][:2] for j in range(5)]
        if len(set(heads)) == 1:
            hits += 1
    if hits > 0:
        result.add(ValidationIssue(
            severity='warning',
            category='单一句式',
            pattern=f'{hits}处单一句式',
            count=hits,
            position=f'共 {hits} 处连续5句同主语开头（{sentences[0][:2]}...）',
            suggestion='避免单一句式，重构语序，兼顾专业和灵活。',
        ))


# ====================================================================
# P2 扩展：基于 BookBible 的确定性硬伤校验
# ====================================================================

# 修仙/玄幻常见境界等级表（从低到高）。不同小说可部分命中；未命中则跳过境界回退检测。
_DEFAULT_REALM_ORDER = [
    '凡人', '练气', '筑基', '金丹', '元婴', '化神', '炼虚', '合体', '大乘', '渡劫', '飞升',
    '武者', '武师', '大武师', '宗师', '大宗师', '武王', '武皇', '武帝', '武神',
    '斗者', '斗师', '大斗师', '斗灵', '斗王', '斗皇', '斗宗', '斗尊', '斗圣', '斗帝',
    '黄阶', '玄阶', '地阶', '天阶',
    '一阶', '二阶', '三阶', '四阶', '五阶', '六阶', '七阶', '八阶', '九阶',
]

# 死亡关键词（KeyEvent 含此则判定角色死亡）
_DEATH_KEYWORDS = ['死亡', '身亡', '陨落', '陨', '战死', '丧命', '毙命', '死去', '身死', '气绝', '断气', '灰飞烟灭', '魂飞魄散']

# 复活关键词（KeyEvent 含此则撤销死亡判定）
_REVIVE_KEYWORDS = ['复活', '重生', '苏醒', '还魂', '复生', '诈死']

# 活人动作动词（死亡角色不应在正文中与这些词近距离共现）
_LIVING_ACTIONS = ['说道', '说：', '道：', '笑道', '怒道', '喝道', '冷笑道', '低声道', '高声道',
                   '点头', '摇头', '走来', '走出', '走入', '起身', '出手', '拔剑', '挥剑',
                   '运转', '盘膝', '凝视', '叹息', '皱眉', '拱手', '站起', '坐下', '开口']


def _extract_character_names_from_bible(bible: Dict) -> set:
    """从 character_profiles 提取角色名集合。
    支持两种格式：
    - 文本：## 角色：<姓名>
    - JSON：[{"name": "..."}] / [{"CharacterId": "..."}]
    """
    names = set()
    cp = (bible.get('character_profiles') or '').strip()
    if not cp:
        return names
    # 尝试 JSON
    if cp.startswith('[') or cp.startswith('{'):
        try:
            arr = json.loads(cp)
            if isinstance(arr, list):
                for c in arr:
                    if isinstance(c, dict):
                        nm = c.get('name') or c.get('CharacterId') or c.get('姓名') or ''
                        if nm and isinstance(nm, str):
                            names.add(nm.strip())
            elif isinstance(arr, dict):
                for c in (arr.get('characters') or []):
                    if isinstance(c, dict):
                        nm = c.get('name') or c.get('CharacterId') or ''
                        if nm and isinstance(nm, str):
                            names.add(nm.strip())
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    # 文本格式：## 角色：<姓名>
    for m in re.finditer(r'##\s*角色\s*[:：]\s*(.+?)$', cp, re.MULTILINE):
        nm = m.group(1).strip()
        nm = re.split(r'[（(]', nm)[0].strip()  # 去掉括号说明
        if nm and 2 <= len(nm) <= 10:
            names.add(nm)
    return names


def _extract_dead_characters_from_log(bible: Dict) -> Dict[str, int]:
    """从 chapter_changes_log 提取已死亡角色。
    返回 {角色名: 死亡章号}。已复活的角色会被移除。
    """
    dead: Dict[str, int] = {}
    log_str = bible.get('chapter_changes_log') or ''
    if not log_str:
        return dead
    try:
        log_list = json.loads(log_str)
    except (json.JSONDecodeError, ValueError, TypeError):
        return dead
    if not isinstance(log_list, list):
        return dead
    for entry in log_list:
        if not isinstance(entry, dict):
            continue
        ch_num = entry.get('chapter_num', 0) or 0
        try:
            ch_num = int(ch_num)
        except (ValueError, TypeError):
            ch_num = 0
        chg = entry.get('changes') or {}
        if not isinstance(chg, dict):
            continue
        for c in (chg.get('CharacterStateChanges') or []):
            if not isinstance(c, dict):
                continue
            nm = c.get('CharacterId') or c.get('Name') or ''
            if not nm or not isinstance(nm, str):
                continue
            nm = nm.strip()
            ke = c.get('KeyEvent') or ''
            if not isinstance(ke, str):
                ke = str(ke)
            # 复活判定优先（撤销之前的死亡）
            if any(kw in ke for kw in _REVIVE_KEYWORDS):
                dead.pop(nm, None)
                continue
            # 死亡判定（后章覆盖前章）
            if any(kw in ke for kw in _DEATH_KEYWORDS):
                dead[nm] = ch_num
    return dead


def _extract_character_realms_from_log(bible: Dict) -> Dict[str, tuple]:
    """从 chapter_changes_log 提取每个角色的最新境界。
    返回 {角色名: (境界名, 章号)}。后章覆盖前章。
    """
    realms: Dict[str, tuple] = {}
    log_str = bible.get('chapter_changes_log') or ''
    if not log_str:
        return realms
    try:
        log_list = json.loads(log_str)
    except (json.JSONDecodeError, ValueError, TypeError):
        return realms
    if not isinstance(log_list, list):
        return realms
    for entry in log_list:
        if not isinstance(entry, dict):
            continue
        ch_num = entry.get('chapter_num', 0) or 0
        try:
            ch_num = int(ch_num)
        except (ValueError, TypeError):
            ch_num = 0
        chg = entry.get('changes') or {}
        if not isinstance(chg, dict):
            continue
        for c in (chg.get('CharacterStateChanges') or []):
            if not isinstance(c, dict):
                continue
            nm = c.get('CharacterId') or c.get('Name') or ''
            lvl = c.get('NewLevel') or ''
            if nm and lvl and isinstance(nm, str) and isinstance(lvl, str):
                realms[nm.strip()] = (lvl.strip(), ch_num)
    return realms


def _realm_index(realm_str: str, realm_order: List[str]) -> int:
    """返回境界在等级表中的序号（越高越大）；未匹配返回 -1。"""
    if not realm_str:
        return -1
    for i, r in enumerate(realm_order):
        if r in realm_str:
            return i
    return -1


# 境界体系解析的常见锚点词（出现这些词的行/段大概率在描述境界体系）
_REALM_ANCHOR_KEYWORDS = ['境界', '修为', '实力划分', '等级划分', '力量等级', '修炼体系', '阶位', '段位', '品阶']
# 界定境界顺序的连接词
_REALM_SEPARATORS = ['→', '➜', '➞', '>', '＞', '，', '、', '/', '|', '至', '到', '然后']


def _parse_realm_order_from_bible(bible: Dict) -> Optional[List[str]]:
    """从 key_rules / worldbuilding 维度动态解析境界等级体系。
    识别策略：
      1. 优先找"境界/修为/等级划分"锚点行，提取该行的境界序列
      2. 提取"X→Y→Z"或"X、Y、Z"格式的境界链
      3. 过滤掉过短（<2字）或过长（>6字）的项
    返回从低到高的境界列表；解析失败返回 None（回退默认表）。
    """
    if not bible:
        return None
    combined = ((bible.get('key_rules') or '') + '\n' + (bible.get('worldbuilding') or ''))
    if not combined.strip():
        return None

    # 策略1：找含锚点词的行，提取该行的境界序列
    for line in combined.split('\n'):
        line = line.strip()
        if not line or len(line) > 200:
            continue
        if not any(kw in line for kw in _REALM_ANCHOR_KEYWORDS):
            continue
        # 该行很可能在描述境界体系，尝试提取境界链
        realms = _extract_realm_chain(line)
        if realms and len(realms) >= 3:
            return realms

    # 策略2：扫描全文找"X→Y→Z"格式的境界链（至少4个环节才算体系）
    import re as _re_realm
    # 匹配 "境界1→境界2→境界3→..." 这类显式链
    chain_pattern = r'([\u4e00-\u9fa5A-Za-z]{2,6}(?:→|➜|➞|>)[\u4e00-\u9fa5A-Za-z]{2,6}(?:(?:→|➜|➞|>)[\u4e00-\u9fa5A-Za-z]{2,6}){2,})'
    for m in _re_realm.finditer(chain_pattern, combined):
        chain = m.group(1)
        realms = _extract_realm_chain(chain)
        if realms and len(realms) >= 4:
            return realms

    return None


def _extract_realm_chain(text: str) -> List[str]:
    """从一段文本中提取境界序列。
    按 →/>/、/， 等分隔符拆分，过滤非境界词。
    """
    import re as _re_split
    # 按多种分隔符拆分
    parts = _re_split.split(r'[→➜➞>＞，、/|至]', text)
    realms = []
    seen = set()
    for p in parts:
        p = p.strip()
        # 去掉前缀编号和说明性文字
        p = _re_split.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩\d.、\s]+', '', p)
        # 去掉"XX划分/XX体系/XX等级"这类说明性前缀（保留真正的境界名）
        # 前缀最多8个中文字（如"异能等级划分""武道力量体系"）
        p = _re_split.sub(r'^[\u4e00-\u9fa5]{0,8}(?:等级|境界|修为|实力|划分|体系|阶位|段位|品阶)[：:]\s*', '', p)
        p = _re_split.sub(r'[：:（(].*$', '', p).strip()
        # 过滤：长度2-6字，非纯说明词
        if not p or len(p) < 2 or len(p) > 6:
            continue
        # 排除明显的非境界词（含说明性复合词）
        if p in {'境界', '修为', '实力', '等级', '划分', '体系', '修炼', '阶段', '分为', '以下',
                 '以上', '分别', '对应', '例如', '如下', '说明', '注',
                 '境界划分', '修为划分', '实力划分', '等级划分', '力量等级',
                 '异能等级', '异能等级划分', '武道体系', '修炼体系', '力量体系'}:
            continue
        # 排除含"划分/体系/等级/境界"子串的说明性词组
        if any(kw in p for kw in ['划分', '体系', '等级', '境界', '修为', '实力', '阶位', '段位', '品阶']):
            continue
        if p not in seen:
            seen.add(p)
            realms.append(p)
    return realms


def _get_realm_order(bible: Dict) -> List[str]:
    """获取境界等级表：优先用 bible 动态解析，失败回退默认表。"""
    dynamic = _parse_realm_order_from_bible(bible)
    if dynamic and len(dynamic) >= 3:
        return dynamic
    return _DEFAULT_REALM_ORDER


def _check_dead_character_revival(text: str, bible: Dict, result: ValidationResult):
    """死亡角色复活检测：已死亡角色在本章说话/行动。
    若 bible/worldbuilding 含转世/复活/封印等铺垫词，降为 warning；否则 critical。"""
    dead_chars = _extract_dead_characters_from_log(bible)
    if not dead_chars:
        return
    # 检索 bible 是否有复活铺垫
    revival_hints = ['转世', '复活', '封印', '夺舍', '重生', '还魂', '复生', '轮回', '神魂', '残魂', '残识', '附身', '魂魄', '转生']
    bible_text = ''.join(str(bible.get(k)) for k in ('key_rules', 'worldbuilding', 'character_profiles') if bible.get(k))
    has_revival_setup = any(h in bible_text for h in revival_hints)
    for name, dead_ch in dead_chars.items():
        if not name or len(name) < 2:
            continue
        if name not in text:
            continue
        # 检查角色名出现位置后 20 字内是否有活人动作
        reported = False
        for m in re.finditer(re.escape(name), text):
            window = text[m.end():m.end() + 20]
            if any(act in window for act in _LIVING_ACTIONS):
                # 局部铺垫：本章上下文含复活铺垫词
                local_hint = any(h in text[max(0, m.start() - 60):m.end() + 60] for h in revival_hints)
                if has_revival_setup or local_hint:
                    result.add(ValidationIssue(
                        severity='warning',
                        category='死亡角色复活(有铺垫)',
                        pattern=name,
                        count=1,
                        position=f'第{dead_ch}章已死亡，本章出现活人动作（有复活铺垫）',
                        suggestion=f'角色“{name}”已于第{dead_ch}章死亡，本章让其说话/行动。已有复活铺垫，请确认铺垫充分。',
                    ))
                else:
                    result.add(ValidationIssue(
                        severity='critical',
                        category='死亡角色无故复活',
                        pattern=name,
                        count=1,
                        position=f'第{dead_ch}章已死亡，本章出现活人动作',
                        suggestion=f'角色“{name}”已于第{dead_ch}章死亡，但本章让其说话/行动，属硬伤。若为回忆/幻觉/复活剧情，请显式标注铺垫。',
                    ))
                reported = True
                break
        if reported:
            continue


def _check_realm_regression(text: str, bible: Dict, result: ValidationResult):
    """境界/功法回退检测：角色已记录境界，本章出现明显更低的境界。
    检测范围从境界扩展到功法等级，加入"重伤/封印/反噬"等回退铺垫词判定。
    P2增强：优先使用从 key_rules/worldbuilding 动态解析的境界体系。
    """
    char_realms = _extract_character_realms_from_log(bible)
    if not char_realms:
        return
    # 动态解析境界表（P2增强），失败回退默认表
    realm_order = _get_realm_order(bible)
    # 回退铺垫词：有这些铺垫则降为 warning
    regression_hints = ['重伤', '封印', '反噬', '走火入魔', '被废', '废掉', '废除', '丹田破碎', '经脉寸断', '修为尽失']
    for name, (recorded_realm, rec_ch) in char_realms.items():
        if not name or len(name) < 2 or not recorded_realm:
            continue
        rec_idx = _realm_index(recorded_realm, realm_order)
        if rec_idx < 0:
            continue  # 记录境界未在等级表中，跳过
        if name not in text:
            continue
        # 在角色名 ±30 字窗口内找境界词
        for m in re.finditer(re.escape(name), text):
            window = text[max(0, m.start() - 30):m.end() + 30]
            hit_lower = None
            for r in realm_order:
                if r in window:
                    cur_idx = realm_order.index(r)
                    # 允许 1 级误差（描述差异），≥2 级才算回退
                    if cur_idx <= rec_idx - 2:
                        hit_lower = r
                        break
            if hit_lower:
                # 检查上下文是否有回退铺垫
                ctx_window = text[max(0, m.start() - 60):m.end() + 60]
                has_hint = any(h in ctx_window for h in regression_hints)
                if has_hint:
                    result.add(ValidationIssue(
                        severity='warning',
                        category='境界/功法回退(有铺垫)',
                        pattern=f'{name}:{recorded_realm}→{hit_lower}',
                        count=1,
                        position=f'第{rec_ch}章记录为{recorded_realm}，本章出现{hit_lower}（有回退铺垫）',
                        suggestion=f'角色“{name}”已记录为“{recorded_realm}”（第{rec_ch}章），本章出现“{hit_lower}”。已有回退铺垫，请确认合理。',
                    ))
                else:
                    result.add(ValidationIssue(
                        severity='critical',
                        category='境界/功法无故回退',
                        pattern=f'{name}:{recorded_realm}→{hit_lower}',
                        count=1,
                        position=f'第{rec_ch}章记录为{recorded_realm}，本章出现{hit_lower}',
                        suggestion=f'角色“{name}”已记录为“{recorded_realm}”（第{rec_ch}章），本章出现“{hit_lower}”疑似境界/功法无故回退，请核对或补铺垫。',
                    ))
                break  # 同一角色只报一次


def _check_unknown_character_names(text: str, bible: Dict, result: ValidationResult):
    """角色名一致性检测（warning）：正文中以"姓+称谓"出现的疑似新角色，
    但与已知名编辑距离=1，可能是错写。误报率较高，仅做 warning。
    """
    names = _extract_character_names_from_bible(bible)
    if not names:
        return
    # 只对 2-4 字名做近似检测
    candidates = {n for n in names if 2 <= len(n) <= 4}
    if not candidates:
        return
    # 提取正文中所有"X道/X说"模式中的 X（疑似角色名引用）
    # 非贪婪 {2,4}? 避免"笑/说/道"等动作字被吞入名字
    referenced = set()
    for m in re.finditer(r'([\u4e00-\u9fa5]{2,4}?)(?:说道|道：|说：|笑道|怒道|喝道|冷笑道|低声道)', text):
        referenced.add(m.group(1))
    # 对每个引用名，检查是否与已知名编辑距离=1且不相等
    for ref in referenced:
        if ref in candidates:
            continue
        for nm in candidates:
            # 简单编辑距离=1：同长度且仅差 1 字，或长度差 1 且为前缀/包含
            if len(ref) == len(nm):
                diff = sum(1 for a, b in zip(ref, nm) if a != b)
                if diff == 1:
                    result.add(ValidationIssue(
                        severity='warning',
                        category='角色名疑似错写',
                        pattern=f'{ref}≈{nm}',
                        count=1,
                        position=f'正文出现“{ref}”，已知角色有“{nm}”',
                        suggestion=f'正文“{ref}”与已知角色“{nm}”仅一字之差，请确认是否错写。',
                    ))
                    break


def validate_chapter_with_bible(content: str, bible: Optional[Dict] = None) -> ValidationResult:
    """带 bible 上下文的确定性后写校验。
    在 validate_chapter 基础上增加三类硬伤检测：
      1. 死亡角色复活检测（critical）
      2. 境界回退检测（critical）
      3. 角色名疑似错写检测（warning）
    bible: dict，可选字段 character_profiles / chapter_changes_log
    """
    result = validate_chapter(content)
    if not content or not content.strip():
        return result
    if not bible or not isinstance(bible, dict):
        return result

    text = content.strip()

    # 兜底保护：单次校验内每类硬伤最多报 5 条，避免长章误报刷屏
    try:
        _check_dead_character_revival(text, bible, result)
    except Exception:
        pass
    try:
        _check_realm_regression(text, bible, result)
    except Exception:
        pass
    try:
        _check_unknown_character_names(text, bible, result)
    except Exception:
        pass

    # 限制硬伤类问题总数，避免刷屏
    hard_categories = {'死亡角色复活', '境界回退', '角色名疑似错写'}
    hard_issues = [i for i in result.issues if i.category in hard_categories]
    if len(hard_issues) > 8:
        # 保留前 8 条，其余移除
        kept_ids = set(id(i) for i in hard_issues[:8])
        result.issues = [i for i in result.issues if i.category not in hard_categories or id(i) in kept_ids]

    return result


# ===== 借鉴 PlotPilot：张力评分 + 标准文风禁词扫描 =====

# 张力相关词库（用于量化叙事节奏，借鉴 PlotPilot 张力心电图）
_TENSION_HIGH_WORDS = [
    '冲', '杀', '战', '敌', '危', '险', '怒', '吼', '惊', '惧', '死', '伤', '血', '剑', '刀',
    '逃', '追', '拦', '挡', '攻', '守', '破', '裂', '爆', '震', '撞', '摔', '砸', '抢',
    '质问', '逼', '威胁', '陷阱', '阴谋', '背叛', '偷袭', '围攻', '绝境', '危机', '冲突',
]
_TENSION_LOW_WORDS = [
    '歇', '睡', '坐', '茶', '饭', '笑', '闲', '漫步', '回想', '沉思', '独坐', '静',
    '风景', '阳光', '微风', '平静', '安宁', '闲聊', '寒暄',
]
# 对话密度相关
_DIALOGUE_PATTERN = re.compile(r'[""“”『』].*?[""“”『』]')
# 短句标点（。！？）
_SENTENCE_END_PATTERN = re.compile(r'[。！？]')


def _check_tension_score(text: str, result: ValidationResult):
    """张力评分（借鉴 PlotPilot 张力心电图）。
    基于冲突词密度、对话密度、短句密度综合评分 0-100。
    低于 30 触发 warning（节奏过平），高于 90 触发 info（节奏过紧）。
    评分写入 stats 供前端展示张力曲线。"""
    if not text or len(text) < 100:
        return

    char_count = len(text)
    # 冲突词密度
    high_count = sum(text.count(w) for w in _TENSION_HIGH_WORDS)
    low_count = sum(text.count(w) for w in _TENSION_LOW_WORDS)
    # 对话密度（引号包裹的文本占比）
    dialog_matches = _DIALOGUE_PATTERN.findall(text)
    dialog_chars = sum(len(m) for m in dialog_matches)
    dialog_ratio = dialog_chars / char_count if char_count else 0
    # 短句密度（句号/问号/感叹号分割，短句占比）
    sentences = [s.strip() for s in _SENTENCE_END_PATTERN.split(text) if s.strip()]
    short_sentences = sum(1 for s in sentences if len(s) <= 20)
    short_ratio = short_sentences / len(sentences) if sentences else 0

    # 综合评分：冲突词权重 40% + 对话密度 30% + 短句节奏 30%
    high_score = min(40, high_count * 2)  # 每5个冲突词得10分，上限40
    dialog_score = min(30, dialog_ratio * 100)  # 对话占比30%得满分
    rhythm_score = min(30, short_ratio * 50)  # 短句占比60%得满分
    tension = int(high_score + dialog_score + rhythm_score)

    result.stats['tension_score'] = tension
    result.stats['tension_high_words'] = high_count
    result.stats['tension_low_words'] = low_count
    result.stats['dialog_ratio'] = round(dialog_ratio, 2)
    result.stats['short_sentence_ratio'] = round(short_ratio, 2)

    if tension < 30:
        result.add(ValidationIssue(
            severity='warning',
            category='张力过低',
            pattern='张力评分',
            count=tension,
            position=f'张力 {tension}/100',
            suggestion=f'本章张力过低（{tension}分），冲突词{high_count}个、对话占比{int(dialog_ratio*100)}%。建议增加冲突/对抗/悬念，避免纯叙述堆砌。',
        ))
    elif tension > 90:
        result.add(ValidationIssue(
            severity='info',
            category='张力过高',
            pattern='张力评分',
            count=tension,
            position=f'张力 {tension}/100',
            suggestion='本章张力过高，全程紧绷易疲劳。建议插入喘息段（对话/景物/回忆）调节节奏。',
        ))


# 标准文风禁词清单（与 app.py STANDARD_WRITING_STYLE_PROMPT 保持一致）
_STYLE_FORBIDDEN_WORDS = [
    '一股', '一抹', '不由得', '不禁', '随即', '旋即', '与此同时', '颇为', '甚为', '极为',
    '毫无疑问', '毋庸置疑', '不言而喻', '深吸一口气', '眼中闪过一丝', '心中暗想',
    '心念电转', '若有所思', '不知不觉间', '转眼间', '恍然大悟', '面无表情',
    '淡漠', '漠然', '眸子', '嘴角微微上扬', '如同', '宛如', '犹如', '周身', '周遭',
    '气息', '威压', '那道身影', '说话间', '话音未落', '当即', '顿时', '瞬时',
    '因此', '然而', '显而易见', '由此可见', '总而言之', '综上所述',
    # ===== Humanizer 5.1·删废话黑名单（16+7 词，正文 0 次命中）=====
    '值得注意的是', '总的来说', '不可否认', '众所周知', '值得一提的是', '换言之',
    '从某种意义上说', '需要指出的是', '也就是说', '换句话说', '不难看出', '可以说',
    '可以这么说', '需要说明的是',
    # ===== Humanizer 5.5·分析报告术语（正文绝不能出现，像AI把PPT硬塞进小说）=====
    '核心动机', '信息边界', '信息落差', '利益最大化', '底层逻辑', '认知差', '降维打击',
    '震惊', '复杂', '激动',
]


def _check_style_forbidden_words(text: str, result: ValidationResult):
    """标准文风禁词扫描（critical 级，对接 STANDARD_WRITING_STYLE_PROMPT 禁词清单）。
    命中即报 critical，强制作者规避 AI 常用词和书面化表达。"""
    if not text:
        return
    hits = {}
    for word in _STYLE_FORBIDDEN_WORDS:
        cnt = text.count(word)
        if cnt > 0:
            hits[word] = cnt
    if not hits:
        return
    # 按命中次数排序，取前 10 个展示
    sorted_hits = sorted(hits.items(), key=lambda x: -x[1])[:10]
    for word, cnt in sorted_hits:
        result.add(ValidationIssue(
            severity='critical',
            category='文风禁词',
            pattern=word,
            count=cnt,
            position=f'出现 {cnt} 次',
            suggestion=f'“{word}”在标准文风禁词清单中，禁止使用。请用动作/物象/对白替代。',
        ))


# ===== 视角与信息控制铁律：确定性检测 =====
# 对接 STANDARD_WRITING_STYLE_PROMPT 中的“视角与信息控制铁律”小节
# 检测上帝视角、剧透式叙述、上帝点评、伏笔明写、伏笔过载等违规

# 上帝视角/剧透式叙述触发词（命中即 critical）
_GOD_VIEW_PATTERNS = [
    # 剧透式预告（"他不知道此时…""这个决定将改变命运"）
    (r'他(?:不知道|不知道的是|不知道的是|未曾料到|没想到|不曾想到)[^。！？]{0,40}(?:将|会|日后|后来|最终|必将|注定)[^。！？]{0,20}(?:改变|决定|成为|遭遇|面临)', '剧透式叙述'),
    (r'此(?:时|刻)的他还不知道', '剧透式叙述'),
    (r'(?:他|她|它)(?:不知道|未曾察觉|不曾发觉)[^。！？]{0,30}(?:此时|此刻|与此同时|远在|另一边)[^。！？]{0,30}', '上帝视角·跨场景全知'),
    # 上帝点评（作者跳出来升华）
    (r'命运(?:就是|就是如此|便是|总是)(?:如此|这样|奇妙|神奇|弄人|无常)', '上帝点评·命运升华'),
    (r'冥冥之中(?:自有|似有|仿佛有)(?:天意|定数|安排|注定)', '上帝点评·冥冥天意'),
    (r'历史的车轮(?:滚滚|无情|缓缓)(?:向前|转动|碾过)', '上帝点评·历史车轮'),
    (r'(?:也许|或许)(?:这就是|这便是)(?:命运|天意|宿命|缘分)(?:的安排|的捉弄|的玩笑|吧)', '上帝点评·宿命论'),
    # 上帝视角·跨场景全知（"与此同时，另一边"）
    (r'与此同时[，,]?\s*(?:另一边|在千里之外|在远|在(?:他|她)看不到的)', '上帝视角·跨场景全知'),
    (r'(?:就在|正当)(?:此时|此刻|同一时间)[，,]?\s*(?:另一边|在千里之外|在远|在(?:他|她)不知道的)', '上帝视角·跨场景全知'),
    # 全知式心理入侵（"其实他不知道，对方心里在想…"）
    (r'(?:其实|实际上)(?:他|她)(?:不知道|不知道的是|不知道的是)[^。！？]{0,20}(?:心里|内心|心中)(?:想|盘算|计较)', '上帝视角·心理入侵'),
]

# 伏笔明写触发词（命中即 critical）
_FORESHADOW_LEAK_PATTERNS = [
    (r'这(?:是|里是|里就是|里是一处|里是一个)(?:一个|一处)?伏笔', '伏笔明写'),
    (r'此处(?:埋(?:下|设)|埋线|埋伏笔|是伏笔|是一处伏笔)', '伏笔明写'),
    (r'(?:后面|日后|之后|将来)(?:会)?回收(?:这个|这条|此)?伏笔', '伏笔明写'),
    (r'(?:这里|此处)(?:是|算是|就是)(?:为|为后面)?(?:埋|埋设|埋下)(?:的)?(?:伏笔|暗线|铺垫)', '伏笔明写'),
    (r'(?:埋下|设下)(?:一个|一处|一条)?伏笔[^。！？]{0,15}(?:后面|日后|之后)(?:会|将|将会)?(?:回收|揭晓|兑现)', '伏笔明写'),
    (r'这(?:个|条|处)(?:伏笔|暗线|铺垫)(?:将会|将在|将在后面|日后)(?:回收|揭晓|兑现|揭开)', '伏笔明写'),
    (r'此处(?:是|为)(?:一处|一个)?(?:伏笔|暗线|铺垫)', '伏笔明写'),
]

# 视角频繁切换检测（同段内出现3个以上不同人名+心理动词）
_PSYCH_VERBS = ['心想', '心中暗想', '内心', '心里想', '暗自', '心念', '思绪', '心中', '心想', '暗道']
_VIEW_SWITCH_PATTERN = re.compile(r'(?:(?:他|她|它)(?:心想|心中暗想|心里想|暗自|心念|暗道|内心|心中|思绪))')


def _check_god_view_and_foreshadow_leak(text: str, result: ValidationResult):
    """视角与信息控制铁律检测（critical 级）。
    检测上帝视角、剧透式叙述、上帝点评、伏笔明写、伏笔过载、视角频繁切换。
    对接 STANDARD_WRITING_STYLE_PROMPT 中的“视角与信息控制铁律”小节。"""
    if not text or len(text) < 20:
        return

    # 1. 上帝视角/剧透式叙述/上帝点评 模式匹配
    for pattern, category in _GOD_VIEW_PATTERNS:
        try:
            matches = re.findall(pattern, text)
            if matches:
                result.add(ValidationIssue(
                    severity='critical',
                    category=category,
                    pattern=pattern[:40],
                    count=len(matches),
                    position=f'共 {len(matches)} 处',
                    suggestion=f'命中{category}：违反视角锁定铁律。删除预告/升华/跨场景全知语句，只写视角人物能感知的内容。',
                ))
        except re.error:
            continue

    # 2. 伏笔明写检测
    for pattern, category in _FORESHADOW_LEAK_PATTERNS:
        try:
            matches = re.findall(pattern, text)
            if matches:
                result.add(ValidationIssue(
                    severity='critical',
                    category=category,
                    pattern=pattern[:40],
                    count=len(matches),
                    position=f'共 {len(matches)} 处',
                    suggestion=f'命中{category}：伏笔必须隐性埋设，伪装成日常细节/闲笔/环境描写。删除"这是伏笔/此处埋线/后面回收"等明示语句。',
                ))
        except re.error:
            continue

    # 3. 伏笔过载检测：单章"伏笔/暗线/铺垫"关键词出现超过3次（warning）
    foreshadow_keywords = ['伏笔', '暗线', '铺垫', '埋线', '暗棋', '后手']
    fs_count = sum(text.count(w) for w in foreshadow_keywords)
    if fs_count > 3:
        result.add(ValidationIssue(
            severity='warning',
            category='伏笔过载',
            pattern='伏笔/暗线关键词总和',
            count=fs_count,
            position=f'全章出现 {fs_count} 次',
            suggestion=f'本章伏笔/暗线/铺垫关键词出现 {fs_count} 次（建议≤3）。单章最多埋1-2处暗线，禁止一股脑集中铺设，分散到不同章节。',
        ))

    # 4. 视角频繁切换检测：同段内出现3个以上"他/她心想"类心理动词（warning）
    paragraphs = re.split(r'\n\s*\n', text)
    for i, p in enumerate(paragraphs):
        psych_matches = _VIEW_SWITCH_PATTERN.findall(p)
        if len(psych_matches) >= 3:
            result.add(ValidationIssue(
                severity='warning',
                category='视角频繁切换',
                pattern='同段心理动词',
                count=len(psych_matches),
                position=f'第{i+1}段',
                suggestion=f'第{i+1}段内出现 {len(psych_matches)} 处不同人物心理描写，视角频繁切换。单段应锁定1个视角人物，切换视角请用分场（空行+地点/人物标头）明确分隔。',
            ))
            break  # 只报第一处，避免刷屏


# ===== 借鉴 PlotPilot：文风指纹漂移检测（统计特征简化版） =====

def compute_style_fingerprint(text: str) -> Dict[str, float]:
    """计算文本的文风指纹（统计特征向量）。
    特征：平均句长、短句占比、对话占比、形容词密度、动作动词密度、标点密度。
    用于跨章节对比文风是否漂移。"""
    if not text or len(text) < 100:
        return {}
    char_count = len(text)
    # 句子分割
    sentences = [s.strip() for s in _SENTENCE_END_PATTERN.split(text) if s.strip()]
    sent_count = len(sentences) or 1
    sent_lengths = [len(s) for s in sentences]
    avg_sent_len = sum(sent_lengths) / sent_count
    short_sent_ratio = sum(1 for l in sent_lengths if l <= 20) / sent_count
    long_sent_ratio = sum(1 for l in sent_lengths if l > 70) / sent_count
    # 对话占比
    dialog_matches = _DIALOGUE_PATTERN.findall(text)
    dialog_chars = sum(len(m) for m in dialog_matches)
    dialog_ratio = dialog_chars / char_count
    # 标点密度（，。！？；：）
    punct_count = sum(text.count(p) for p in '，。！？；：')
    punct_density = punct_count / char_count
    # 形容词密度（简化：常见形容词后缀）
    adj_patterns = ['的', '地', '美丽', '英俊', '强大', '神秘', '古老', '深邃', '明亮', '黑暗']
    adj_count = sum(text.count(a) for a in adj_patterns)
    adj_density = adj_count / char_count
    # 动作动词密度
    verb_count = sum(text.count(v) for v in _TENSION_HIGH_WORDS[:20])  # 复用张力词库前20个
    verb_density = verb_count / char_count
    return {
        'avg_sent_len': round(avg_sent_len, 1),
        'short_sent_ratio': round(short_sent_ratio, 3),
        'long_sent_ratio': round(long_sent_ratio, 3),
        'dialog_ratio': round(dialog_ratio, 3),
        'punct_density': round(punct_density, 3),
        'adj_density': round(adj_density, 4),
        'verb_density': round(verb_density, 4),
    }


def detect_style_drift(current_fp: Dict[str, float], baseline_fp: Dict[str, float]) -> Optional[Dict]:
    """检测当前章文风指纹是否偏离基准（前几章平均指纹）。
    返回漂移报告 dict 或 None（无显著漂移）。
    阈值：任一特征偏差超过 30% 视为漂移。"""
    if not current_fp or not baseline_fp:
        return None
    drifts = []
    for key in ['avg_sent_len', 'short_sent_ratio', 'dialog_ratio', 'punct_density', 'adj_density', 'verb_density']:
        cur = current_fp.get(key, 0)
        base = baseline_fp.get(key, 0)
        if base == 0:
            continue
        diff_ratio = abs(cur - base) / base
        if diff_ratio > 0.3:  # 偏差超 30%
            drifts.append({
                'feature': key,
                'baseline': base,
                'current': cur,
                'drift_ratio': round(diff_ratio, 2),
            })
    if not drifts:
        return None
    return {
        'drifted': True,
        'drift_count': len(drifts),
        'drifts': drifts,
        'suggestion': f'文风漂移检测：{len(drifts)} 项特征偏离基准（{"、".join(d["feature"] for d in drifts[:3])}），建议检查是否切换了叙述视角或语气。',
    }


def validate_chapter_with_drift(content: str, baseline_fp: Optional[Dict[str, float]] = None) -> ValidationResult:
    """带文风漂移检测的章节校验（扩展入口）。
    baseline_fp: 前几章的文风指纹基准（由 app.py 计算并传入）。
    若提供基准且检测到漂移，追加 warning。"""
    result = validate_chapter(content)
    if not content or not baseline_fp:
        return result
    try:
        current_fp = compute_style_fingerprint(content)
        result.stats['style_fingerprint'] = current_fp
        drift = detect_style_drift(current_fp, baseline_fp)
        if drift:
            result.stats['style_drift'] = drift
            result.add(ValidationIssue(
                severity='warning',
                category='文风漂移',
                pattern='文风指纹',
                count=drift['drift_count'],
                position=f'{drift["drift_count"]} 项特征偏离',
                suggestion=drift['suggestion'],
            ))
    except Exception:
        pass
    return result


def _check_quantitative_hardcards(text: str, cfg: Dict, result: ValidationResult):
    """硬卡4：整章量化双轨自检 —— 对应 app.py 文风铁律硬卡 4.1~4.4。
    统计口径：
      - 段落：空行分割（忽略 HTML <p> 包裹，先脱标签）
      - 句子段内句数：段内「。！？」合计作为句号数（句终标点总计数）
      - 句均字数：按 _SENTENCE_END_PATTERN 拆句，单句字数=句内字符数（含标点）
      - 段均句数 = 全章句终标点数 / 段落数；段均句数比值越大越碎（目标 ≤ 1.8）
    口径说明：与 chat_collab_bp.WRITING_STYLE_RULES 短段主导对齐——主力段落 10-50 字
    （1-2 个逗号长句）、句均约 12-18 字。4.3 不再按旧"叙述句 20-35 字"口径把短段判碎。
    """
    # 1) 脱 <p> 等标签再统计（避免 html 化正文干扰空行计数）
    stripped = re.sub(r'</?[^>]+>', '', text)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', stripped) if p and p.strip()]
    if not paragraphs:
        paragraphs = [stripped] if stripped else []
    if not paragraphs:
        return
    n_par = len(paragraphs)

    # 4.1 段内句号/句终标点硬上限：每自然段 ≤ 2，一般以每自然段 1 个句号为主；含 ≥ 3 句终标点的段数必须 = 0
    # 4.1b 补充口径：段落字数（含标点）≤15 字的短段，句终标点必须 ≤ 1（短段绝对不允许塞 2 句完整话）
    over3_per_par = 0
    first_over3_idx = -1
    first_over3_count = 0
    short_le15_ge2 = 0                 # ≤15字短段 且 ≥2个句终标点 的段数
    first_le15_idx = -1
    first_le15_count = 0
    first_le15_len = 0
    per_par_period_counts = []
    total_sentences = 0
    total_sentence_chars = 0
    sent_lengths = []
    for i, p in enumerate(paragraphs):
        plen = len(p)
        c = 0
        for pend in ('。', '！', '？'):
            c += p.count(pend)
        per_par_period_counts.append(c)
        total_sentences += c
        # 句级切分：按句终标点切开后算每个单句长度（含标点，把句号算入前一句尾）
        frags = re.split(r'([。！？])', p)
        # 把标点并回前一句
        sents = []
        buf = ''
        for ch in frags:
            if ch in ('。', '！', '？'):
                sents.append((buf + ch).strip())
                buf = ''
            else:
                buf = ch
        if buf.strip():
            pass  # 段尾未结句的不计数
        for s in sents:
            sl = len(s)
            if sl > 0:
                total_sentence_chars += sl
                sent_lengths.append(sl)
        if c >= 3:
            over3_per_par += 1
            if first_over3_idx < 0:
                first_over3_idx = i + 1
                first_over3_count = c
        # 4.1b 短段≤15字 但 ≥2个句终标点 → 违规（最典型AI碎段形态）
        if plen <= 15 and c >= 2:
            short_le15_ge2 += 1
            if first_le15_idx < 0:
                first_le15_idx = i + 1
                first_le15_count = c
                first_le15_len = plen

    # 4.2 段均字数比例：≤70字 占比 ≥ 70%（手机端三行内）；叙述短段(<40字)仅统计参考（含对白段，占比无下限要求）
    par_lengths = [len(p) for p in paragraphs]
    le70_ratio = sum(1 for l in par_lengths if l <= 70) / n_par if n_par else 1
    lt40_ratio = sum(1 for l in par_lengths if l < 40) / n_par if n_par else 1

    # 4.3 句均字数 12–22（含标点）
    if sent_lengths:
        avg_sent_len = total_sentence_chars / len(sent_lengths)
    else:
        avg_sent_len = 0.0
    short_shards = sum(1 for l in sent_lengths if l < 10)
    short_shard_ratio = short_shards / len(sent_lengths) if sent_lengths else 0.0

    # 4.4 段均句数：全章句数 / 段落数 目标 ≤ 1.8；> 2.0 立即判碎
    if n_par and total_sentences:
        sentences_per_par = total_sentences / n_par
    else:
        sentences_per_par = 0.0

    # 写入 stats（无论是否告警都记录，前端 / 报告可展示）
    stats = result.stats
    stats['par_count'] = n_par
    stats['sentences_total'] = total_sentences
    stats['sentences_per_par'] = round(sentences_per_par, 2)
    stats['avg_sent_len'] = round(avg_sent_len, 1)
    stats['sent_len_samples'] = len(sent_lengths)
    stats['par_length_le70_ratio'] = round(le70_ratio, 3)
    stats['par_length_lt40_ratio'] = round(lt40_ratio, 3)
    stats['short_sent_shard_ratio'] = round(short_shard_ratio, 3)
    stats['pars_with_periods_ge3'] = over3_per_par
    stats['pars_le15_periods_ge2'] = short_le15_ge2  # ≤15字短段塞≥2句号的段数

    # 新增 Q-extra：对白占比（估算对白段数 / 总段数；简易版按段含""或「」引号算对白段）
    dialogue_pars = 0
    for p in paragraphs:
        if '"' in p or '「' in p or '”' in p:
            dialogue_pars += 1
    dialog_ratio = dialogue_pars / n_par if n_par else 0.0
    stats['dialog_ratio'] = round(dialog_ratio, 3)

    # 新增 Q-extra：tension_score 简易估算（高张力词密度，按 emotion/conflict/动作词）
    tension_high_tokens = [
        '死','杀','血','炸','碎','震','崩','断','爆','刺','劈','砸','砸烂','撕','裂','喊','喝','吼','冷笑','咬牙','发抖','颤抖','恐惧','愤怒','暴怒','焦急','崩溃','绝望','危险','警报','警告','锁','扣','抓住','危机','倒计时','紧急','立刻','骤然','陡然','猛然','突然','忽然','竟','竟然','黑色','红','血色','火光','电火花','蜂鸣','咔哒','金属响','落锁','落下去',
    ]
    tension_low_tokens = ['豆浆','馒头','早餐','热','香气','冷','白','灯','影子','影子','脚步','慢走','走着','看了看','低下头','抬头','笑了','轻声','咳','嘟囔','划痕','指甲','油泥','机油','扳手','螺丝刀','笔记','账本','资料','纸张']
    text_for_tension = text[:12000] if len(text) > 12000 else text
    t_high = sum(text_for_tension.count(w) for w in tension_high_tokens)
    t_low = sum(text_for_tension.count(w) for w in tension_low_tokens)
    tension_den = t_high + t_low + 1
    tension_score_raw = min(100, int(100 * (t_high + 0.5 * t_low) / tension_den))  # 相对比例0-100
    # 校准：t_low >= 15 说明喘息段充足，按比例下调 tension score_raw 上限
    if t_low >= 15:
        tension_score_raw = int(tension_score_raw * 0.70)
    elif t_low >= 8:
        tension_score_raw = int(tension_score_raw * 0.85)
    tension_score = max(0, min(100, tension_score_raw))
    stats['tension_score'] = tension_score
    stats['tension_high_words'] = t_high
    stats['tension_low_words'] = t_low

    # ===== 告警判定（分 critical / warning 两级）=====
    # 4.1 段内≥3句号 —— 段数 ≥ 3 或 段占比 ≥ 10% 任一满足 → critical；段数 1–2 warning（更严口径：段占比+段数双阈值）
    over3_ratio = over3_per_par / n_par if n_par else 0.0
    if over3_per_par > 0:
        severity = 'critical' if (over3_per_par >= 3 or over3_ratio >= 0.10) else 'warning'
        sev_label = '严重不合格' if severity == 'critical' else '不合格'
        result.add(ValidationIssue(
            severity=severity,
            category='硬卡4.1·段内句号数超限',
            pattern='段内含≥3个句号（句终标点）',
            count=over3_per_par,
            position=f'例如第 {first_over3_idx} 段含 {first_over3_count} 句；整章共 {over3_per_par} 段（占 {over3_ratio*100:.1f}%）',
            suggestion=(
                f'文风铁律 4.1 规定每自然段 ≤ 2 个句号（一般以每自然段 1 个句号为主，=最多 2 句完整话），'
                f'但本章有 {over3_per_par} 段堆了 ≥ 3 句小短句（漫画分镜脚本化是最浓 AI 味来源）。'
                f'修复：把同 POV/同镜头/同动作链的 3+ 个小短句合并成 1–2 句完整中长句；'
                f'绝不允许一句话硬剁成 3+ 个残切碎段。'
            ),
        ))

    # 4.1b 短段（≤15字）塞 ≥2 个句终标点 → 命中即判 critical（典型AI碎段：短段里挤2个完整句号）
    if short_le15_ge2 > 0:
        short_le15_ratio = short_le15_ge2 / n_par if n_par else 0.0
        result.add(ValidationIssue(
            severity='critical' if (short_le15_ge2 >= 2 or short_le15_ratio >= 0.05) else 'warning',
            category='硬卡4.1b·短段句号超限（≤15字硬塞≥2句）',
            pattern='段落字数≤15字 且 句终标点≥2',
            count=short_le15_ge2,
            position=f'例如第 {first_le15_idx} 段仅 {first_le15_len} 字就塞了 {first_le15_count} 句；整章共 {short_le15_ge2} 段（占 {short_le15_ratio*100:.1f}%）',
            suggestion=(
                f'文风铁律 4.1 补充口径：≤15 字的短段只能含 ≤ 1 个句号（短段里绝对不允许塞 2 句完整话）。'
                f'修复：①把 2 句短段合并成 1 句逗号长句（16–28字），要么②拆成 2 个独立短段各含 1 句（仅用于重拍/转折/收尾）；'
                f'绝不允许 7–13 字一段里挤 2 个句号。'
            ),
        ))

    # 4.4 段均句数 > 2.0 → critical；1.8–2.0 → warning
    if sentences_per_par > 2.0:
        result.add(ValidationIssue(
            severity='critical',
            category='硬卡4.4·段均句数超限（整章碎段）',
            pattern='句数/段数 比值',
            count=int(sentences_per_par * 10),
            position=f'段均句数 = {total_sentences}/{n_par} ≈ {sentences_per_par:.2f}（硬卡 ≤ 1.8；> 2.0 立即判定 AI 碎段）',
            suggestion=(
                f'整章段落切得太碎：平均每段塞了 {sentences_per_par:.1f} 句话。'
                f'修复：①连续 3 段一句话独立段 → 至少合并相邻 2 段成 1 段含 1–2 句；'
                f'②把同镜头动作链的残切小句合并（例如「他抬手。他握拳。他砸下。」→「他抬手握拳，狠狠砸下。」）。'
            ),
        ))
    elif sentences_per_par > 1.8:
        result.add(ValidationIssue(
            severity='warning',
            category='硬卡4.4·段均句数偏高（接近碎段）',
            pattern='句数/段数 比值',
            count=int(sentences_per_par * 10),
            position=f'段均句数 = {total_sentences}/{n_par} ≈ {sentences_per_par:.2f}（硬卡 ≤ 1.8）',
            suggestion='部分段落仍偏碎：把同 POV/同场景的相邻短段合并，降低段均句数。',
        ))

    # 4.3 句均字数（叙述句口径，与 WRITING_STYLE_RULES 短段主导对齐：主力段落 10-50 字、句均约 12-18 字）
    if sent_lengths:
        if avg_sent_len < 8:
            result.add(ValidationIssue(
                severity='critical',
                category='硬卡4.3·句均字数过短（整章碎句）',
                pattern='句均字数',
                count=int(avg_sent_len * 10),
                position=f'句均 {avg_sent_len:.1f} 字 / 共 {len(sent_lengths)} 句；短碎句(<10字)占比 {short_shard_ratio*100:.0f}%（短段主导句均约 12-18 字）',
                suggestion='整章句子被切成了碎碎的几个字一句（像 AI 战报）。修复：连续 3 个 ＜10 字残句必须合并成完整句（逗号串联 1-2 个动作单元收一个句号）；句内补连接词/状语使主谓齐全。',
            ))
        elif avg_sent_len < 11:
            result.add(ValidationIssue(
                severity='warning',
                category='硬卡4.3·句均字数偏短（接近碎句）',
                pattern='句均字数',
                count=int(avg_sent_len * 10),
                position=f'句均 {avg_sent_len:.1f} 字 / 共 {len(sent_lengths)} 句（短段主导句均约 12-18 字）',
                suggestion='叙述句略偏碎：把同场景同动作链的相邻句号短句合并（"他踩进泥水。脚底滑过硬东西。"→"他踩进泥水，脚底滑过硬东西。"），让叙述句落到 12-18 字区间。',
            ))
        elif avg_sent_len > 26:
            result.add(ValidationIssue(
                severity='warning',
                category='硬卡4.3·句均字数偏长（背离短段主导）',
                pattern='句均字数',
                count=int(avg_sent_len * 10),
                position=f'句均 {avg_sent_len:.1f} 字 / 共 {len(sent_lengths)} 句（短段主导句均约 12-18 字）',
                suggestion='句子过长、叙述主力偏长段：按语义节点拆成 1-2 个动作单元收尾的完整句，让主力段落回到 10-50 字区间。',
            ))

        # 短碎句占比过高也单独报
        if short_shard_ratio >= 0.30:
            result.add(ValidationIssue(
                severity='warning' if short_shard_ratio < 0.45 else 'critical',
                category='硬卡4.3·短碎句占比过高',
                pattern='单句 < 8 字',
                count=short_shards,
                position=f'{short_shards}/{len(sent_lengths)} 句（{short_shard_ratio*100:.0f}%）为 <8 字残句',
                suggestion='一大堆「几字小句」拼起来最像 AI 战报。修复：按动作链/镜头合并残句，每 3–4 个残句拼成 1 句完整话，落到 12-18 字区间。',
            ))

    # 4.2 段均字数比例
    if le70_ratio < 0.60:
        result.add(ValidationIssue(
            severity='warning',
            category='硬卡4.2·长段占比过高（手机端难读）',
            pattern='段字数 ≤ 70 字占比',
            count=int(le70_ratio * 100),
            position=f'{int(le70_ratio*100)}% 的段落 ≤ 70 字（硬卡 ≥ 70%）',
            suggestion='段落普遍过长（>手机端三行）。修复：短段主导——在场景切换/镜头切换/对白前后空行分段，用力短段 10-50 字（1-2 个逗号长句）承载动作，把 80+ 字长段从语义节点拆成 2-3 段。',
        ))
    if lt40_ratio < 0.25:
        result.add(ValidationIssue(
            severity='info',
            category='硬卡4.2·两行内短段偏少（节奏偏平）',
            pattern='段字数 < 40 字占比',
            count=int(lt40_ratio * 10),
            position=f'{int(lt40_ratio*100)}% 的段落 < 40 字（建议 ≥ 40%）',
            suggestion='建议把炸点/对白/最狠那句单独成段，制造视觉停顿与节奏呼吸。',
        ))

    # Q-extra 告警判定：对白占比（<20% critical；20–25% warning）— 叙述全=作者讲解=僵硬AI味
    if dialog_ratio < 0.20:
        result.add(ValidationIssue(
            severity='critical',
            category='对白占比铁律不足（僵硬AI味头号来源）',
            pattern='对白段 / 总段数',
            count=int(dialog_ratio * 100),
            position=f'对白段仅占 {dialog_ratio*100:.0f}%（13条铁律下限 25%，真人爽文 40-55%）；80%+都是叙述在"讲故事"，僵硬感直接出',
            suggestion='对白拉到 35%+ 才会自然（真人对话占比高）。不用开新剧情，3种不用动脑的拉对白方法：①把叙述里人物会说的话改成旁边人碎嘴 ②把主角独白改成自言自语声口 ③每个叙述主力段后加5-10字碎对白（骂一句/疑问/吐槽），不推进剧情只拉人味。',
        ))
    elif dialog_ratio < 0.25:
        result.add(ValidationIssue(
            severity='warning',
            category='对白占比偏低（接近僵硬阈值）',
            pattern='对白段 / 总段数',
            count=int(dialog_ratio * 100),
            position=f'对白段占比 {dialog_ratio*100:.0f}%（下限 25%，建议 35%+）',
            suggestion='用上面3种方法（碎嘴/自言自语/吐槽对白）补 3–5 段对白，叙述比例立刻降。',
        ))

    # Q-extra 告警判定：tension_score ≥ 95 → warning；连续紧绷（t_low <5 且 tension_score≥95 其实就是全程无喘息）→ 升级 critical
    if tension_score >= 95:
        t_low_cnt = stats.get('tension_low_words', 0)
        sev = 'critical' if t_low_cnt < 5 else 'warning'
        result.add(ValidationIssue(
            severity=sev,
            category='节奏温度·张力全程过高（无喘息段=读者疲劳+AI紧绷模板腔）',
            pattern='tension_score',
            count=tension_score,
            position=f'张力评分 {tension_score}/100（高张力词 {t_high} vs 喘息词 {t_low_cnt}）；写作要求10.7铁律：至少 15% 段是 Band1/Band2 喘息段',
            suggestion='立即补 1–2 段喘息段（不用推进剧情）3选1：①环境锚（豆浆热气裹脸/冷馒头渣卡喉咙咳3声）②人物小动作锚（抠表盖划痕到指甲发白）③碎嘴对白锚（主角自己吐槽1句）。喘息段补完，tension_score 就会自然降到 80-85 区间，疲劳没了，人味立刻出。',
        ))



# ====================================================================
# B1：风格对齐度 12 维评分器（配套文风对齐 SkillPack，纯 regex/统计，零 LLM 成本）
# ====================================================================

# 8 大 AI 套话比喻词（与 NARRATIVE_CRAFT_RULES §0 总则口径一致）
_8_AI_CLICHE_METAPHORS = ['宛如', '犹如', '恍若', '宛若', '大海', '巨龙', '深渊', '星河']

# 提示语引导词（XXX说/道/问/答/喊/叫……），用于判断提示语在对白的句首/句中/句尾
_DIALOGUE_TAGS = [
    '说', '道', '问', '答', '喊', '叫', '喝道', '冷道', '笑道', '叹道', '低声道', '高声道',
    '怒道', '急道', '悠悠道', '淡淡道', '缓缓道', '轻声道', '应道', '回道', '咬牙道',
    '吩咐道', '解释道', '叮嘱道', '安抚道', '嗤笑道', '讥笑道', '调侃道', '涩声道',
]

# 感官细节关键词（温度/气味/触感/声音），用于判断动作段是否有细节三叠
_SENSE_WORDS = [
    '冷', '凉', '冰', '烫', '热', '暖', '温',  # 温度
    '臭', '腥', '香', '骚', '膻', '味', '馊', '霉',  # 气味
    '疼', '痛', '刺', '麻', '胀', '酸', '痒', '扎', '滑', '黏', '软', '硬', '糙', '硌',  # 触感
    '嗡', '咚', '铛', '砰', '啪', '嚓', '嘶', '哑', '颤', '响', '鸣', '啸',  # 声音
    '血', '泥', '灰', '尘', '沙', '汗',  # 材质
]

# 目标/决策/验证词（用于判断动作目标闭环）
_GOAL_WORDS = ['得', '要', '得把', '必须', '先', '先把', '找', '放', '藏', '挪', '搬']
_DECISION_WORDS = ['于是', '就', '干脆', '索性', '只好', '只得', '当即', '立刻', '决定', '打定主意']
_VERIFY_WORDS = ['还好', '幸好', '果然', '果真', '不枉', '没白', '幸亏', '多亏', '早知道', '好在']

# Q2-2 新增：跨章钩子常见时间节点 + 悬念名词词（结尾末段命中=关联合格，不再强求在前文出现）
# ⚠️ 只做"微加成"（防误判扣光），不再直接给满分
_CROSS_CHAPTER_HOOK_TIME_WORDS = ['天后', '天后，', '日后', '下月', '来年', '明晚', '明早', '明天', '后天', '冬至', '除夕', '初一', '十五', '年底', '月末', '三天后', '三日后', '七日后', '半月后', '一月后', '三月后', '半年后', '一年后', '十年后', '百年后']
_CROSS_CHAPTER_HOOK_SUSPENSE_WORDS = ['名单', '天灯', '约', '赌', '局', '宴', '帖', '令', '符', '契', '阵', '劫', '寿', '榜文', '密信', '暗令', '遗诏', '名册', '玉碟', '丹方', '剑谱', '密约', '婚约', '血书', '阵图']

# （注意：以下 _BACKGROUND_EXEMPT_WORDS / _PLOT_ABSORB_WORDS / _PLOT_RESULT_WORDS 已在对应函数内废弃，不再引用，
#  反例（如矿道文第7章）里出现的背景群像/吸收→突破不应该被"豁免"或"奖励"，严格抓问题。）

# 递进比较链连接词（3-4 层"鸡→牛→小牛→小巫见大巫"式结构）
_COMPARISON_CHAIN_WORDS = ['比起', '可比', '相比', '可见', '小巫见大巫', '大巫见小巫', '差得远', '肉眼可见的差距']

# 修正感句式触发词
_CORRECTION_WORDS = ['不是', '准确说', '准确的说是', '不对', '……不对', '不，', '不…', '不是…']


def _check_humanizer_patterns(text: str, result: ValidationResult):
    """Humanizer 硬卡5 专项检测：5.2禁止句式 + 5.3被字句 + 5.4X地副词 + 5.1/5.5已由_style_forbidden_words+forbidden_patterns兜底。
    此处做计数级统计 + 超阈值告警（独立计数方便前端展示）。"""
    if not text:
        return
    stats = result.stats
    # 5.2 虽然但是/不仅而且/第一第二第三列举腔/连续了堆砌/排比三连
    sb_count = len(re.findall(r'虽然[^。！？]{4,60}但是', text))
    bj_count = len(re.findall(r'不仅[^。！？]{4,60}(而且|并且|还同时|更进一步)', text))
    enum3_count = len(re.findall(r'(?:第一|首先)[^。！？]{2,30}(?:第二|其次)[^。！？]{2,30}(?:第三|最后)', text))
    # 连续「了」堆砌：按单句切，单句内 count('了')>=3 的句数
    le_ge3_sents = 0
    first_le_sent = ''
    for s in re.split(r'[。！？\n]', text):
        s = s.strip()
        if not s:
            continue
        n = s.count('了')
        if n >= 3:
            le_ge3_sents += 1
            if not first_le_sent:
                first_le_sent = s[:36] + ('…' if len(s) > 36 else '')
    # 5.3 被字句（简单计数：按"被…谓语动词"句型，认常见 28 个被动动词 + 被动构式）
    bei_re = re.compile(r'被[^，。！？\n]{0,30}(?:捏碎|打碎|杀死|打伤|吓跑|传到|推开|吹开|吹倒|打开|关上|放开|咬住|刺伤|劈碎|砸烂|扔掉|丢下|拖走|拉住|拽住|按住|压住|撞上|抓住|发现|带走|传了出去|捏|抓|打|杀|吓|传|推|吹|开|关|放|咬|刺|劈|砸|扔|丢|拖|拉|拽|按|压|撞)')
    bei_count = len(bei_re.findall(text))
    # 补抓"被吓了一跳/被吓到"的短式（避免被上面 30 字范围 + 动词表卡掉）
    bei_count += len(re.findall(r'被(?:他|她|它|他们|她们)?(?:给|把|叫|让)?吓了一跳', text))
    # 5.4 X地副词（高频模板词）
    adv_pat = re.compile(r'(冷冷地|悄悄地|快速地|慢慢地|缓缓地|死死地|轻轻地|狠狠地|微微地|默默地|静静地|重重地|深深地|紧紧地)')
    adv_hits = {}
    for m in adv_pat.finditer(text):
        w = m.group(1)
        adv_hits[w] = adv_hits.get(w, 0) + 1
    adv_total = sum(adv_hits.values())
    adv_top3 = sorted(adv_hits.items(), key=lambda x: -x[1])[:3]
    stats['humanizer_suoran_danshi'] = sb_count
    stats['humanizer_bujin_erqie'] = bj_count
    stats['humanizer_enum3_liedui'] = enum3_count
    stats['humanizer_sentence_le_ge3'] = le_ge3_sents
    stats['humanizer_beizi_count'] = bei_count
    stats['humanizer_advde_total'] = adv_total
    # 告警判定（critical / warning）
    if sb_count > 0:
        result.add(ValidationIssue(
            severity='critical' if sb_count >= 2 else 'warning',
            category='Humanizer 5.2·「虽然…但是…」公式转折',
            pattern='虽然…但是…',
            count=sb_count,
            position=f'全章 {sb_count} 处；写法属于AI标准对仗模板',
            suggestion='不要用「虽然A但是B」硬套转折。改用角色内心吐槽/前后动作反差写转折：「虽然他很强，但是他输了」→「他确实强，可对面那个老东西更脏」。',
        ))
    if bj_count > 0:
        result.add(ValidationIssue(
            severity='critical' if bj_count >= 1 else 'warning',
            category='Humanizer 5.2·「不仅…而且…」递进对仗',
            pattern='不仅…而且/并且…',
            count=bj_count,
            position=f'全章 {bj_count} 处',
            suggestion='「不仅A而且B」是AI写说明文的模板。拆成两句，各写一个事实，让读者自己感受递进。',
        ))
    if enum3_count > 0:
        result.add(ValidationIssue(
            severity='critical',
            category='Humanizer 5.2·「第一/第二/第三」三段式列举腔',
            pattern='第一…第二…第三/首先…其次…最后…',
            count=enum3_count,
            position=f'全章 {enum3_count} 处三段式列举',
            suggestion='不要把正文写成会议纪要。两项或四项都比三项自然；改成散句叙述，每条信息埋在动作/对白里。',
        ))
    if le_ge3_sents > 0:
        result.add(ValidationIssue(
            severity='warning' if le_ge3_sents <= 1 else 'critical',
            category='Humanizer 5.2·连续「了」字堆砌',
            pattern='同一句≥3个「了」',
            count=le_ge3_sents,
            position=f'全章 {le_ge3_sents} 句；例：{first_le_sent}',
            suggestion='一句只保留 1 个有力的"了"，其余删掉或改动词原形：「他走了过去，拿了杯子，喝了一口水」→「他走过去，端起杯子，灌了一口」。',
        ))
    if bei_count > 1:
        result.add(ValidationIssue(
            severity='critical' if bei_count > 1 else ('warning' if bei_count == 1 else 'info'),
            category='Humanizer 5.3·被字句超阈值（网文偏爱主动）',
            pattern='含「被」+ 被动谓语',
            count=bei_count,
            position=f'整章 {bei_count} 处被字句（硬卡 ≤ 1 处，>1 直接 critical）',
            suggestion='翻成主动语态：「杯子被他捏碎了」→「他捏碎了杯子」；「消息被传到城里」→「消息传到城里」；「被吓了一跳」→「他浑身一激灵」。主动语态天然有网文味。',
        ))
    if adv_total >= 3:
        result.add(ValidationIssue(
            severity='warning' if adv_total <= 5 else 'critical',
            category='Humanizer 5.4·「X地」副词模板化',
            pattern='冷冷地/悄悄地/快速地/慢慢地/死死地… 合计≥3处',
            count=adv_total,
            position=f'合计 {adv_total} 处；TOP：' + '，'.join(f'{w}×{c}' for w,c in adv_top3),
            suggestion='不要写「X地XX」副词模板。冷冷地说→写动作微表情；悄悄地走→写声音+触感。副词一律删，换成具体描写。',
        ))


def _clamp_score(v):
    return max(0, min(100, int(round(v))))


def _check_style_alignment_score(text: str, cfg: Dict, result: ValidationResult):
    """B1：12 维风格对齐度评分，写入 stats.style_alignment；低分维度追加 warning。
    口径对齐文风黄金对白 6 式 + 文风黄金长短句 4 型 + ONE 主钩子数字硬约束。
    """
    import math
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    sentences = [s.strip() for s in re.split(r'[。！？!?；;]', text) if s.strip()]
    total_chars = len(text) or 1
    per_k = total_chars / 1000

    # 准备对白段集合（段首/段尾出现「"」或「」」的段视为对白段）
    dialogue_paras = [p for p in paragraphs if ('"' in p and ('“' in p or '”' in p) or ('\u201c' in p or '\u201d' in p))]
    # 简化：包含中文引号的段
    dialogue_paras = [p for p in paragraphs if ('“' in p or '”' in p or '"' in p)]
    # 从对白段抽取对白句（按引号截取）
    dialogue_sents = []
    for p in dialogue_paras:
        for m in re.finditer(r'[“"]([^”"]{1,80})[”"]', p):
            dialogue_sents.append(m.group(1).strip())

    # ================ 12 个维度 ================
    dims = {}  # key -> dict(score, note)

    # 1) 提示语中位率（理想值：≥60% 的提示语不在对白句首，即嵌在中间或尾部）
    total_tagged = 0
    mid_or_tail = 0
    for tag in _DIALOGUE_TAGS:
        # 匹配模式：对白 + 提示语位置
        # 句首模式（扣）："XXX" + 提示语对白：“...” XXX说/道
        # 更简单的统计：段落里包含 tag，且 tag 所在位置不是段落开头
        for p in paragraphs:
            if tag not in p:
                continue
            idx = p.find(tag)
            # 必须是一个独立对白提示单元（tag前后是动作/标点/人名，不是在词组中间）
            before = p[max(0, idx - 5):idx]
            after = p[idx + len(tag):min(len(p), idx + len(tag) + 5)]
            # 合法提示语：前面通常是"…"或动作，后面是"："或换行或句号
            if ('：' in after[:3] or ':' in after[:3] or '。' in after[:3] or '”' in before or '"' in before or '“' in after or '"' in after):
                total_tagged += 1
                # 若提示语前面有对白引号（说明对白在提示语前=提示语在对白后/中）→ mid_or_tail
                pre_win = p[max(0, idx - 40):idx]
                if '”' in pre_win or '"' in pre_win:
                    mid_or_tail += 1
    mid_ratio = mid_or_tail / total_tagged if total_tagged else 0.5
    score_mid = _clamp_score(60 + 80 * (mid_ratio - 0.5))  # 0.5→60，1.0→100
    note_mid = f'对白提示语 {mid_or_tail}/{total_tagged} 处放在对白中或尾（中位率 {mid_ratio:.0%}），理想≥60%'
    if total_tagged == 0:
        score_mid, note_mid = 80, '未检测到明显提示语（全文短对白或无"说/道"标记，默认偏良）'
    dims['prompt_tag_mid'] = dict(name='对话·提示语中位率', score=score_mid, note=note_mid)

    # 2) 问答错位率（理想值：连续 1:1 工整问答占比 ≤30%）
    q_then_a = 0
    dial_total = max(1, len(dialogue_sents))
    for i in range(len(dialogue_sents) - 1):
        q = dialogue_sents[i]
        a = dialogue_sents[i + 1]
        if q.endswith('?') or q.endswith('？') or '吗' in q or '呢' in q or '不' in q and 1 <= len(q) <= 15:
            # 直接完整答（非反问、非半句话、无……、不是答别的）=工整问答
            if 1 <= len(a) <= 20 and '……' not in a and '...' not in a and (not any(k in a for k in ['？', '?', '不，', '不对', '你管', '关你'])):
                q_then_a += 1
    q_ratio = q_then_a / dial_total if dial_total else 0
    score_q = _clamp_score(100 - 140 * max(0, q_ratio - 0.3))  # 0.3→100，1.0→0
    note_q = f'1:1工整问答 {q_then_a}/{dial_total} 对白句（占比 {q_ratio:.0%}），理想≤30%'
    dims['qa_disalign'] = dict(name='对话·问答错位率', score=score_q, note=note_q)

    # 3) 对话动作插入率（理想值：每 3 句对白≥1 句动作段插，达标率≥60%）
    dial_block_count = 0
    action_insert_count = 0
    # 以对白段为核心，前后一段看是否动作段
    for pi, p in enumerate(paragraphs):
        if p not in dialogue_paras:
            continue
        dial_block_count += 1
        # 前一段/后一段不是对白段，且含动作词（看是否存在含主谓结构的动作段，即不含引号但长度≥4字符）
        for nb_idx in (pi - 1, pi + 1):
            if 0 <= nb_idx < len(paragraphs):
                nb = paragraphs[nb_idx]
                if nb not in dialogue_paras and 4 <= len(nb) <= 60:
                    action_insert_count += 1
                    break
    insert_ratio = action_insert_count / dial_block_count if dial_block_count else 1.0
    score_insert = _clamp_score(40 + 100 * min(1.0, insert_ratio - 0.2))  # 0.2→40，1.0→100
    note_insert = f'动作段插入 {action_insert_count}/{dial_block_count} 对白块（插率 {insert_ratio:.0%}），理想≥每3句对白插1次'
    dims['dial_action_insert'] = dict(name='对话·动作插入率', score=score_insert, note=note_insert)

    # 4) 独立支线数（剧情不散乱：估计独立场景块 - 核心块的差；0-1 最佳）
    # 启发式：按连续 2 个以上空行？不，paragraphs 已经按空行切。
    # 独立"路人支线"的特征：一段里出现的姓名+称呼，在其他段中都没再次出现。
    para_chars = []
    name_re = re.compile(r'[\u4e00-\u9fa5]{2,4}(?=(?:爷|哥|姐|叔|婶|师傅|师兄|师姐|公子|姑娘|夫人|老师|老板|同学|保安|队长))|[\u4e00-\u9fa5]{2,3}(?=[说道问喊喝道])')
    names_by_para = []
    for p in paragraphs:
        names = set(name_re.findall(p)) | set(re.findall(r'[\u4e00-\u9fa5]{2,3}(?=(?:说|道|问|喊|喝|叫|笑|骂))', p))
        names_by_para.append({n for n in names if n not in {'不是', '没有', '他们', '我们', '你们', '自己', '大家', '怎么', '什么', '这个'}})
    all_names = set()
    for s in names_by_para:
        all_names |= s
    name_global_freq = {n: sum(1 for s in names_by_para if n in s) for n in all_names}
    # 只出现 1 段的"一次性名字"→独立支线候选
    one_shot_names = [n for n, f in name_global_freq.items() if f == 1]
    one_shot_paras = sum(1 for s in names_by_para if any(n in one_shot_names for n in s))
    # 独立支线估计数 = 一次性名字段落数（最多 4 段一支线）
    estimated_side = (one_shot_paras + 3) // 4 if one_shot_paras else 0
    score_side = _clamp_score(100 - 20 * estimated_side)  # 0→100，多1支扣20
    note_side = f'估计独立支线 {estimated_side} 条（一次性角色段 {one_shot_paras} 段），理想=0（严禁路人支线）'
    dims['side_story'] = dict(name='剧情·独立支线数', score=score_side, note=note_side)

    # 5) 结尾钩子关联度（结尾 10% 的字符中再次命中本章前 90% 中出现过的关键词）
    cutoff = int(total_chars * 0.9)
    head_text = text[:cutoff]
    tail_text = text[cutoff:]
    tail_last200 = tail_text[-200:] if len(tail_text) > 200 else tail_text
    cross_time_hit = any(w in tail_last200 for w in _CROSS_CHAPTER_HOOK_TIME_WORDS)
    cross_susp_hit = any(w in tail_last200 for w in _CROSS_CHAPTER_HOOK_SUSPENSE_WORDS)
    head_keywords = set(re.findall(r'[\u4e00-\u9fa5]{2,4}', head_text))
    # 过滤单段频率不高的词（只留头 50 个最常见 2-4 字实体）
    from collections import Counter
    head_counts = Counter(re.findall(r'[\u4e00-\u9fa5]{2,4}', head_text))
    head_top = {w for w, _ in head_counts.most_common(50) if w not in {'我们', '你们', '他们', '自己', '这个', '那个', '什么', '怎么', '不是', '没有'}}
    tail_hits = sum(1 for w in re.findall(r'[\u4e00-\u9fa5]{2,4}', tail_text) if w in head_top)
    # 结尾 10% 段中至少 3 个命中头高频词=关联良好
    tail_hits_expected = max(1, int(len(tail_text) / 100))
    hook_ratio = tail_hits / tail_hits_expected if tail_hits_expected else 1.0
    # 回滚：跨章锚点只给**最多 0.6 的比例保底微加成**（防全扣光），不再直接给100分——反例（矿道文）末尾冬至+名单这种敷衍式伏笔钩子不该奖励
    if cross_time_hit or cross_susp_hit:
        hook_ratio = max(hook_ratio, 0.6)
    score_hook = _clamp_score(min(100, 50 + 50 * min(1.0, hook_ratio)))
    note_hook = f'结尾与前文高频词命中 {tail_hits}/{tail_hits_expected}（关联比 {hook_ratio:.2f}，跨章锚点{"识别，给0.6保底" if cross_time_hit or cross_susp_hit else "未识别"}），理想≥1.0 且只留 1 个主钩子；跨章伏笔钩子需要在前文有线索铺垫，否则是生硬空降伏笔'
    dims['end_hook_link'] = dict(name='剧情·结尾钩子关联', score=score_hook, note=note_hook)

    # 6) 长段臃肿率（真人金标准：>100字 仅 0.1%；阈值>3%=告警，>5%=低分）
    long_threshold = cfg.get('long_paragraph_threshold', 100)
    long_max_r    = cfg.get('long_paragraph_max_ratio', 0.03)
    long_count = sum(1 for p in paragraphs if len(p) > long_threshold)
    long_ratio = long_count / len(paragraphs) if paragraphs else 0
    # 评分曲线：ratio=0→100，ratio=long_max_r(3%)→80，ratio=10%→40，ratio≥18%→0
    score_long = _clamp_score(max(0, 100 - (long_ratio / max(1e-6, long_max_r)) * 20 - max(0, long_ratio - long_max_r) / 0.07 * 80))
    note_long = f'臃肿段（>{long_threshold}字）{long_count}/{len(paragraphs)}段，占比 {long_ratio:.0%}，理想≤{long_max_r:.0%}（真人0.1%）'
    dims['long_paragraph'] = dict(name='长短句·长段臃肿率', score=score_long, note=note_long)

    # 7) 段长节奏变异系数（CV 健康=0.50-1.00；CV<0.30=机械网格，CV>1.60=过于零碎）
    lens = [len(p) for p in paragraphs if paragraphs]
    cv_min_cfg = cfg.get('cv_min_healthy', 0.30)
    cv_max_cfg = cfg.get('cv_max_healthy', 1.60)
    if len(lens) >= 4:
        mean = sum(lens) / len(lens)
        var = sum((x - mean) ** 2 for x in lens) / len(lens)
        std = math.sqrt(var)
        cv = std / mean if mean > 0 else 0
        # 评分曲线：
        #   CV 0.50-1.00 → 100 分（健康区）
        #   CV 0.30-0.50 → 线性 70→100（偏均匀，可接受）
        #   CV < 0.30     → 0→70（明显机械网格）
        #   CV 1.00-1.60 → 100→60（偏零碎，可接受）
        #   CV > 1.60     → 60 往下掉 每超0.1扣10分
        if cv < cv_min_cfg:
            score_uniform = _clamp_score(cv / cv_min_cfg * 70)
        elif cv < 0.50:
            score_uniform = _clamp_score(70 + (cv - cv_min_cfg) / (0.50 - cv_min_cfg) * 30)
        elif cv <= 1.00:
            score_uniform = 100
        elif cv <= cv_max_cfg:
            score_uniform = _clamp_score(100 - (cv - 1.00) / (cv_max_cfg - 1.00) * 40)
        else:
            score_uniform = _clamp_score(max(0, 60 - (cv - cv_max_cfg) / 0.1 * 10))
        note_uniform = (f'段长变异系数 CV={cv:.2f}（均值段长 {mean:.0f}字），'
                        f'健康区间 0.50-1.00（真人金标准=0.67）；'
                        f'CV<{cv_min_cfg:.2f}=机械网格，CV>{cv_max_cfg:.2f}=过于零碎')
    else:
        score_uniform, note_uniform = 90, '段落太少，均匀性不统计（默认良）'
    dims['para_uniformity'] = dict(name='长短句·段长均匀率', score=score_uniform, note=note_uniform)

    # 8) 递进比较链数（每 10000 字≥2 条递进比较链为良）
    chain_hits = sum(1 for w in _COMPARISON_CHAIN_WORDS if w in text)
    chain_den = total_chars / 10000
    chain_expected = max(1, 2 * chain_den) if chain_den > 0.2 else 0
    chain_ratio = chain_hits / chain_expected if chain_expected else 1.0
    score_chain = _clamp_score(60 + 40 * min(1.0, chain_ratio))
    note_chain = f'递进比较链 {chain_hits} 条（比起/可见/小巫见大巫等连接词命中），理想≥{chain_expected:.0f}/章'
    if chain_den <= 0.2:
        score_chain, note_chain = 95, '短章（<2000字）递进链不考核（默认优）'
    dims['comparison_chain'] = dict(name='长短句·递进比较链数', score=score_chain, note=note_chain)

    # 9) 比喻套话命中率（命中 8 大 AI 套话词 = 直接拉低）
    cliche_hits = sum(text.count(w) for w in _8_AI_CLICHE_METAPHORS)
    cliche_allowed = max(0, 3 * per_k)
    if cliche_hits <= cliche_allowed:
        score_cliche = 100
    else:
        over = (cliche_hits - cliche_allowed) / max(1, cliche_allowed)
        score_cliche = _clamp_score(max(0, 100 - 50 * over))
    note_cliche = f'8大AI套话比喻词命中 {cliche_hits} 处（允许≤{cliche_allowed:.0f}），严禁宛如/犹如/恍若/宛若+大海/巨龙/深渊/星河'
    dims['cliche_metaphor'] = dict(name='语气·比喻套话率', score=score_cliche, note=note_cliche)

    # 10) 修正感句式率（每 1500 字≥1 条"不是X，准确说Y"/"不对"为良）
    corr_hits = sum(text.count(w) for w in _CORRECTION_WORDS)
    corr_allowed = max(0, total_chars / 1500)
    corr_ratio = corr_hits / corr_allowed if corr_allowed else 1.0
    score_corr = _clamp_score(60 + 40 * min(1.0, corr_ratio))
    note_corr = f'修正感句式 {corr_hits} 条（不是…/不对…/准确说…），理想≥{corr_allowed:.1f}/章'
    if total_chars < 1500:
        score_corr, note_corr = 90, '短章（<1500字）修正感句式不考核（默认良）'
    dims['correction_style'] = dict(name='语气·修正感句式率', score=score_corr, note=note_corr)

    # 11) 动作感官细节密度（动作段中 6 大类感官词的密度，每 500 字≥2 个为良）
    # 动作段=不含对白引号的段
    action_paras = [p for p in paragraphs if '“' not in p and '”' not in p and '"' not in p]
    action_chars = sum(len(p) for p in action_paras) or 1
    sense_hits = sum(1 for w in _SENSE_WORDS for c in action_paras if w in c)
    sense_density = sense_hits / (action_chars / 500)  # 每500字动作段的感官词数
    if sense_density < 0.5:
        score_sense = _clamp_score(sense_density / 0.5 * 60)
    elif sense_density < 2:
        score_sense = _clamp_score(60 + (sense_density - 0.5) / 1.5 * 40)
    else:
        score_sense = 100
    note_sense = f'动作段感官词 {sense_hits} 个（动作段共 {action_chars} 字，密度 {sense_density:.2f}/500字），理想≥2/500字；避免干巴巴"他藏好"'
    dims['sense_detail'] = dict(name='文字·动作感官细节密度', score=score_sense, note=note_sense)

    # 12) 动作目标闭环率（每 1500 字至少 1 个"目标→决策→验证"小三段闭环）
    # 简化：目标词 + 决策词 + 验证词三者在 300 字窗口内共同出现视为 1 个闭环
    goal_idx = [m.start() for w in _GOAL_WORDS for m in re.finditer(re.escape(w), text)]
    dec_idx = [m.start() for w in _DECISION_WORDS for m in re.finditer(re.escape(w), text)]
    ver_idx = [m.start() for w in _VERIFY_WORDS for m in re.finditer(re.escape(w), text)]
    closed = 0
    WINDOW = 300
    # 对每个目标词，看 window 内是否有决策词、附近 1000 字内是否有验证词
    for g in goal_idx:
        has_dec = any(abs(g - d) <= WINDOW for d in dec_idx)
        has_ver = any(abs(v - g) <= 3 * WINDOW for v in ver_idx)  # 验证词可以在更后
        if has_dec and has_ver:
            closed += 1
    closed_expected = max(0, total_chars / 1500)
    closed_ratio = closed / closed_expected if closed_expected else 1.0
    if closed_expected == 0:
        score_closed, note_closed = 95, '极短章，动作闭环不考核（默认优）'
    else:
        score_closed = _clamp_score(50 + 50 * min(1.0, closed_ratio))
        note_closed = f'动作闭环 {closed} 个（目标词→决策→验证窗口内命中），理想≥{closed_expected:.1f} 个/章；避免无目标流水账；"熬十鞭→突破"类空闭环不奖励（节奏/铺垫不到位就是问题）'
    dims['goal_closed_chain'] = dict(name='文字·动作目标闭环率', score=score_closed, note=note_closed)

    # ================ 写入 stats + warning ================
    result.stats['style_alignment'] = dims
    scores = [d['score'] for d in dims.values()]
    avg = sum(scores) / len(scores) if scores else 0
    result.stats['style_alignment_avg'] = round(avg, 1)
    # 60 以下的维度集中写 1 条 warning（不重复写 N 条爆 issue）
    bad = [(k, d) for k, d in dims.items() if d['score'] < 60]
    if bad:
        summary = '；'.join(
            f'{d["name"]} {d["score"]}分（{d["note"]}）' for _, d in bad[:4])
        if len(bad) > 4:
            summary += f'…（共{len(bad)}项不及格）'
        result.add(ValidationIssue(
            severity='warning',
            category='风格对齐',
            pattern='12维风格评分',
            count=len(bad),
            position=f'风格对齐平均分 {avg:.0f}/100，共 {len(bad)} 项<60分',
            suggestion=summary + '。具体改法对照：文风黄金对白6式 + 文风黄金长短句4型 + ONE主钩子数字硬约束。',
        ))

