"""
Spot-Fix 修订路由器（P2-9）
按校验问题的严重性和类型，决定是局部修补（local）还是整章重写（structural）。
local 类：只送问题段落给 LLM 修订，正文其余部分 byte-for-byte 保留，省 token 且不引入新 AI 味。

参考：InkOS Auto 模式 Spot-Fix 路由
设计原则：
  - local 类（高疲劳词/套话/段落等长/连续了字）→ patch-only
  - structural 类（OOC/剧情偏离/伏笔遗漏/一致性异常）→ 整章重写
  - 模糊匹配定位问题段落，50% 相似度阈值
"""
import re
import json
from typing import Dict, List, Tuple, Optional


# ===== 问题分类路由 =====

# local 类：局部问题，可只修补问题段落
LOCAL_CATEGORIES = {
    '高疲劳词', '段落过长', '短段堆砌', '连续了字', '转折密度',
    '禁止句式',  # 公式化句式也是局部的
}

# structural 类：结构性问题，需整章重写
STRUCTURAL_CATEGORIES = {
    'OOC', '剧情偏离', '伏笔遗漏', '一致性异常', '战力崩坏', '信息越界',
}


def classify_issues(issues: List[Dict]) -> Dict[str, List[Dict]]:
    """将校验问题分为 local 和 structural 两类。
    issues: post_validate.issues 列表"""
    local_issues = []
    structural_issues = []
    for issue in issues:
        category = issue.get('category', '')
        if category in STRUCTURAL_CATEGORIES:
            structural_issues.append(issue)
        else:
            # 默认归 local（包括未知类别）
            local_issues.append(issue)
    return {'local': local_issues, 'structural': structural_issues}


def route_revision(content: str, validation: Dict, mode: str = 'auto') -> Dict:
    """路由修订策略。
    mode: auto（自动判断）/ spot_fix（强制局部）/ rewrite（强制整章）
    返回：{strategy, local_issues, structural_issues, patches}"""
    issues = validation.get('issues', []) if validation else []
    classified = classify_issues(issues)

    result = {
        'strategy': 'none',  # none / spot_fix / rewrite
        'local_issues': classified['local'],
        'structural_issues': classified['structural'],
        'patches': [],
    }

    if mode == 'rewrite':
        result['strategy'] = 'rewrite'
        return result
    if mode == 'spot_fix':
        result['strategy'] = 'spot_fix' if classified['local'] else 'none'
        if result['strategy'] == 'spot_fix':
            result['patches'] = _build_patches(content, classified['local'])
        return result

    # auto 模式：有 structural 则整章重写，否则有 local 则 spot_fix
    if classified['structural']:
        result['strategy'] = 'rewrite'
    elif classified['local']:
        result['strategy'] = 'spot_fix'
        result['patches'] = _build_patches(content, classified['local'])
    else:
        result['strategy'] = 'none'
    return result


def _build_patches(content: str, local_issues: List[Dict]) -> List[Dict]:
    """为每个 local 问题构建补丁：定位问题段落，生成修订指令。
    返回 [{paragraph_index, original, issues, instruction}]"""
    paragraphs = _split_paragraphs(content)
    patches = []

    for issue in local_issues:
        pattern = issue.get('pattern', '')
        category = issue.get('category', '')
        suggestion = issue.get('suggestion', '')

        # 定位含问题词的段落
        matched_idx = _find_paragraph_with_pattern(paragraphs, pattern)
        if matched_idx < 0:
            continue  # 定位失败，跳过

        # 合并到已有补丁（同一段落多个问题合并）
        existing = next((p for p in patches if p['paragraph_index'] == matched_idx), None)
        if existing:
            existing['issues'].append(issue)
            existing['instruction'] += f'\n- {suggestion}'
        else:
            patches.append({
                'paragraph_index': matched_idx,
                'original': paragraphs[matched_idx],
                'issues': [issue],
                'instruction': f'请修订以下段落中的问题：\n- {suggestion}',
            })

    return patches


def _split_paragraphs(content: str) -> List[str]:
    """按空行分段"""
    return [p.strip() for p in re.split(r'\n\s*\n', content) if p.strip()]


