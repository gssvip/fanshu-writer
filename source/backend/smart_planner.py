# -*- coding: utf-8 -*-
"""智驾·中央调度层（M2）

核心能力：
  1. ImpactGraph：维度/实体/伏笔/事件之间的影响关系图
  2. SmartPlanner：输入动作（rename_entity / edit_dim / generate_chapter / adopt_card），
     输出可执行任务图 TaskGraph
  3. TaskRunner：按拓扑顺序执行，自动处理级联修改

设计原则：
  - 先给用户"影响预览"（preview），用户确认后再执行
  - 安全任务自动执行（DAG 重渲染、实体注册表同步、EventLog 索引更新）
  - 高风险任务提示用户（章节正文批量重写、删除伏笔）
  - 所有任务持久化到 book_bible.plan_log_json，支持中断恢复
"""
import json
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Callable
from collections import defaultdict


# ---------- ImpactGraph：谁改了会影响谁 ----------

DIMENSION_IMPACT = {
    # 改 concept → 下游全部要重看
    'concept': ['key_rules', 'worldbuilding', 'plot_design', 'character_profiles', 'timeline', 'foreshadowing'],
    # 改大纲 → 剧情/伏笔/人物可能需要联调
    'plot_design': ['timeline', 'foreshadowing', 'character_profiles'],
    # 改人物 → 剧情/关系/伏笔/正文可能要改
    'character_profiles': ['timeline', 'foreshadowing', 'relation_graph'],
    # 改剧情 → 伏笔/正文/动态文件要更新
    'timeline': ['foreshadowing', 'dynamic_volumes'],
    # 改世界 → 大纲/剧情/规则可能要补
    'worldbuilding': ['plot_design', 'timeline', 'key_rules'],
    # 改规则 → 所有生成都受影响，但不直接改数据
    'key_rules': [],
    # 改伏笔 → 主要影响正文写作时的任务清单
    'foreshadowing': [],
}

ENTITY_IMPACT = {
    'rename_character': ['character_profiles', 'timeline', 'foreshadowing', 'foreshadowing_graph', 'event_log', 'entity_registry', 'chapters'],
    'merge_character': ['character_profiles', 'timeline', 'foreshadowing', 'foreshadowing_graph', 'event_log', 'entity_registry', 'chapters'],
    'rename_location': ['worldbuilding', 'locations', 'timeline', 'chapters'],
    'rename_faction': ['worldbuilding', 'dynamic_volumes', 'timeline', 'chapters'],
}


def get_dim_impact(dim_key: str) -> List[str]:
    return DIMENSION_IMPACT.get(dim_key, [])


def get_entity_impact(action: str) -> List[str]:
    return ENTITY_IMPACT.get(action, [])


# ---------- 任务图 ----------

@dataclass
class Task:
    id: str
    op: str  # rename_entity / refresh_dim / reindex_events / reindex_hooks / mark_dirty_chapters / regenerate_text / sync_registry
    target: str  # 作用对象（维度名/实体名/章节号）
    args: Dict = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    auto: bool = True  # True=安全可自动执行；False=需用户确认
    reason: str = ''
    status: str = 'pending'  # pending / running / done / failed
    result: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class TaskGraph:
    def __init__(self, tasks: List[Task] = None):
        self.tasks: Dict[str, Task] = {t.id: t for t in (tasks or [])}

    def add(self, task: Task):
        self.tasks[task.id] = task

    def topo_order(self) -> List[Task]:
        """拓扑排序，未声明依赖的任务放前面"""
        ids = list(self.tasks.keys())
        order = []
        done = set()
        # 简单迭代：每次找 depends_on 已全部在 done 中的 task
        while len(order) < len(ids):
            progressed = False
            for tid in ids:
                if tid in done:
                    continue
                t = self.tasks[tid]
                if all(d in done or d not in self.tasks for d in t.depends_on):
                    order.append(t)
                    done.add(tid)
                    progressed = True
            if not progressed:
                # 有环，按原顺序追加剩余
                for tid in ids:
                    if tid not in done:
                        order.append(self.tasks[tid])
                break
        return order

    def auto_tasks(self) -> List[Task]:
        return [t for t in self.topo_order() if t.auto]

    def manual_tasks(self) -> List[Task]:
        return [t for t in self.topo_order() if not t.auto]

    def to_dict(self) -> Dict:
        return {'tasks': [t.to_dict() for t in self.topo_order()]}


# ---------- SmartPlanner ----------

