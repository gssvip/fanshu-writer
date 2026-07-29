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
    检查 LLM 是否按格式输出（CHANGES 标签是否完整、正文是否非空）。"""
    issues = []
    if not content or len(content.strip()) < 500:
        issues.append({'gate': 'protocol', 'severity': 'critical',
                       'message': '正文过短（<500字），可能生成失败'})
        return {'passed': False, 'issues': issues}

    # 检查是否有残留的 prompt 标签（LLM 误把指令当输出）
    leaked_tags = re.findall(r'<(?:pre_write_check|chapter_changes|system|user|assistant)>', content)
    if leaked_tags:
        # chapter_changes 标签是正常的（P1-6 产物），只在正文中间出现残留才算问题
        # 这里只检查是否把标签当正文（即标签外无实质内容）
        body = re.sub(r'<chapter_changes>[\s\S]*?</chapter_changes>', '', content)
        body = re.sub(r'<pre_write_check>[\s\S]*?</pre_write_check>', '', body)
        if len(body.strip()) < 500:
            issues.append({'gate': 'protocol', 'severity': 'critical',
                           'message': '正文被标签覆盖，实际内容过少'})

    # 检查是否有 CHANGES 标签（P1-6 启用后应有）
    has_changes = bool(re.search(r'<chapter_changes>', content, re.IGNORECASE))
    if not has_changes:
        issues.append({'gate': 'protocol', 'severity': 'warning',
                       'message': '未输出 chapter_changes 标签，状态回写将跳过'})

    return {'passed': len([i for i in issues if i['severity'] == 'critical']) == 0, 'issues': issues}


def gate_reference_check(content: str, bb) -> Dict:
    """门禁2：引用校验。
    检查正文中提到的人物/地点是否在 bible 中定义（防 LLM 编造实体）。
    简化版：提取正文中的"姓名说/姓名道"模式，校验是否在 character_profiles 中。"""
    issues = []
    if not bb or not content:
        return {'passed': True, 'issues': issues}

    # 提取"XX说""XX道""XX笑"等人物引用
    ref_pattern = re.findall(r'([\u4e00-\u9fa5]{2,4})(?:说|道|笑|怒|惊|叹|问|答|喝)', content)
    refs = set(ref_pattern) if ref_pattern else set()

    if not refs:
        return {'passed': True, 'issues': issues}

    # 从 character_profiles 提取已定义角色名
    defined_chars = set()
    if bb.character_profiles:
        for m in re.finditer(r'##\s*角色[：:]\s*([^\n]+)', bb.character_profiles):
            name = m.group(1).strip().split('（')[0].split('(')[0].strip()
            if name:
                defined_chars.add(name)

    # 检查未定义的引用（只在有定义角色时才校验，避免空 bible 误报）
    if defined_chars:
        undefined = refs - defined_chars
        # 过滤掉常见非人名（如"他们""众人"等）
        stop_words = {'他们', '她们', '众人', '大家', '对方', '自己', '我们', '有人', '那人', '此人', '一人'}
        undefined = {u for u in undefined if u not in stop_words}
        if undefined and len(undefined) <= 5:  # 超过5个可能是误判
            issues.append({'gate': 'reference', 'severity': 'warning',
                           'message': f'可能引用了未定义角色：{", ".join(list(undefined)[:3])}',
                           'undefined': list(undefined)})

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
                           'message': f'本章规划焦点「{focus[:10]}」未在正文中体现'})

    return {'passed': len([i for i in issues if i['severity'] == 'critical']) == 0, 'issues': issues}


def run_all_gates(content: str, bb, chapter_num: int) -> Dict:
    """运行全部3道门禁，返回汇总结果。"""
    results = []
    results.append(gate_protocol_check(content))
    results.append(gate_reference_check(content, bb))
    results.append(gate_blueprint_check(content, bb, chapter_num))

    all_issues = []
    for r in results:
        all_issues.extend(r['issues'])

    critical_count = sum(1 for i in all_issues if i['severity'] == 'critical')
    return {
        'passed': critical_count == 0,
        'critical_count': critical_count,
        'warning_count': sum(1 for i in all_issues if i['severity'] == 'warning'),
        'issues': all_issues,
    }


# ===== P2-11：PRE_WRITE_CHECK 13行表 =====

def build_pre_write_check_prompt(chapter_num: int, bb, dag_hooks: str = '') -> str:
    """构建 PRE_WRITE_CHECK 模板（注入章节 prompt 顶部）。
    要求 LLM 写正文前先输出 13 行意图表，对齐上下文。"""
    return f"""

【写章前·PRE_WRITE_CHECK】（P2-11）
开始写正文前，必须先输出 <pre_write_check> 表格（13行），再写正文：

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

写完表格后，空一行，开始写正文。正文写完后，输出 <chapter_changes> JSON。"""
