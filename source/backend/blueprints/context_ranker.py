# -*- coding: utf-8 -*-
"""上下文相关性加权裁剪（Relevance-Weighted Context Pruning）

为正文写作场景按相关性加权排序裁剪上下文，避免长篇时低相关性内容膨胀占满 token。

接入点：_action_chapter 正文写作时组装 ctx_blocks 后调用 rank_for_chapter。
"""
from dataclasses import dataclass, field
from typing import List, Optional
import re


# M3: 简单 ContextBus 查询封装（可被 _action_chapter 直接调用）
class ContextBus:
    """跨引擎上下文查询：从 EventLog / ForeshadowingGraph / EntityRegistry 按需拉取。"""

    @staticmethod
    def get_event_log_snippets(bb, before_chapter: int, limit: int = 5) -> str:
        """返回前情提要文本（优先上一章，再往前最近几条）。"""
        if not bb or not bb.event_log_json:
            return ''
        try:
            from event_log_manager import EventLogManager
            events = EventLogManager.load(bb)
            prev = [e for e in events if e.chapter_num == before_chapter - 1]
            recent = sorted([e for e in events if e.chapter_num < before_chapter - 1],
                            key=lambda x: x.chapter_num, reverse=True)[:limit]
            selected = (prev + recent)[:limit]
            if not selected:
                return ''
            lines = ['【前情提要·事件序列】']
            for e in sorted(selected, key=lambda x: x.chapter_num):
                actors = '、'.join(e.actors) if e.actors else '（无）'
                loc = f'｜地点：{e.location}' if e.location else ''
                lines.append(f'· 第{e.chapter_num}章｜{e.type}｜{actors}{loc}｜{e.summary}')
            return '\n'.join(lines)
        except Exception:
            return ''

    @staticmethod
    def get_hook_mission(bb, chapter_num: int) -> str:
        """返回本章伏笔任务清单（应埋/应收/禁揭示）。"""
        if not bb or not bb.foreshadowing_graph or not chapter_num:
            return ''
        try:
            from foreshadowing_manager import ForeshadowingGraph
            graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
            hooks = graph.get_nodes_for_chapter(chapter_num)
            lines = []
            if hooks.get('setup'):
                lines.append('【本章必须埋设的伏笔】')
                for n in hooks['setup']:
                    lines.append(f'  · {n.id}｜{n.content}（计划{n.target_chapter}回收，权重{n.weight}）')
            if hooks.get('payoff'):
                lines.append('【本章应当回收的伏笔】')
                for n in hooks['payoff']:
                    dep = f'（依赖 {",".join(n.depends_on)}）' if n.depends_on else ''
                    lines.append(f'  · {n.id}｜{n.content}{dep}')
            # 禁揭示：未收核心伏笔且非本章应收
            payoff_ids = {n.id for n in hooks.get('payoff', [])}
            forbidden = [n for n in graph.get_pending_nodes(min_weight=7) if n.id not in payoff_ids]
            if forbidden:
                lines.append('【本章严禁揭示谜底的核心伏笔（只能给线索）】')
                for n in forbidden[:5]:
                    lines.append(f'  · {n.id}｜{n.content[:60]}')
            return '\n'.join(lines)
        except Exception:
            return ''

    @staticmethod
    def get_pov_context(book_id, bb, chapter_num: int) -> dict:
        """返回 POV 人物上下文：关系网 + 最近事件。"""
        result = {'pov_name': '', 'relations': '', 'recent_events': ''}
        try:
            from app import Character
            from entity_registry import _load_registry
            # 简化：取主角作为 POV 兜底
            protagonist = Character.query.filter_by(book_id=book_id, role='protagonist').first()
            if not protagonist:
                return result
            result['pov_name'] = protagonist.name
            # 关系网
            try:
                rels = json.loads(protagonist.relationships_json or '[]')
                if rels:
                    result['relations'] = '、'.join([
                        f"{r.get('target_name') or r.get('name') or r.get('with') or '?'}"
                        for r in rels if isinstance(r, dict)
                    ])
            except Exception:
                pass
            # 最近涉及 POV 的事件
            if bb and bb.event_log_json:
                from event_log_manager import EventLogManager
                events = EventLogManager.load(bb)
                pov_events = [e for e in events if protagonist.name in e.actors and e.chapter_num < chapter_num]
                pov_events.sort(key=lambda x: x.chapter_num, reverse=True)
                if pov_events[:3]:
                    result['recent_events'] = '\n'.join([
                        f"· 第{e.chapter_num}章｜{e.summary}" for e in pov_events[:3]
                    ])
        except Exception:
            pass
        return result


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
    # 1 = 高优先：题材/文风/POV人物/本卷剧情/前情提要必须保留
    # 2 = 中优先：总纲/设定/世界观/伏笔可裁剪
    # 3 = 低优先：地图可裁剪
    BASE_PRIORITY = {
        'concept': 1, 'style_guide': 1,
        'character_profiles': 1,
        'timeline': 1,
        'event_log': 1,  # M3: 前情提要高优先
        'plot_design': 2,
        'key_rules': 2, 'worldbuilding': 2,
        'foreshadowing': 2,  # M3: 伏笔升级到中优先（按章号过滤后通常很短）
        'locations': 3,
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
        """M3 升级：动态加权（POV、章号窗口、伏笔未收、事件新鲜度）。"""
        score = 0.0
        if not c.content:
            return 0.0

        # POV 人物相关：人物档案/事件/伏笔中含 POV 名字加权
        if pov_name and pov_name in c.content:
            if c.dim_key == 'character_profiles':
                score += 0.6
            elif c.dim_key in ('event_log', 'foreshadowing', 'dynamic'):
                score += 0.4

        # 章号窗口相关：内容中提到的章节号越接近当前章，越相关
        if ch_num:
            mentioned = self._extract_chapter_numbers(c.content)
            if mentioned:
                # 找最小距离
                min_gap = min(abs(x - ch_num) for x in mentioned)
                if min_gap == 0:
                    score += 0.7
                elif min_gap <= 2:
                    score += 0.5
                elif min_gap <= 5:
                    score += 0.3
                elif min_gap <= 10:
                    score += 0.1

        # 事件新鲜度：EventLog 里最近 5 章的事件加权
        if c.dim_key == 'event_log' and ch_num:
            mentioned = self._extract_chapter_numbers(c.content)
            fresh = [x for x in mentioned if 0 < ch_num - x <= 5]
            if fresh:
                score += 0.5

        # 伏笔：未收/待收状态额外加权（文本里有"待回收"等词）
        if c.dim_key == 'foreshadowing':
            if '应' in c.content or '必须' in c.content or '本章' in c.content:
                score += 0.4
            if '待收' in c.content or '待回收' in c.content:
                score += 0.2

        # 高优先维度基础分
        if c.priority == 1:
            score += 0.2
        return min(1.0, score)

    def _extract_chapter_numbers(self, text: str) -> List[int]:
        """从文本中提取所有章号（支持"第X章"和单独数字）。"""
        nums = []
        if not text:
            return nums
        # 第N章
        for m in re.finditer(r'第\s*(\d+)\s*章', text):
            nums.append(int(m.group(1)))
        return nums

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