class SmartPlanner:
    """根据用户动作生成 TaskGraph"""

    def __init__(self, book_id: str, bb=None):
        self.book_id = book_id
        self.bb = bb

    def build_plan(self, action: str, **kwargs) -> TaskGraph:
        """统一入口"""
        if action == 'rename_entity':
            return self._plan_rename_entity(**kwargs)
        if action == 'edit_dim':
            return self._plan_edit_dim(**kwargs)
        if action == 'adopt_card':
            return self._plan_adopt_card(**kwargs)
        if action == 'generate_chapter':
            return self._plan_generate_chapter(**kwargs)
        return TaskGraph()

    def _plan_rename_entity(self, old_name: str, new_name: str, entity_type: str = 'character') -> TaskGraph:
        g = TaskGraph()
        if not old_name or not new_name or old_name == new_name:
            return g

        impact_fields = get_entity_impact(f'rename_{entity_type}')
        # 1. 文本替换任务（自动）
        g.add(Task(
            id='t1', op='rename_entity', target=old_name,
            args={'old_name': old_name, 'new_name': new_name, 'entity_type': entity_type},
            auto=True,
            reason=f'将 {entity_type}「{old_name}」重命名为「{new_name}」'
        ))

        # 2. 依赖字段重渲染（自动）
        for idx, field in enumerate(impact_fields, start=2):
            if field in ('chapters',):
                continue
            g.add(Task(
                id=f't{idx}', op='refresh_dim', target=field,
                depends_on=['t1'], auto=True,
                reason=f'{field} 字段可能含「{old_name}」，替换后需要同步索引/结构'
            ))

        # 3. 标脏相关章节（自动：记录哪些章节需要重写）
        g.add(Task(
            id='t_dirty', op='mark_dirty_chapters', target='chapters',
            args={'keyword': old_name}, depends_on=['t1'], auto=True,
            reason=f'扫描所有章节正文，标记含「{old_name}」的章节为待重写'
        ))

        # 4. 章节正文重写（手动：需要用户确认）
        g.add(Task(
            id='t_rewrite', op='regenerate_text', target='chapters',
            args={'keyword': old_name, 'scope': 'light'}, depends_on=['t_dirty'], auto=False,
            reason=f'含「{old_name}」的章节需要批量替换为「{new_name}」；是否让 AI 轻量重写（只改名，不改剧情）？'
        ))

        return g

    def _plan_edit_dim(self, dim_key: str, content: str, is_overwrite: bool = False) -> TaskGraph:
        g = TaskGraph()
        # 维度修改后，下游维度可能需要联调
        impacted = get_dim_impact(dim_key)
        for idx, target_dim in enumerate(impacted, start=1):
            g.add(Task(
                id=f't{idx}', op='check_dim_consistency', target=target_dim,
                args={'source_dim': dim_key}, auto=True,
                reason=f'{dim_key} 已变更，检查 {target_dim} 是否与新内容冲突'
            ))

        # 伏笔维度落地后，自动解析为 DAG
        if dim_key == 'foreshadowing':
            g.add(Task(
                id='t_parse_fs', op='parse_foreshadowing_dag', target='foreshadowing_graph',
                depends_on=[f't{idx}' for idx, _ in enumerate(impacted, start=1)] if impacted else [],
                auto=True,
                reason='将 foreshadowing 文本解析为 DAG，供后续章节任务清单使用'
            ))

        # 人物维度落地后，同步实体注册表
        if dim_key == 'character_profiles':
            g.add(Task(
                id='t_sync_entity', op='sync_entity_registry', target='entity_registry',
                auto=True,
                reason='从人物档案同步实体注册表'
            ))

        return g

    def _plan_adopt_card(self, card_type: str, dim_key: str) -> TaskGraph:
        """采纳卡片本质上就是 edit_dim（overwrite=false 时追加）"""
        return self._plan_edit_dim(dim_key, '', is_overwrite=False)

    def _plan_generate_chapter(self, chapter_num: int) -> TaskGraph:
        """写章节前：算任务清单"""
        g = TaskGraph()
        g.add(Task(
            id='t_hooks', op='compute_chapter_mission', target=f'chapter_{chapter_num}',
            args={'chapter_num': chapter_num}, auto=True,
            reason=f'计算第{chapter_num}章的伏笔任务（应埋/应收/禁揭示）'
        ))
        return g


# ---------- TaskRunner：执行任务 ----------