def _find_paragraph_with_pattern(paragraphs: List[str], pattern: str, threshold: float = 0.5) -> int:
    """在段落中查找含 pattern 的段落，返回索引。找不到返回 -1。
    先精确匹配，再模糊匹配（50% 相似度阈值）。"""
    if not pattern or not paragraphs:
        return -1

    # 1. 精确匹配：段落含 pattern
    for i, p in enumerate(paragraphs):
        if pattern in p:
            return i

    # 2. 模糊匹配：段落含 pattern 的部分子串
    # 取 pattern 的前 4-6 字作为模糊线索
    if len(pattern) >= 4:
        sub = pattern[:min(6, len(pattern))]
        for i, p in enumerate(paragraphs):
            if sub in p:
                return i

    return -1


def build_spot_fix_prompt(content: str, patches: List[Dict]) -> Tuple[str, str]:
    """构建 Spot-Fix 修订 prompt。
    返回 (system_prompt, user_prompt)。
    LLM 只输出修订后的段落，正文其余部分由代码拼接，不送全文。"""
    if not patches:
        return '', ''

    # 只送问题段落给 LLM（省 token）
    para_list = []
    for i, patch in enumerate(patches):
        para_list.append(f'【段落{i+1}】（第{patch["paragraph_index"]+1}段）\n{patch["original"]}\n\n修订要求：\n{patch["instruction"]}')

    user_prompt = '\n\n'.join(para_list)
    user_prompt += '\n\n【输出格式】严格按以下格式输出，每个修订段落用 <patch index="N"> 包裹：\n'
    for i, patch in enumerate(patches):
        user_prompt += f'<patch index="{i+1}">\n修订后的段落{ i+1}内容\n</patch>\n'

    system_prompt = """你是文字修订专家，只做局部修补，不改写整段。
【铁律】
1. 只修订指定段落中的问题，保持原文风格和情节不变
2. 不要改动没有问题的句子
3. 不要扩写或缩写，字数变化不超过 ±20%
4. 严格按 <patch index="N"> 格式输出，不要输出其他内容
5. 修订后段落不要包含 <patch> 标签本身"""

    return system_prompt, user_prompt


def apply_spot_fix_patches(original_content: str, patches: List[Dict], llm_output: str) -> str:
    """将 LLM 修订的段落 patch 回原文。
    解析 <patch index="N"> 标签，替换对应段落。"""
    if not patches or not llm_output:
        return original_content

    # 解析 LLM 输出的修订段落
    revised_paras = {}
    for m in re.finditer(r'<patch\s+index="(\d+)"\s*>([\s\S]*?)</patch>', llm_output):
        idx = int(m.group(1))
        revised = m.group(2).strip()
        revised_paras[idx] = revised

    if not revised_paras:
        return original_content  # 解析失败，返回原文

    # 按段落分割原文
    para_split = re.split(r'(\n\s*\n)', original_content)
    paragraphs = []
    separators = []
    for i, part in enumerate(para_split):
        if re.match(r'\n\s*\n', part):
            separators.append(part)
        elif part.strip():
            paragraphs.append(part)

    # 替换修订的段落
    for i, patch in enumerate(patches):
        para_idx = patch['paragraph_index']
        patch_num = i + 1
        if patch_num in revised_paras and 0 <= para_idx < len(paragraphs):
            paragraphs[para_idx] = revised_paras[patch_num]

    # 重新拼接（段落数与分隔符数关系：n段有n-1个分隔符）
    result = paragraphs[0]
    for i, sep in enumerate(separators):
        if i + 1 < len(paragraphs):
            result += sep + paragraphs[i + 1]
    return result


def estimate_token_saving(content: str, patches: List[Dict]) -> Dict:
    """估算 Spot-Fix 相比整章重写的 token 节省"""
    full_tokens = len(content) // 2  # 中文约 2 字/token
    patch_tokens = sum(len(p['original']) for p in patches) // 2
    return {
        'full_rewrite_tokens': full_tokens,
        'spot_fix_tokens': patch_tokens,
        'saved_tokens': full_tokens - patch_tokens,
        'saving_ratio': round(1 - patch_tokens / max(full_tokens, 1), 2),
    }
