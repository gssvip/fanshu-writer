# -*- coding: utf-8 -*-
"""上下文相关性加权裁剪（Relevance-Weighted Context Pruning）

为正文写作场景按相关性加权排序裁剪上下文，避免长篇时低相关性内容膨胀占满 token。

接入点：_action_chapter 正文写作时组装 ctx_blocks 后调用 rank_for_chapter。
"""
from dataclasses import dataclass, field
from typing import List, Optional
import re


@dataclass
class ContextChunk:
    dim_key: str
    label: str
    content: str
    relevance: float = 0.0    # 0.0-1.0，相关性分数
    priority: int = 2          # 1=高(必须保留) 2=中(尽量保留) 3=低(可裁剪)


class ContextRanker:
    """按相关性加权裁剪上下文，长篇时智能保留高相关内容"""

    # 维度基础优先级（正文写作场景）
    # 1 = 高优先：题材/文风/POV人物/本卷剧情必须保留
    # 2 = 中优先：总纲/设定/世界观可裁剪
    # 3 = 低优先：地图/伏笔可裁剪
    BASE_PRIORITY = {
        'concept': 1, 'style_guide': 1,
        'character_profiles': 1,
        'timeline': 1,
        'plot_design': 2,
        'key_rules': 2, 'worldbuilding': 2,
        'locations': 3, 'foreshadowing': 3,
    }

    def __init__(self, max_tokens: int = 4000):
        self.max_tokens = max(500, int(max_tokens))

    def rank_for_chapter(self, chunks: List[ContextChunk], target_chapter_num: int = 0,
                         pov_name: Optional[str] = None, book_id: Optional[str] = None) -> List[ContextChunk]:
        """为正文写作排序裁剪上下文

        Args:
            chunks: 待排序的上下文块列表
            target_chapter_num: 当前要写的章节号
            pov_name: 当前章节视点人物名
            book_id: 书籍 ID（用于动态文件相关性查询，当前简化实现未用）
        Returns:
            裁剪后的 chunks 列表（已按重要性排序，低优先级内容被摘要或裁剪）
        """
        if not chunks:
            return []

        # 1. 计算每个 chunk 的相关性分数
        for c in chunks:
            c.relevance = self._compute_relevance(c, target_chapter_num, pov_name)

        # 2. 按综合分数排序（高分在前）
        chunks.sort(key=lambda c: -self._score(c))

        # 3. 贪心填充到 token 上限
        result: List[ContextChunk] = []
        used = 0
        for c in chunks:
            tok = self._estimate_tokens(c.content)
            if tok == 0:
                continue
            # 超限且非高优先：裁剪到摘要
            if used + tok > self.max_tokens and c.priority > 1:
                remaining = max(0, self.max_tokens - used)
                if remaining < 100:
                    # 剩余空间太小，直接跳过低优先
                    if c.priority >= 3:
                        continue
                c.content = self._summarize(c.content, max_chars=remaining * 2)
                tok = self._estimate_tokens(c.content)
                if tok == 0:
                    continue
            result.append(c)
            used += tok
            if used >= self.max_tokens:
                break
        return result

    def _score(self, c: ContextChunk) -> float:
        """综合分数 = 优先级权重(0.6) + 相关性权重(0.4)"""
        prio_weight = {1: 1.0, 2: 0.5, 3: 0.2}.get(c.priority, 0.5)
        return prio_weight * 0.6 + c.relevance * 0.4

    def _compute_relevance(self, c: ContextChunk, ch_num: int, pov_name: Optional[str]) -> float:
        """计算相关性分数（0.0-1.0）"""
        score = 0.0
        if not c.content:
            return 0.0
        # POV 人物相关：人物档案中包含 POV 名字加权
        if c.dim_key == 'character_profiles' and pov_name and pov_name in c.content:
            score += 0.5
        # 当前章节号相关：timeline 中包含当前章号加权
        if c.dim_key == 'timeline' and ch_num:
            # 检测 nodes.chapters 字段是否含当前章号
            if re.search(rf'\b{ch_num}\b', c.content):
                score += 0.6
        # 伏笔：当前章号附近可能埋设/回收
        if c.dim_key == 'foreshadowing' and ch_num:
            score += 0.3
        # 高优先维度基础分
        if c.priority == 1:
            score += 0.2
        return min(1.0, score)

    def _estimate_tokens(self, text: str) -> int:
        """粗估 token 数（中文约 2 字/token）"""
        if not text:
            return 0
        return max(1, len(text) // 2)

    def _summarize(self, content: str, max_chars: int) -> str:
        """保留开头+结尾的摘要裁剪"""
        if max_chars <= 0 or not content:
            return ''
        if len(content) <= max_chars:
            return content
        # 保留前 70% + 末尾 30%（开头通常含设定概要，末尾含近期动态）
        head_size = int(max_chars * 0.7)
        tail_size = max_chars - head_size
        head = content[:head_size]
        tail = content[-tail_size:] if tail_size > 0 else ''
        sep = '\n…（已省略中间部分，仅保留关键设定与近期动态）…\n'
        return f'{head}{sep}{tail}'
