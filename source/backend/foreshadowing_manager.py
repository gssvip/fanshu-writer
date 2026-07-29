"""
伏笔 DAG 管理器（P0-2）
将伏笔从"文本列表"升级为"有向无环图"，支持依赖关系、状态机、权重分层、逾期检测。

参考：Openwrite foreshadowing_manager + InkOS hook-promotion
设计原则：
  - 与现有 foreshadowing 文本字段并存：文本字段供前端展示，DAG 供后端逻辑用
  - 老书兼容：首次访问时自动从文本解析建图
  - 章节生成时查询本章应埋/应收的伏笔，注入 prompt
"""
import json
import re
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict


# ===== 数据模型 =====

@dataclass
class ForeshadowingNode:
    """伏笔节点"""
    id: str                          # 伏笔ID，如 f001
    content: str                     # 伏笔内容描述
    weight: int = 5                  # 权重 1-10，主线建议 ≥7
    layer: str = '支线'              # 主线/支线/彩蛋
    status: str = '埋伏'             # 埋伏/待收/已收/废弃
    created_at: str = ''             # 创建位置（章号或卷号）
    target_chapter: str = ''         # 预期回收章号
    depends_on: List[str] = field(default_factory=list)  # 依赖的伏笔ID列表
    core_hook: bool = False          # 是否主线承重伏笔
    promoted: bool = False           # 是否升级为活跃伏笔（影响审计噪音）
    tags: List[str] = field(default_factory=list)        # 标签
    payoff_chapter: str = ''         # 实际回收章号（已收时填）

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ForeshadowingEdge:
    """伏笔边（依赖关系）"""
    from_id: str    # 源伏笔ID
    to_id: str      # 目标（伏笔ID或回收点）
    edge_type: str = '依赖'  # 依赖/强化/反转


