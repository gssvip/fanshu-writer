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
    """死亡角色复活检测（critical）：已死亡角色在本章说话/行动。"""
    dead_chars = _extract_dead_characters_from_log(bible)
    if not dead_chars:
        return
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
                result.add(ValidationIssue(
                    severity='critical',
                    category='死亡角色复活',
                    pattern=name,
                    count=1,
                    position=f'第{dead_ch}章已死亡，本章出现活人动作',
                    suggestion=f'角色「{name}」已于第{dead_ch}章死亡，但本章让其说话/行动，属硬伤。若为回忆/幻觉/复活剧情，请显式标注。',
                ))
                reported = True
                break
        if reported:
            continue


def _check_realm_regression(text: str, bible: Dict, result: ValidationResult):
    """境界回退检测（critical）：角色已记录境界，本章出现明显更低的境界。
    P2增强：优先使用从 key_rules/worldbuilding 动态解析的境界体系，
    非修仙题材（都市/科幻/异能）也能检测境界回退；解析失败回退默认表。
    """
    char_realms = _extract_character_realms_from_log(bible)
    if not char_realms:
        return
    # 动态解析境界表（P2增强），失败回退默认表
    realm_order = _get_realm_order(bible)
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
                result.add(ValidationIssue(
                    severity='critical',
                    category='境界回退',
                    pattern=f'{name}:{recorded_realm}→{hit_lower}',
                    count=1,
                    position=f'第{rec_ch}章记录为{recorded_realm}，本章出现{hit_lower}',
                    suggestion=f'角色「{name}」已记录为「{recorded_realm}」（第{rec_ch}章），本章出现「{hit_lower}」疑似境界回退，请核对。',
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
                        position=f'正文出现「{ref}」，已知角色有「{nm}」',
                        suggestion=f'正文「{ref}」与已知角色「{nm}」仅一字之差，请确认是否错写。',
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
