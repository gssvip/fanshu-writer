"""
落地门禁（P2-10）+ PRE_WRITE_CHECK（P2-11）
章节落库前的确定性拦截 + 写章前的意图对齐。

参考：天命 6道门禁（裁剪到3道低门槛）+ InkOS PRE_WRITE_CHECK 13行表
设计原则：
  - 3道门禁均为确定性检查，零 LLM 成本
  - 门禁只做 warning，不硬阻断（避免误杀正常章节）
  - PRE_WRITE_CHECK 注入 prompt 顶部，要求 LLM 写正文前先输出意图表
"""
import re
import json
from typing import Dict, List, Tuple


# ===== P2-10：3道落地门禁 =====

def gate_protocol_check(content: str) -> Dict:
    """门禁1：协议解析检查。
    检查 LLM 是否按格式输出（CHANGES 标签是否完整、正文是否非空）。
    【修复】先剥离所有内部标签（pre_write_check/chapter_changes/【标题】）再检查字数，
    避免标签内容被计入导致误判；阈值从500改为1500（标准是2400±100，过短才critical）。"""
    issues = []
    if not content or not content.strip():
        issues.append({'gate': 'protocol', 'severity': 'critical',
                       'message': '正文为空，可能生成失败'})
        return {'passed': False, 'issues': issues}

    # 先剥离所有内部标签，得到纯正文
    body = re.sub(r'<chapter_changes>[\s\S]*?</chapter_changes>', '', content, flags=re.IGNORECASE)
    body = re.sub(r'<pre_write_check>[\s\S]*?</pre_write_check>', '', body, flags=re.IGNORECASE)
    body = re.sub(r'【标题】[^\n]*', '', body)
    # 剥离末尾的标题 JSON 块
    body = re.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', body).strip()

    # 统计纯正文字数（中文字符数）
    cn_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', body))
    if cn_chars < 1500:
        issues.append({'gate': 'protocol', 'severity': 'critical',
                       'message': f'正文过短（{cn_chars}字<1500字），可能生成失败'})
    elif cn_chars < 2000:
        issues.append({'gate': 'protocol', 'severity': 'warning',
                       'message': f'正文偏短（{cn_chars}字，标准2400±100），建议检查'})

    # 检查是否有残留的 prompt 标签（LLM 误把指令当输出）
    leaked_tags = re.findall(r'<(?:system|user|assistant)>', content)
    if leaked_tags:
        issues.append({'gate': 'protocol', 'severity': 'warning',
                       'message': '正文残留 prompt 标签，可能 LLM 误输出指令'})

    # 检查是否有 CHANGES 标签（P1-6 启用后应有）
    has_changes = bool(re.search(r'<chapter_changes>', content, re.IGNORECASE))
    if not has_changes:
        issues.append({'gate': 'protocol', 'severity': 'warning',
                       'message': '未输出 chapter_changes 标签，状态回写将跳过'})

    return {'passed': len([i for i in issues if i['severity'] == 'critical']) == 0, 'issues': issues}


def gate_reference_check(content: str, bb) -> Dict:
    """门禁2：引用校验（P1-4：从正则升级为实体注册表对照）。

    检查正文中提到的人物/地点/势力是否在实体注册表（EntityHub）中定义，防 LLM 编造实体。
    首选 bb.entity_registry_json（覆盖人物/势力/地点/物品/技能全维度）；
    registry 为空的老书再回退 Character 表 + character_profiles 正则。
    """
    issues = []
    if not bb or not content:
        return {'passed': True, 'issues': issues}

    # 1) 提取人物引用（"XX说/道/笑…"，多字动词放前面+名字非贪婪，避免"林墨冷哼道"误吞"冷哼"）
    person_refs = set(re.findall(r'([\u4e00-\u9fa5]{2,4}?)(?:冷哼|低喝|喊道|说道|说|道|笑|怒|惊|叹|问|答|喝|劝|喊)', content))
    location_refs = set(re.findall(r'(?:在|于|前往|抵达|回到|进入|离开|踏足)([\u4e00-\u9fa5]{2,6})(?:中|里|外|前|后|附近|方向|深处)', content))
    refs = person_refs | location_refs
    if not refs:
        return {'passed': True, 'issues': issues}

    # 2) 已定义实体：优先实体注册表（多桶对照）
    from entity_registry import _load_registry, _is_valid_entity_name
    registry = _load_registry(bb)
    defined = set()
    for bucket in ('characters', 'locations', 'factions'):
        defined |= set((registry.get(bucket) or {}).keys())

    # 回退：老书 registry 为空时，用 Character 表 + character_profiles regex 兜底（避免误报）
    if not defined:
        try:
            for ch in bb.book.characters:
                if ch and ch.name:
                    defined.add(ch.name)
        except Exception:
            pass
        if bb.character_profiles:
            for m in re.finditer(r'##\s*角色[：:]\s*([^\n]+)', bb.character_profiles):
                name = m.group(1).strip().split('（')[0].split('(')[0].strip()
                if name:
                    defined.add(name)

    # 只在有已定义实体时才校验，避免空 bible/无注册表误报
    if not defined:
        return {'passed': True, 'issues': issues}

    # 3) 对照：未在注册表中的引用才提示；用强噪声过滤替代硬编码停用词
    undefined = {r for r in refs if r not in defined and _is_valid_entity_name(r)}
    if undefined and len(undefined) <= 5:  # 超过5个可能是误判
        issues.append({'gate': 'reference', 'severity': 'warning',
                       'message': f'可能引用了未定义实体：{", ".join(sorted(undefined)[:3])}',
                       'undefined': sorted(undefined)})

    return {'passed': len([i for i in issues if i['severity'] == 'critical']) == 0, 'issues': issues}