class TaskRunner:
    """执行 TaskGraph，返回执行摘要"""

    def __init__(self, book_id: str, db=None, app_context=None):
        self.book_id = book_id
        self.db = db
        self.app_context = app_context

    def run(self, graph: TaskGraph, only_auto: bool = True) -> Dict:
        summary = {'executed': [], 'failed': [], 'skipped_manual': 0}
        tasks = graph.auto_tasks() if only_auto else graph.topo_order()
        for t in tasks:
            if t.status == 'done':
                continue
            t.status = 'running'
            try:
                result = self._execute(t)
                t.status = 'done'
                t.result = result or {}
                summary['executed'].append({'id': t.id, 'op': t.op, 'target': t.target, 'result': t.result})
            except Exception as e:
                t.status = 'failed'
                t.result = {'error': str(e)}
                summary['failed'].append({'id': t.id, 'op': t.op, 'error': str(e)})
        if only_auto:
            summary['skipped_manual'] = len(graph.manual_tasks())
        return summary

    def _execute(self, task: Task) -> Dict:
        op = task.op
        if op == 'rename_entity':
            return self._exec_rename_entity(task)
        if op == 'refresh_dim':
            return self._exec_refresh_dim(task)
        if op == 'mark_dirty_chapters':
            return self._exec_mark_dirty_chapters(task)
        if op == 'parse_foreshadowing_dag':
            return self._exec_parse_foreshadowing_dag(task)
        if op == 'sync_entity_registry':
            return self._exec_sync_entity_registry(task)
        if op == 'compute_chapter_mission':
            return self._exec_compute_chapter_mission(task)
        return {'noop': True}

    def _exec_rename_entity(self, task: Task) -> Dict:
        from app import BookBible, Chapter
        from entity_registry import rename_entity
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb:
            return {'error': 'bible not found'}
        chapters = Chapter.query.filter_by(book_id=self.book_id, is_volume=False).all()
        return rename_entity(
            bb, chapters,
            task.args.get('old_name'), task.args.get('new_name'),
            task.args.get('entity_type', 'character')
        )

    def _exec_refresh_dim(self, task: Task) -> Dict:
        # 目前阶段：只做 DAG/结构重渲染，不调用 LLM 重写
        from app import BookBible
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb:
            return {'error': 'bible not found'}
        field = task.target
        if field == 'foreshadowing_graph' and bb.foreshadowing:
            from foreshadowing_manager import parse_text_to_dag
            try:
                graph = parse_text_to_dag(bb.foreshadowing)
                bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
                return {'nodes': len(graph.nodes)}
            except Exception as e:
                return {'error': str(e)}
        if field == 'event_log':
            # EventLog 在章节入库时自动维护，这里不做额外操作
            return {'note': 'event_log maintained incrementally'}
        if field == 'entity_registry':
            from entity_registry import extract_entities
            entities = extract_entities(bb)
            return {'entities': sum(len(v) for v in entities.values())}
        return {'note': f'no auto-refresh for {field}'}

    def _exec_mark_dirty_chapters(self, task: Task) -> Dict:
        from app import BookBible, Chapter
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        keyword = task.args.get('keyword', '')
        if not bb or not keyword:
            return {'marked': 0}
        dirty = []
        for ch in Chapter.query.filter_by(book_id=self.book_id, is_volume=False).all():
            if ch.content and keyword in ch.content:
                dirty.append({'id': ch.id, 'title': ch.title, 'order_index': ch.order_index})
        # 写入 bible 的 plan_log（临时字段，也可扩展为 dirty_chapters_json）
        return {'marked': len(dirty), 'chapters': dirty[:20]}

    def _exec_parse_foreshadowing_dag(self, task: Task) -> Dict:
        from app import BookBible
        from foreshadowing_manager import parse_text_to_dag
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb or not bb.foreshadowing:
            return {'nodes': 0}
        graph = parse_text_to_dag(bb.foreshadowing)
        bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
        return {'nodes': len(graph.nodes)}

    def _exec_sync_entity_registry(self, task: Task) -> Dict:
        from app import BookBible, Character
        from entity_registry import extract_entities, register_chapter_entities
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb:
            return {'error': 'bible not found'}
        entities = extract_entities(bb)
        # 同步 Character 表到注册表
        known_actors = [c.name for c in Character.query.filter_by(book_id=self.book_id).all() if c.name]
        # 找一个最新章节号兜底
        from app import Chapter
        latest = Chapter.query.filter_by(book_id=self.book_id, is_volume=False).order_by(
            Chapter.order_index.desc()).first()
        ch_num = latest.order_index if latest else 0
        register_chapter_entities(bb, ch_num, '', known_actors=known_actors)
        return {'entities': sum(len(v) for v in entities.values())}

    def _exec_compute_chapter_mission(self, task: Task) -> Dict:
        from app import BookBible
        from foreshadowing_manager import ForeshadowingGraph, build_hooks_prompt_section
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        chapter_num = task.args.get('chapter_num')
        if not bb or not chapter_num:
            return {'error': 'missing params'}
        if not bb.foreshadowing_graph:
            return {'setup': [], 'payoff': [], 'forbidden': []}
        graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
        hooks = graph.get_nodes_for_chapter(chapter_num)
        setup = [{'id': n.id, 'content': n.content, 'weight': n.weight} for n in hooks.get('setup', [])]
        payoff = [{'id': n.id, 'content': n.content, 'weight': n.weight} for n in hooks.get('payoff', [])]
        # 禁揭示：核心伏笔且未收、且不是本章应收的
        payoff_ids = {n.id for n in hooks.get('payoff', [])}
        forbidden = []
        for n in graph.get_pending_nodes(min_weight=7):
            if n.id not in payoff_ids:
                forbidden.append({'id': n.id, 'content': n.content[:60]})
        return {'setup': setup, 'payoff': payoff, 'forbidden': forbidden}
