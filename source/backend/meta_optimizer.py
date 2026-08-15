# -*- coding: utf-8 -*-
"""自我进化层（M4）：FailureDB + Meta-LLM prompt 优化

核心流程：
  1. 各维度生成/校验出错时，按 6 类问题写入 BookBible.failure_log_json
  2. 累积到一定数量（如 10 条）或用户触发时，调用 Meta-LLM 分析高频模式
  3. 输出可执行的 prompt 改进建议（不改原始模板，只生成"覆盖补丁"）
  4. 前端可查看"系统建议"并一键采纳 / 忽略
     - 采纳：patch 文本写进 BookBible.prompt_patches_json，后续所有 system prompt 末尾自动追加
     - 忽略：bucket_key 写进 BookBible.ignored_failure_buckets_json，不再出现在建议列表

6 类问题：
  format:     格式错（JSON/Markdown/字段缺失）
  structure:  结构错（卷数/章号/节点范围）
  content:    内容错（OOC/时间穿越/矛盾）
  mission:    任务遗漏（该埋/收的伏笔没做）
  conflict:   维度冲突（改了 A 没同步 B）
  entity:     实体漂移（未注册人名/地名）
"""
import json
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone


FAILURE_CATEGORIES = ['format', 'structure', 'content', 'mission', 'conflict', 'entity']

FAILURE_CN_LABEL = {
    'format': '格式错',
    'structure': '结构错',
    'content': '内容错（OOC/矛盾）',
    'mission': '伏笔/任务遗漏',
    'conflict': '维度冲突',
    'entity': '实体漂移（未注册）',
}


@dataclass
class FailureRecord:
    id: str
    ts: str
    category: str
    dim_key: str
    chapter_num: int
    summary: str
    snippet: str
    fix_hint: str = ''

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'FailureRecord':
        return cls(**{k: data.get(k, '') for k in ['id', 'ts', 'category', 'dim_key', 'chapter_num', 'summary', 'snippet', 'fix_hint']})


class FailureDB:
    @staticmethod
    def load(bb) -> List[FailureRecord]:
        if not bb or not getattr(bb, 'failure_log_json', None):
            return []
        try:
            arr = json.loads(bb.failure_log_json)
            if isinstance(arr, list):
                return [FailureRecord.from_dict(x) for x in arr if isinstance(x, dict)]
        except Exception:
            pass
        return []

    @staticmethod
    def save(bb, records: List[FailureRecord]):
        if not bb:
            return
        bb.failure_log_json = json.dumps([r.to_dict() for r in records], ensure_ascii=False)

    @staticmethod
    def append(bb, category: str, dim_key: str = '', chapter_num: int = 0,
               summary: str = '', snippet: str = '', fix_hint: str = ''):
        if category not in FAILURE_CATEGORIES:
            category = 'content'
        records = FailureDB.load(bb)
        rid = f'f{len(records)+1:04d}_{int(datetime.now(timezone.utc).timestamp())}'
        records.append(FailureRecord(
            id=rid,
            ts=datetime.now(timezone.utc).isoformat(),
            category=category,
            dim_key=dim_key,
            chapter_num=chapter_num,
            summary=summary,
            snippet=snippet[:300],
            fix_hint=fix_hint,
        ))
        FailureDB.save(bb, records)


# ---------- Prompt 补丁存储 ----------

def _safe_load_json(txt, default):
    try:
        v = json.loads(txt or '')
        if isinstance(v, type(default)):
            return v
    except Exception:
        pass
    return default


def get_prompt_patches(bb) -> List[Dict[str, Any]]:
    """BookBible.prompt_patches_json：用户已采纳的 prompt 补丁列表"""
    if not bb:
        return []
    return _safe_load_json(getattr(bb, 'prompt_patches_json', '') or '[]', [])


def add_prompt_patch(bb, category: str, dim_key: str, patch_text: str,
                     bucket_key: str = '') -> Dict[str, Any]:
    """保存一条新补丁。返回补丁对象。"""
    patches = get_prompt_patches(bb)
    pid = f'p{len(patches)+1:03d}_{int(datetime.now(timezone.utc).timestamp())}'
    obj = {
        'id': pid,
        'category': category,
        'dim_key': dim_key or '',
        'bucket_key': bucket_key or f'{category}::{dim_key or ""}',
        'patch_text': (patch_text or '').strip(),
        'applied_at': datetime.now(timezone.utc).isoformat(),
    }
    patches.append(obj)
    bb.prompt_patches_json = json.dumps(patches, ensure_ascii=False)
    # 采纳后对应 bucket 自动标记为"已处理→不再提示"（等价于 ignore）
    if bucket_key:
        add_ignored_bucket(bb, bucket_key)
    return obj


