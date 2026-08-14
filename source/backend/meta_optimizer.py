# -*- coding: utf-8 -*-
"""自我进化层（M4）：FailureDB + Meta-LLM prompt 优化

核心流程：
  1. 各维度生成/校验出错时，按 6 类问题写入 BookBible.failure_log_json
  2. 累积到一定数量（如 10 条）或用户触发时，调用 Meta-LLM 分析高频模式
  3. 输出可执行的 prompt 改进建议（不改原始模板，只生成"覆盖补丁"）
  4. 前端可查看"系统建议"并一键采纳

6 类问题：
  format:     格式错（JSON/Markdown/字段缺失）
  structure:  结构错（卷数/章号/节点范围）
  content:    内容错（OOC/时间穿越/矛盾）
  mission:    任务遗漏（该埋/收的伏笔没做）
  conflict:   维度冲突（改了 A 没同步 B）
  entity:     实体漂移（未注册人名/地名）
"""
import json
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from datetime import datetime, timezone


FAILURE_CATEGORIES = ['format', 'structure', 'content', 'mission', 'conflict', 'entity']


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


class MetaPromptOptimizer:
    """分析 failure_log，输出 prompt 改进建议"""

    @staticmethod
    def analyze(records: List[FailureRecord], min_count: int = 3) -> Dict:
        if len(records) < min_count:
            return {'ready': False, 'reason': f'当前失败记录 {len(records)} 条，未达到分析阈值 {min_count}', 'suggestions': []}

        # 按 category + dim_key 聚合
        buckets: Dict[str, List[FailureRecord]] = {}
        for r in records:
            key = f'{r.category}::{r.dim_key}'
            buckets.setdefault(key, []).append(r)

        suggestions = []
        for key, items in buckets.items():
            if len(items) < 2:
                continue
            cat, dim = key.split('::', 1)
            recent = sorted(items, key=lambda x: x.ts, reverse=True)[:5]
            suggestions.append({
                'category': cat,
                'dim_key': dim,
                'count': len(items),
                'pattern': recent[0].summary,
                'suggestion': recent[0].fix_hint or MetaPromptOptimizer._default_fix_hint(cat),
                'examples': [{'summary': r.summary, 'snippet': r.snippet} for r in recent[:3]],
            })

        suggestions.sort(key=lambda x: -x['count'])
        return {
            'ready': True,
            'total_records': len(records),
            'suggestions': suggestions,
        }

    @staticmethod
    def _default_fix_hint(category: str) -> str:
        hints = {
            'format': '在 prompt 末尾追加："严格输出纯文本/JSON，禁止 Markdown 代码块"',
            'structure': '在 prompt 中增加章节号/卷数校验示例，让 LLM 自洽',
            'content': '在 prompt 中注入 POV 限制 + 时间线检查清单',
            'mission': '在 prompt 开头加入"本章必须执行的任务清单"并强制勾选',
            'conflict': '在 prompt 中加入"改动前请读取相关维度并声明影响"',
            'entity': '在 prompt 中加入"出现新人物/地名时必须先注册到 EntityHub"',
        }
        return hints.get(category, '请检查 prompt 并补充约束')

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
    """返回给前端的"系统学习报告""""
    records = FailureDB.load(bb)
    analysis = MetaPromptOptimizer.analyze(records, min_count=3)
    return {
        'failure_count': len(records),
        'ready': analysis.get('ready', False),
        'suggestions': analysis.get('suggestions', []),
    }