def gate_blueprint_check(content: str, bb, chapter_num: int) -> Dict:
    """门禁3：蓝图出场检查。
    检查本章是否涉及了 outline_hierarchy 中规划的关键角色/事件。
    简化版：检查正文中是否提到该章节规划的关键词。"""
    issues = []
    if not bb or not bb.outline_hierarchy or not content:
        return {'passed': True, 'issues': issues}

    try:
        hierarchy = json.loads(bb.outline_hierarchy)
    except Exception:
        return {'passed': True, 'issues': issues}

    # 找到本章的规划
    chapter_plan = None
    for ch in hierarchy.get('chapters', []):
        if ch.get('chapter_num') == chapter_num:
            chapter_plan = ch
            break

    if not chapter_plan:
        return {'passed': True, 'issues': issues}

    # 检查 content_focus（节标题）是否在正文中出现
    focus = chapter_plan.get('content_focus', '')
    if focus and len(focus) > 2:
        # 取焦点关键词（前4字）
        keywords = [focus[:4], focus[-4:]]
        found = any(kw in content for kw in keywords if len(kw) >= 2)
        if not found:
            issues.append({'gate': 'blueprint', 'severity': 'warning',
                           'message': f'本章规划焦点“{focus[:10]}”未在正文中体现'})

    return {'passed': len([i for i in issues if i['severity'] == 'critical']) == 0, 'issues': issues}


def run_all_gates(content: str, bb, chapter_num: int) -> Dict:
    """运行全部3道门禁，返回汇总结果。

    S1 升级：
    - 有 critical 问题时默认 blocked=True（调用方应阻断落库，由前端二次确认 ignore_gates 才放行）
    - 仅 warning 时仍 passed=True，但 issues 附带给 LLM/前端看
    """
    results = []
    results.append(gate_protocol_check(content))
    results.append(gate_reference_check(content, bb))
    results.append(gate_blueprint_check(content, bb, chapter_num))

    all_issues = []
    for r in results:
        all_issues.extend(r['issues'])

    critical_count = sum(1 for i in all_issues if i['severity'] == 'critical')
    warning_count = sum(1 for i in all_issues if i['severity'] == 'warning')
    return {
        'passed': critical_count == 0,
        'blocked': critical_count > 0,   # S1：critical 默认 block
        'critical_count': critical_count,
        'warning_count': warning_count,
        'issues': all_issues,
    }


# ===== P2-11：PRE_WRITE_CHECK 13行表 =====

def build_pre_write_check_prompt(chapter_num: int, bb, dag_hooks: str = '') -> str:
    """构建 PRE_WRITE_CHECK 模板（注入章节 prompt 顶部）。
    要求 LLM 先写正文，正文后再输出 13 行意图表，避免检查报告出现在正文前面影响阅读感。"""
    return f"""

【写章后·PRE_WRITE_CHECK】（P2-11）
本章输出顺序铁律：① 先写正文 → ② 正文结束后空一行，输出 <pre_write_check> 表格（13行） → ③ 最后输出 <chapter_changes> JSON。
禁止在正文前面输出任何检查报告/表格/分析，正文必须是输出的第一部分。

正文写完后，空一行，输出 <pre_write_check> 表格：

<pre_write_check>
| 检查项 | 本章记录 |
| 当前任务 | （一句话复述本章核心目标）|
| 读者期待 | （读者此刻最想看到什么）|
| 上章衔接 | （上章结尾的悬念/状态）|
| 本章核心事件 | （1-3个必须发生的事件）|
| 待回收伏笔 | （本章应收的伏笔ID，无则填"无"）|
| 本章埋设伏笔 | （本章应埋的伏笔ID，无则填"无"）|
| 角色出场 | （本章出场的角色名单）|
| 章尾改变 | （本章结束时世界/人物的变化）|
| 不要做 | （本章禁止的事：OOC/越界/崩坏）|
| 戏剧位置 | （起/承/转/合/过渡）|
| 风险扫描 | （OOC风险/信息越界风险/战力崩坏风险）|
| 字数预算 | （2300-2500字）|
</pre_write_check>

表格后再输出 <chapter_changes> JSON。"""