def build_active_patch_text(bb) -> str:
    """把所有已采纳补丁拼成一段，追加到 system prompt 末尾。空时返回 ''。"""
    patches = get_prompt_patches(bb)
    if not patches:
        return ''
    lines = ['\n\n## 🔧 用户定制·铁律补丁（从失败记录中学习并采纳）']
    for i, p in enumerate(patches, 1):
        cat = FAILURE_CN_LABEL.get(p.get('category', ''), p.get('category', ''))
        lines.append(f'### 补丁{i}（{cat or "通用"}）')
        lines.append((p.get('patch_text') or '').strip())
    lines.append('以上补丁优先级最高，与其他规则冲突时以补丁为准。')
    return '\n'.join(lines)


# ---------- 忽略的失败 bucket ----------

def get_ignored_buckets(bb) -> List[str]:
    if not bb:
        return []
    arr = _safe_load_json(getattr(bb, 'ignored_failure_buckets_json', '') or '[]', [])
    return [x for x in arr if isinstance(x, str)]


def add_ignored_bucket(bb, bucket_key: str):
    if not bucket_key:
        return
    lst = get_ignored_buckets(bb)
    if bucket_key in lst:
        return
    lst.append(bucket_key)
    bb.ignored_failure_buckets_json = json.dumps(lst, ensure_ascii=False)


def remove_ignored_bucket(bb, bucket_key: str):
    lst = get_ignored_buckets(bb)
    if bucket_key in lst:
        lst.remove(bucket_key)
        bb.ignored_failure_buckets_json = json.dumps(lst, ensure_ascii=False)


class MetaPromptOptimizer:
    """分析 failure_log，输出 prompt 改进建议"""

    @staticmethod
    def analyze(records: List[FailureRecord], min_count: int = 3,
                ignored_buckets: Optional[List[str]] = None) -> Dict:
        ignored = set(ignored_buckets or [])
        if len(records) < min_count:
            return {
                'ready': False,
                'reason': f'当前失败记录 {len(records)} 条，未达到分析阈值 {min_count}。多跑几次校审/章节生成后会自动出现建议。',
                'suggestions': [],
            }

        # 按 category + dim_key 聚合
        buckets: Dict[str, List[FailureRecord]] = {}
        for r in records:
            key = f'{r.category}::{r.dim_key}'
            buckets.setdefault(key, []).append(r)

        suggestions = []
        for key, items in buckets.items():
            if key in ignored:
                continue
            if len(items) < 2:
                continue
            cat, dim = key.split('::', 1)
            recent = sorted(items, key=lambda x: x.ts, reverse=True)[:5]
            cat_cn = FAILURE_CN_LABEL.get(cat, cat)
            base_suggestion = recent[0].fix_hint or MetaPromptOptimizer._default_fix_hint(cat)
            # 生成一段可直接"复制到 prompt 末尾"的默认补丁文本（短、强约束、可执行）
            default_patch = base_suggestion
            if not default_patch.startswith('在') and '：' not in default_patch[:5]:
                default_patch = f'【{cat_cn}补丁】{default_patch}'
            suggestions.append({
                'bucket_key': key,                 # 用于 adopt/dismiss 索引
                'category': cat,
                'category_cn': cat_cn,
                'dim_key': dim,
                'count': len(items),
                # 兼容旧字段名（前端原来用的）
                'problem_pattern': recent[0].summary,
                'pattern': recent[0].summary,
                'affected_dims': [dim] if dim else [],
                'proposed_patch': default_patch,
                'suggestion': base_suggestion,
                'sample_snippet': recent[0].snippet,
                'examples': [{'summary': r.summary, 'snippet': r.snippet, 'chapter_num': r.chapter_num, 'ts': r.ts}
                             for r in recent[:3]],
            })

        suggestions.sort(key=lambda x: -x['count'])
        return {
            'ready': True,
            'total_records': len(records),
            'ignored_count': len(ignored),
            'suggestions': suggestions,
            # 使用说明（首次无数据时前端也可显示）
            'how_to_use': {
                'step1': '数据来源：跑校审（防遗忘/一致性）、门禁校验、或章节生成后 PostGenValidator 检测到问题，错误会自动写入 FailureDB',
                'step2': '出建议阈值：同类问题累积 ≥ 2 条 → 生成一条优化建议（同一模式出现越多，建议可信度越高）',
                'step3': '使用建议：点「✅ 采纳建议」→ 建议文字会作为"铁律补丁"追加到系统 prompt 末尾，生成本书任何维度/章节时都会带上',
                'step4': '自定义：点「📝 自定义编辑」→ 可改补丁文字后再采纳；不认可建议 → 点「❌ 忽略」不再提示',
            },
        }

    @staticmethod
    def _default_fix_hint(category: str) -> str:
        hints = {
            'format': '铁律：输出严格使用指定格式（纯文本/JSON）。**禁止使用 ```markdown 代码块包裹、禁止加"下面是..."等寒暄、禁止输出字段说明**。违反此条直接扣分。',
            'structure': '铁律：先检查本次任务涉及的卷数/章号/顺序范围，输出时**严格对齐**，不得出现章节号跳跃、卷号错误、或"在 N 章之前"与实际时间线矛盾。',
            'content': '铁律：写作/检查前先读相关维度（人物档案/设定/时间线）。**POV 视角只写该视角能知道的信息**；时间线按时间推进，不出现穿越/回流；同一人物不得出现前后矛盾的性格、立场、能力。',
            'mission': '铁律：生成前先罗列「本章必须执行的任务清单」（含：该埋哪条伏笔、该回收哪条伏笔、该推进哪条主线支线），生成后**逐条打勾**确认全部完成，遗漏立即补写。',
            'conflict': '铁律：修改任何维度内容前，**先读取所有会被改动或可能冲突的维度**，并在输出开头声明「本次改动影响 X 维度，已读取它们的最新值」。不得单方面改 A 而不同步 B。',
            'entity': '铁律：首次出现新的人名/地名/功法/法宝/势力名时，**必须先到实体注册表注册，带上 1-2 句核心说明**，之后再使用；已经注册过的实体，严格沿用既有设定，不能漂移。',
        }
        return hints.get(category, '铁律：请严格对齐设定/时间线/实体注册。若出现同类问题三次以上，请联系管理员补充 prompt 约束。')

    @staticmethod
    def build_patch_prompt(records: List[FailureRecord], current_tail_rule: str = '') -> str:
        """生成给 Meta-LLM 的 prompt，请求输出一段可直接追加的 prompt 补丁"""
        analysis = MetaPromptOptimizer.analyze(records, min_count=1)
        sug_text = json.dumps(analysis.get('suggestions', [])[:5], ensure_ascii=False, indent=2)
        return f"""你是 prompt 工程专家。请根据以下小说 AI 创作平台的失败记录，输出一段可以直接追加到 system prompt 末尾的"铁律补丁"。

要求：
1. 只输出补丁文本（不要解释、不要 JSON）
2. 补丁要针对高频失败模式，措辞强烈、具体、可执行
3. 补丁长度控制在 300 字以内
4. 如果失败主要是格式问题，补丁必须包含具体反例

失败记录摘要：
{sug_text}

当前已有铁律：
{current_tail_rule or '（无）'}

请输出补丁："""