class ForeshadowingGraph:
    """伏笔 DAG 图"""

    def __init__(self):
        self.nodes: Dict[str, ForeshadowingNode] = {}
        self.edges: List[ForeshadowingEdge] = []

    def add_node(self, node: ForeshadowingNode):
        self.nodes[node.id] = node
        # 自动补全 promoted：主线承重 或 有依赖 即升级
        if not node.promoted:
            node.promoted = node.core_hook or bool(node.depends_on)

    def add_edge(self, edge: ForeshadowingEdge):
        self.edges.append(edge)

    def validate(self) -> List[str]:
        """校验 DAG 完整性：环检测 + 孤立边检测。返回错误信息列表。"""
        errors = []

        # 1. 环检测（DFS）
        visited = set()
        rec_stack = set()

        def _dfs(node_id: str, path: List[str]) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)
            # 找所有从 node_id 出发的边
            for edge in self.edges:
                if edge.from_id == node_id and edge.to_id in self.nodes:
                    if edge.to_id not in visited:
                        if _dfs(edge.to_id, path):
                            return True
                    elif edge.to_id in rec_stack:
                        path.append(edge.to_id)
                        errors.append(f'检测到环: 涉及节点 {" → ".join(path)}')
                        return True
            rec_stack.discard(node_id)
            path.pop()
            return False

        for nid in self.nodes:
            if nid not in visited:
                _dfs(nid, [])

        # 2. 孤立边检测（边引用了不存在的源节点）
        for edge in self.edges:
            if edge.from_id not in self.nodes:
                errors.append(f'边引用了不存在的源节点: {edge.from_id}')

        return errors

    def get_pending_nodes(self, min_weight: int = 0, layer: str = '') -> List[ForeshadowingNode]:
        """查询待回收伏笔（status 为 埋伏/待收）"""
        result = []
        for node in self.nodes.values():
            if node.status not in ('埋伏', '待收'):
                continue
            if node.weight < min_weight:
                continue
            if layer and node.layer != layer:
                continue
            result.append(node)
        # 按权重降序
        result.sort(key=lambda n: n.weight, reverse=True)
        return result

    def get_nodes_for_chapter(self, chapter_num: int) -> Dict[str, List[ForeshadowingNode]]:
        """查询本章应埋/应收的伏笔。
        返回 {'setup': [...], 'payoff': [...]}"""
        ch_str = str(chapter_num)
        setup_list = []
        payoff_list = []
        for node in self.nodes.values():
            # 本章应埋设：created_at 匹配本章
            if node.created_at and _chapter_match(node.created_at, chapter_num):
                setup_list.append(node)
            # 本章应回收：target_chapter 匹配本章
            if node.target_chapter and _chapter_match(node.target_chapter, chapter_num):
                if node.status in ('埋伏', '待收'):
                    payoff_list.append(node)
        return {'setup': setup_list, 'payoff': payoff_list}

    def mark_setup(self, node_id: str, chapter_num: int):
        """标记伏笔已埋设"""
        if node_id in self.nodes:
            self.nodes[node_id].status = '待收'
            if not self.nodes[node_id].created_at:
                self.nodes[node_id].created_at = str(chapter_num)

    def mark_resolved(self, node_id: str, chapter_num: int):
        """标记伏笔已回收"""
        if node_id in self.nodes:
            self.nodes[node_id].status = '已收'
            self.nodes[node_id].payoff_chapter = str(chapter_num)

    def get_overdue_nodes(self, current_chapter: int, overdue_threshold: int = 10) -> List[ForeshadowingNode]:
        """查询逾期未回收的伏笔（超过 target_chapter 仍未回收）"""
        overdue = []
        for node in self.nodes.values():
            if node.status in ('已收', '废弃'):
                continue
            if not node.target_chapter:
                continue
            target = _parse_chapter_num(node.target_chapter)
            if target and current_chapter - target > 0:
                gap = current_chapter - target
                node_dict = node.to_dict()
                node_dict['overdue_gap'] = gap
                node_dict['severity'] = 'critical' if (node.promoted and gap > overdue_threshold) else 'warning'
                overdue.append(node)
        # 按逾期章数降序
        overdue.sort(key=lambda n: _parse_chapter_num(n.target_chapter) or 0)
        return overdue

    def to_dict(self) -> Dict:
        return {
            'nodes': {nid: n.to_dict() for nid, n in self.nodes.items()},
            'edges': [asdict(e) for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'ForeshadowingGraph':
        g = cls()
        for nid, ndata in (data.get('nodes') or {}).items():
            g.add_node(ForeshadowingNode(
                id=nid,
                content=ndata.get('content', ''),
                weight=ndata.get('weight', 5),
                layer=ndata.get('layer', '支线'),
                status=ndata.get('status', '埋伏'),
                created_at=ndata.get('created_at', ''),
                target_chapter=ndata.get('target_chapter', ''),
                depends_on=ndata.get('depends_on', []),
                core_hook=ndata.get('core_hook', False),
                promoted=ndata.get('promoted', False),
                tags=ndata.get('tags', []),
                payoff_chapter=ndata.get('payoff_chapter', ''),
            ))
        for edata in (data.get('edges') or []):
            g.add_edge(ForeshadowingEdge(
                from_id=edata.get('from_id', ''),
                to_id=edata.get('to_id', ''),
                edge_type=edata.get('edge_type', '依赖'),
            ))
        return g

    def to_text_view(self) -> str:
        """将 DAG 渲染回文本列表格式（兼容现有 foreshadowing 字段展示）"""
        lines = []
        sorted_nodes = sorted(self.nodes.values(), key=lambda n: n.id)
        for i, node in enumerate(sorted_nodes, 1):
            lines.append(f'## 伏笔{i}：{node.content[:50]}')
            lines.append(f'- 埋设内容：{node.content}')
            lines.append(f'- 埋设时机：{node.created_at or "未埋设"}')
            lines.append(f'- 预期回收：{node.target_chapter or "待定"}')
            lines.append(f'- 回收方式：{"已回收" if node.status == "已收" else "待回收"}')
            lines.append(f'- 状态：{node.status}（权重{node.weight}，{node.layer}）')
            if node.depends_on:
                lines.append(f'- 依赖伏笔：{", ".join(node.depends_on)}')
            lines.append('')
        return '\n'.join(lines)


# ===== 辅助函数 =====

def _parse_chapter_num(chapter_str: str) -> Optional[int]:
    """从字符串中解析章号，如 '第50章' → 50，'50' → 50"""
    if not chapter_str:
        return None
    m = re.search(r'(\d+)', str(chapter_str))
    return int(m.group(1)) if m else None


def _chapter_match(chapter_ref: str, chapter_num: int) -> bool:
    """判断章节引用是否匹配当前章号"""
    parsed = _parse_chapter_num(chapter_ref)
    return parsed == chapter_num if parsed else False


def parse_text_to_dag(text: str) -> ForeshadowingGraph:
    """从文本列表格式解析为 DAG。
    支持现有格式：## 伏笔N：标题\n- 埋设内容：xxx\n- 埋设时机：xxx\n- 预期回收：xxx
    自动推断 weight/layer/depends_on（启发式）"""
    g = ForeshadowingGraph()
    if not text or not text.strip():
        return g

    # 按 ## 标题分段
    blocks = re.split(r'\n(?=##\s*伏笔)', text.strip())
    idx = 0
    for block in blocks:
        block = block.strip()
        if not block.startswith('##'):
            continue
        idx += 1
        node_id = f'f{idx:03d}'

        # 提取标题
        title_match = re.match(r'##\s*伏笔\d+[：:]\s*(.+)', block)
        title = title_match.group(1).strip() if title_match else f'伏笔{idx}'

        # 提取各字段
        content = _extract_field(block, r'埋设内容[：:]\s*(.+)')
        if not content:
            content = title  # 无埋设内容时用标题
        setup_time = _extract_field(block, r'埋设时机[：:]\s*(.+)')
        target = _extract_field(block, r'预期回收[：:]\s*(.+)')

        # 启发式推断权重和层级
        weight = 5
        layer = '支线'
        core_hook = False
        keywords_main = ['主线', '核心', '关键', '终极', '真相', '身世', '来历']
        keywords_egg = ['彩蛋', '趣味', '致敬']
        content_lower = content.lower() + title.lower()
        if any(k in content_lower for k in keywords_main):
            weight = 9
            layer = '主线'
            core_hook = True
        elif any(k in content_lower for k in keywords_egg):
            weight = 3
            layer = '彩蛋'

        # 状态推断
        status = '埋伏'
        if setup_time and '未' in setup_time:
            status = '埋伏'
        elif target and ('已' in target or '回收' in target):
            status = '待收'

        node = ForeshadowingNode(
            id=node_id,
            content=content,
            weight=weight,
            layer=layer,
            status=status,
            created_at=setup_time or '',
            target_chapter=target or '',
            core_hook=core_hook,
        )
        g.add_node(node)

    return g


def _extract_field(text: str, pattern: str) -> str:
    """从文本中提取字段值"""
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ''


def get_hooks_for_chapter(graph: ForeshadowingGraph, chapter_num: int) -> Dict:
    """获取本章相关的伏笔信息（供章节 prompt 注入用）"""
    if not graph:
        return {'setup': [], 'payoff': []}
    result = graph.get_nodes_for_chapter(chapter_num)
    return {
        'setup': [{'id': n.id, 'content': n.content, 'weight': n.weight,
                   'target_chapter': n.target_chapter} for n in result['setup']],
        'payoff': [{'id': n.id, 'content': n.content, 'weight': n.weight,
                    'depends_on': n.depends_on} for n in result['payoff']],
    }


def build_hooks_prompt_section(graph: ForeshadowingGraph, chapter_num: int) -> str:
    """构建伏笔注入 prompt 片段"""
    if not graph or not graph.nodes:
        return ''
    hooks = get_hooks_for_chapter(graph, chapter_num)
    if not hooks['setup'] and not hooks['payoff']:
        # 无本章专属伏笔时，注入主线核心伏笔状态提醒
        pending = graph.get_pending_nodes(min_weight=7)
        if not pending:
            return ''
        lines = ['【伏笔状态提醒（主线核心伏笔，注意不要矛盾）】']
        for n in pending[:5]:
            lines.append(f'- {n.id} {n.content[:30]}（{n.status}，计划{n.target_chapter}回收）')
        return '\n'.join(lines)

    lines = ['【本章伏笔任务（必须执行）】']
    if hooks['setup']:
        lines.append('本章必须埋设：')
        for h in hooks['setup']:
            lines.append(f'  - {h["id"]} {h["content"]}（计划{h["target_chapter"]}回收，权重{h["weight"]}）')
    if hooks['payoff']:
        lines.append('本章应当回收：')
        for h in hooks['payoff']:
            dep = f'（依赖 {",".join(h["depends_on"])}，回收时需呼应）' if h['depends_on'] else ''
            lines.append(f'  - {h["id"]} {h["content"]}{dep}')
    return '\n'.join(lines)


def generate_status_report(graph: ForeshadowingGraph, current_chapter: int = 0) -> Dict:
    """生成伏笔状态报告（供防遗忘检查用，零 LLM 成本）"""
    if not graph or not graph.nodes:
        return {'total': 0, 'overdue': [], 'pending': [], 'resolved': []}

    nodes = list(graph.nodes.values())
    total = len(nodes)
    resolved = [n for n in nodes if n.status == '已收']
    pending = [n for n in nodes if n.status in ('埋伏', '待收')]
    overdue = graph.get_overdue_nodes(current_chapter) if current_chapter else []

    return {
        'total': total,
        'resolved_count': len(resolved),
        'pending_count': len(pending),
        'overdue_count': len(overdue),
        'overdue': [n.to_dict() if isinstance(n, ForeshadowingNode) else n for n in overdue],
        'pending': [n.to_dict() for n in pending],
        'resolved': [n.to_dict() for n in resolved],
    }