# ---------- 便捷函数：在 PostGenValidator / 一致性检查失败时调用 ----------

def log_failure(bb, category: str, dim_key: str = '', chapter_num: int = 0,
                summary: str = '', snippet: str = '', fix_hint: str = ''):
    """记录一次失败/错例"""
    FailureDB.append(bb, category, dim_key, chapter_num, summary, snippet, fix_hint)


def get_optimization_report(bb) -> Dict:
    """返回给前端的系统学习报告。

    同时附加：
    - how_to_use：使用说明（空数据时也给前端显示）
    - applied_patches：用户已采纳的补丁列表（UI 展示"已采纳 X 条"）
    """
    records = FailureDB.load(bb)
    ignored = get_ignored_buckets(bb)
    analysis = MetaPromptOptimizer.analyze(records, min_count=3, ignored_buckets=ignored)
    applied = get_prompt_patches(bb)
    result = {
        'failure_count': len(records),
        'ignored_bucket_count': len(ignored),
        'ready': analysis.get('ready', False),
        'suggestions': analysis.get('suggestions', []),
        'reason': analysis.get('reason', ''),
        'how_to_use': analysis.get('how_to_use') or {
            'step1': '数据来源：跑校审（防遗忘/一致性）、门禁校验、章节生成后校验，错误会自动写入 FailureDB',
            'step2': '出建议阈值：同类问题累积 ≥ 2 条 → 生成一条优化建议',
            'step3': '✅ 采纳 → 补丁追加到系统 prompt 末尾，以后生成本书任何维度都会带上',
            'step4': '❌ 忽略 → 不再提示；📝 自定义 → 编辑补丁文字后再采纳',
        },
        'applied_patches': [
            {
                'id': p.get('id'),
                'category': p.get('category', ''),
                'category_cn': FAILURE_CN_LABEL.get(p.get('category', ''), p.get('category', '')),
                'patch_text': p.get('patch_text', ''),
                'applied_at': p.get('applied_at', ''),
            } for p in applied
        ],
        'applied_patch_count': len(applied),
    }
    # 让上层也能直接拿到拼接好的 patch_text（便于调试/预览）
    active_patch = build_active_patch_text(bb)
    result['active_patch_preview'] = active_patch[:500] if active_patch else ''
    return result
