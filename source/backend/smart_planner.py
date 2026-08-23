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
    op: str  # rename_entity / refresh_dim / compute_chapter_mission / declared_stage / ...
    target: str  # 作用对象（维度名/实体名/章节号）
    args: Dict = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    auto: bool = True  # True=安全可自动执行；False=需用户确认
    reason: str = ''
    status: str = 'pending'  # pending / running / done / failed / skipped / declared
    result: Dict = field(default_factory=dict)
    # 优化2：LLM 生成类阶段（正文/审校/校验）无法在 TaskRunner 内流式执行，
    # 由宿主端点（ai_continue*）实际执行后通过 mark_stage 回写状态——此类任务
    # 标记 kind='declared'：TaskGraph 负责"编排+观测+持久化"，不负责执行。
    kind: str = 'exec'  # exec=TaskRunner执行 / declared=宿主端点执行

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
            reason=f'将 {entity_type}“{old_name}”重命名为“{new_name}”'
        ))

        # 2. 依赖字段重渲染（自动）
        for idx, field in enumerate(impact_fields, start=2):
            if field in ('chapters',):
                continue
            g.add(Task(
                id=f't{idx}', op='refresh_dim', target=field,
                depends_on=['t1'], auto=True,
                reason=f'{field} 字段可能含“{old_name}”，替换后需要同步索引/结构'
            ))

        # 3. 标脏相关章节（自动：记录哪些章节需要重写）
        g.add(Task(
            id='t_dirty', op='mark_dirty_chapters', target='chapters',
            args={'keyword': old_name}, depends_on=['t1'], auto=True,
            reason=f'扫描所有章节正文，标记含“{old_name}”的章节为待重写'
        ))

        # 4. 章节正文重写（手动：需要用户确认）
        g.add(Task(
            id='t_rewrite', op='regenerate_text', target='chapters',
            args={'keyword': old_name, 'scope': 'light'}, depends_on=['t_dirty'], auto=False,
            reason=f'含“{old_name}”的章节需要批量替换为“{new_name}”；是否让 AI 轻量重写（只改名，不改剧情）？'
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
        # 【用户反馈的实体识别不到】不止 character_profiles：世界观/剧情/分卷/构思/宗门/物品
        # 等维度里也大量包含人物/势力/地点/物品/技能，统一在"会写入实体的维度"落地后同步。
        sync_reg_dims = {'character_profiles', 'worldbuilding', 'timeline', 'concept',
                         'plot_design', 'dynamic_volumes', 'locations', 'inventory',
                         'foreshadowing', 'key_rules'}
        if dim_key in sync_reg_dims:
            g.add(Task(
                id='t_sync_entity', op='sync_entity_registry', target='entity_registry',
                auto=True,
                reason=f'从「{_DIM_LABEL_CN.get(dim_key, dim_key)}」维度同步实体注册表（人物/势力/地点/物品/技能）'
            ))

        return g

    def _plan_adopt_card(self, card_type: str, dim_key: str) -> TaskGraph:
        """采纳卡片本质上就是 edit_dim（overwrite=false 时追加）"""
        return self._plan_edit_dim(dim_key, '', is_overwrite=False)

    def _plan_generate_chapter(self, chapter_num: int, **opts) -> TaskGraph:
        """【优化2】写作流水线任务图：把 ai_continue* 硬编码的 11 阶段流水线
        统一建模为 TaskGraph（编排统一 / 可观测 / 可持久化恢复）。

        - kind='exec'：安全读侧任务，TaskRunner 直接执行（如伏笔任务计算）
        - kind='declared'：LLM 生成/审校阶段，由宿主端点实际执行后回写状态
          （流式生成必须留在 SSE generator，无法搬进 TaskRunner）
        - opts 可选开关：enable_consistency_check（一致性）/ enable_structured_tags，
          与 ai_continue 入参一致，决定条件阶段的取舍。
          t5_deai（去AI味审校）默认启用（内置规则常驻；skill_pack_ids 里的审查类
          技能包作为增强叠加，不再作为启停开关）
        """
        g = TaskGraph()
        has_cchk = bool(opts.get('enable_consistency_check'))
        t = f'chapter_{chapter_num}'

        def _stage(sid: str, op: str, label: str, kind: str = 'declared', **kw):
            g.add(Task(id=sid, op=op, target=t, kind=kind, reason=label,
                       args={'chapter_num': chapter_num, **kw.get('args', {})}, **{
                           'auto': kw.get('auto', kind == 'exec'),
                           'depends_on': kw.get('depends_on', []),
                           'status': 'declared' if kind == 'declared' else 'pending'}))

        _stage('t1_mission', 'compute_chapter_mission',
               f'写前任务：计算第{chapter_num}章伏笔任务（应埋/应收/禁揭示）', kind='exec')
        _stage('t2_ctx', 'build_context', '构建分层上下文（bible注入+滚动记忆+语义召回）')
        _stage('t2_plan', 'chapter_plan', '章节计划前置（200字计划先于正文）',
               depends_on=['t1_mission', 't2_ctx'])
        _stage('t3_draft', 'generate_draft', '正文生成（动态temperature+智能instruction）',
               depends_on=['t2_plan'])
        _stage('t4_wc', 'ensure_word_count', '字数铁律：初稿字数校验+AI重写修正',
               depends_on=['t3_draft'])
        _stage('t5_deai', 'deai_polish', '去AI味审校Agent（仅文风，不改剧情）',
               depends_on=['t4_wc'])
        if has_cchk:
            _stage('t6_cchk', 'consistency_check', '一致性检查Agent（key_rules/人设比对）',
                   depends_on=['t5_deai'])
        _stage('t7_changes', 'apply_chapter_changes', 'CHANGES解析+delta回写（12类章级变更）',
               depends_on=['t5_deai'])
        _stage('t8_pval', 'post_validate', '确定性后写校验（死亡复活/境界回退/文风漂移，零LLM）',
               depends_on=['t7_changes'])
        _stage('t9_cycle', 'review_cycle', '审计-修订闭环（校验→修订→再校验，最多2轮）',
               depends_on=['t8_pval'])
        _stage('t10_gates', 'landing_gates', '落地门禁（3道，critical拦截落库）',
               depends_on=['t9_cycle'])
        _stage('t11_post', 'post_persist_sync', '落库后置同步（事件日志+伏笔反查+实体注册）')
        return g


# ---------- TaskRunner：执行任务 ----------

class TaskRunner:
    """执行 TaskGraph，返回执行摘要"""

    def __init__(self, book_id: str, db=None, app_context=None, preview_mode: bool = False):
        self.book_id = book_id
        self.db = db
        self.app_context = app_context
        self.preview_mode = preview_mode  # True = 预览模式，所有写入任务返回 mock，不真正改数据

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

    def run_all_auto(self, graph: TaskGraph) -> Dict:
        """兼容入口（chat_collab_bp.preview_impact 调用）：等价 run(graph, only_auto=True)。"""
        return self.run(graph, only_auto=True)

    # ---------- 优化2：写作流水线阶段推进（declared 阶段由宿主端点执行后回写） ----------

    def mark_stage(self, graph: TaskGraph, task_id: str, status: str, result: Optional[Dict] = None):
        """宿主端点（ai_continue*）完成某 LLM 阶段后回写状态，供观测/持久化。"""
        t = graph.tasks.get(task_id)
        if not t:
            return
        t.status = status
        if result is not None:
            t.result = result

    def persist_plan_log(self, graph: TaskGraph, action: str, extra: Optional[Dict] = None) -> bool:
        """任务图持久化到 book_bible.plan_log_json（最近 20 条），支持中断恢复与事后观测。"""
        try:
            from app import BookBible, db
            bb = BookBible.query.filter_by(book_id=self.book_id).first()
            if not bb:
                return False
            logs = []
            try:
                logs = json.loads(bb.plan_log_json or '[]')
                if not isinstance(logs, list):
                    logs = []
            except Exception:
                logs = []
            entry = {'ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%S'),
                     'action': action, **(extra or {}), 'graph': graph.to_dict()}
            logs.append(entry)
            bb.plan_log_json = json.dumps(logs[-20:], ensure_ascii=False)
            db.session.commit()
            return True
        except Exception:
            try:
                from app import db
                db.session.rollback()
            except Exception:
                pass
            return False

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
        if op == 'check_dim_consistency':
            return self._exec_check_dim_consistency(task)
        return {'noop': True}

    def _exec_rename_entity(self, task: Task) -> Dict:
        if self.preview_mode:
            return {'preview_mode': True, 'note': '预览模式下不执行实体替换；实际应用时将整词替换 old→new（含JSON字段递归）'}
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
        if self.preview_mode:
            field = task.target
            if field == 'foreshadowing_graph':
                return {'preview_mode': True, 'note': '预览模式：伏笔DAG重渲染被跳过，实际应用时将从 foreshadowing 文本重算并写入 bible'}
            if field == 'event_log':
                return {'preview_mode': True, 'note': 'event_log 由章节入库时增量维护，此处无操作'}
            if field == 'entity_registry':
                return {'preview_mode': True, 'note': '预览模式：实体注册表被跳过，实际应用时将从 Bible+Character 全量抽取实体'}
            return {'preview_mode': True}
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
        if self.preview_mode:
            return {'preview_mode': True, 'note': '预览模式：解析伏笔DAG被跳过；实际应用时会重算 foreshadowing_graph 并持久化'}
        from app import BookBible
        from foreshadowing_manager import parse_text_to_dag
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb or not bb.foreshadowing:
            return {'nodes': 0}
        graph = parse_text_to_dag(bb.foreshadowing)
        bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
        return {'nodes': len(graph.nodes)}

    def _exec_sync_entity_registry(self, task: Task) -> Dict:
        if self.preview_mode:
            return {'preview_mode': True, 'note': '预览模式：实体注册表同步被跳过；实际应用时同步 Character/Bible → entity_registry_json'}
        from app import BookBible, Character, Chapter, db
        from entity_registry import extract_and_save_registry, register_chapter_entities
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb:
            return {'error': 'bible not found'}
        # 把最近 30 章也作为扫描源（正文/标题里的实体自动入表）
        recent_chapters = []
        try:
            recent_chapters = (
                Chapter.query.filter_by(book_id=self.book_id, is_volume=False)
                .order_by(Chapter.order_index.desc())
                .limit(30)
                .all()
            ) or []
        except Exception:
            recent_chapters = []
        # 1) 全量扫描 Bible + 最近章节 → 合并写入 entity_registry_json
        entities = extract_and_save_registry(bb, chapters_query=recent_chapters)
        # 2) Character 表兜底：确保 Character 表中的人物至少出现在注册表
        known_actors = [c.name for c in Character.query.filter_by(book_id=self.book_id).all() if c.name]
        latest = recent_chapters[0] if recent_chapters else None
        ch_num = (latest.order_index if latest else 0)
        register_chapter_entities(bb, ch_num, '', known_actors=known_actors)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'entities': sum(len(v) for v in entities.values()),
                'characters': len(entities.get('characters', [])),
                'factions': len(entities.get('factions', [])),
                'locations': len(entities.get('locations', [])),
                'items': len(entities.get('items', [])),
                'skills': len(entities.get('skills', []))}

    def _exec_compute_chapter_mission(self, task: Task) -> Dict:
        # 纯计算任务（伏笔 DAG 读操作），preview_mode 正常运行，不做写入
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

    def _exec_check_dim_consistency(self, task: Task) -> Dict[str, Any]:
        """check_dim_consistency 执行器：
        - source_dim = 被用户修改的维度（任务 args['source_dim'] 或 args['dim_key']）
        - source_new_text = 用户新设定内容（args['new_text']，未提供时从 Bible 读当前字段）
        - target_dim = 要核查的下游维度（task.target）
        """
        from app import BookBible
        bb = BookBible.query.filter_by(book_id=self.book_id).first()
        if not bb:
            return {'error': 'bible not found'}
        args = task.args or {}
        source_dim = args.get('source_dim') or args.get('dim_key') or ''
        target_dim = task.target or args.get('target') or ''
        if not source_dim or not target_dim:
            return {'error': 'missing dim key', 'source_dim': source_dim, 'target_dim': target_dim}

        # source_text 优先级：参数 new_text > Bible 当前字段
        src_text = args.get('new_text') or ''
        if not src_text:
            src_text = (getattr(bb, source_dim, None) or '') if source_dim else ''
        tgt_text = (getattr(bb, target_dim, None) or '') if target_dim else ''

        # 若目标维度是列表型字段（如 chapters/characters），拼接成文本
        if not tgt_text and target_dim in ('chapters', 'character_profiles'):
            try:
                tgt_text = _dump_target_dim_list(bb, target_dim, self.book_id)
            except Exception:
                tgt_text = ''

        result = run_dim_consistency_check(source_dim, src_text or '', target_dim, tgt_text or '')
        # severity = critical 的冲突，在任务层面标记为 failed
        if result.get('status') == 'conflict':
            return {
                'status': 'conflict',
                'critical': result['critical'],
                'warning': result['warning'],
                'note': result.get('note', 0),
                'target_label': result.get('target_label'),
                'issues': result['issues'],
                '_severity': 'conflict',
            }
        return {
            'status': result['status'],
            'critical': result['critical'],
            'warning': result['warning'],
            'note': result.get('note', 0),
            'target_label': result.get('target_label'),
            'issues': result['issues'],
        }


# ============================================================================
# 维度一致性冲突检查器（check_dim_consistency 执行器核心逻辑）
#
# 设计原则：
#   · 轻量（不依赖 LLM，全本地 regex/启发式，毫秒级）
#   · 返回结构化 issues，前端直接渲染为"红/黄/蓝点 + 引用片段 + 建议"
#   · severity = critical（冲突明确，建议硬阻断） / warning（疑似矛盾，需作者确认）
#     / note（信息性提示，不产生冲突）
# ============================================================================

_DIM_LABEL_CN = {
    'concept': '核心构思', 'key_rules': '核心规则/设定', 'worldbuilding': '世界观',
    'character_profiles': '人物档案', 'plot_design': '大纲', 'timeline': '剧情时间线',
    'foreshadowing': '伏笔', 'locations': '地点/地图', 'style_guide': '文风',
    'relation_graph': '关系图', 'dynamic_volumes': '动态卷信息',
    'outline_hierarchy': '大纲层级', 'entity_registry': '实体注册表',
    'event_log': '事件日志', 'foreshadowing_graph': '伏笔结构图',
}

# 否定/限制词：规则中的"禁止/不能/仅限"等
_NEGATIVE_WORDS = ['禁止', '不能', '无法', '不可', '绝不', '永远不', '不允许',
                   '仅限', '只能', '只有…才', '需要…才', '代价是', '一旦…就会死亡']
# 能力/许可词：人物/世界观中的"能够/可以/拥有"等
_POSITIVE_WORDS = ['能够', '可以', '拥有', '具有', '会使用', '掌握', '精通',
                   '随意', '无代价', '瞬间', '不死', '无敌', '永生']
# 数值/量级词（检测强弱冲突）
_SCALE_WORDS_PATTERN = r'(第[一二三四五六七八九十\d]+阶|境界[一二三四五六七八九十\d]+|[一二三四五六七八九十\d]+级|Lv\.?\d+|lv\.?\d+|[一二三四五六七八九十\d]+层|[一二三四五六七八九十\d]+品|天阶|地阶|玄阶|黄阶|S级|A级|B级|C级|SSR|SR|R|N)'


def _find_contradiction_pairs(source_text: str, source_dim: str,
                              target_text: str, target_dim: str) -> List[Dict[str, Any]]:
    """在 source_text（新内容）与 target_text（旧已有内容）之间做启发式冲突扫描。"""
    issues: List[Dict[str, Any]] = []
    if not source_text or not target_text:
        return issues

    src = source_text
    tgt = target_text

    # --------------------------------------------------------------
    # 1. 规则（key_rules）vs 人物/世界观：否定词 ↔ 肯定词 交叉命中
    #    例：规则"主角不能飞" vs 人物"林墨能够御剑飞行"
    # --------------------------------------------------------------
    if source_dim == 'key_rules' and target_dim in ('character_profiles', 'worldbuilding', 'plot_design', 'timeline'):
        for neg in _NEGATIVE_WORDS:
            for line in src.splitlines():
                if neg not in line:
                    continue
                # 抽出 line 中的"被约束的主体名词"（前 20 字汉字/字名词组）
                subj_match = _extract_head_subject(line, neg)
                for pos in _POSITIVE_WORDS:
                    for tline in tgt.splitlines():
                        if pos not in tline:
                            continue
                        # 同时主体/关键词有交集：看 neg 前面的词是否也出现在 tline
                        if subj_match and not _has_overlap_word(subj_match, tline):
                            continue
                        # 同一句不能自己命中自己（全文复制粘贴场景）
                        if _line_equalish(line, tline):
                            continue
                        issues.append({
                            'severity': 'warning',
                            'rule': '规则 ↔ 人物/剧情 的能力许可冲突',
                            'source_quote': _clip(line, 60),
                            'target_quote': _clip(tline, 60),
                            'suggestion': '检查：新设定中是否已放宽此限制，或旧内容是否需要同步改写。',
                        })

    # --------------------------------------------------------------
    # 2. 世界观/设定 ↔ 大纲剧情时间线：时间/地点 互相矛盾
    #    例：世界观"大灾变发生在 500 年前" vs 剧情"距今 300 年前曾发生大灾变"
    # --------------------------------------------------------------
    if source_dim in ('worldbuilding', 'key_rules', 'concept') and target_dim in ('timeline', 'plot_design'):
        for num_pat_src in re.findall(r'(\d+)\s*(年前|年后|年后|年|章|卷|代|世纪)', src):
            n_src, unit = num_pat_src
            key_src = _surrounding_keyword(src, n_src + unit, 12)
            if not key_src:
                continue
            for num_pat_tgt in re.findall(r'(\d+)\s*(年前|年后|年后|年|章|卷|代|世纪)', tgt):
                n_tgt, unit2 = num_pat_tgt
                if unit != unit2:
                    continue
                key_tgt = _surrounding_keyword(tgt, n_tgt + unit2, 12)
                if not key_tgt or not _has_overlap_word(key_src, key_tgt):
                    continue
                if n_src == n_tgt:
                    continue
                try:
                    diff = abs(int(n_src) - int(n_tgt))
                except ValueError:
                    continue
                sev = 'critical' if diff >= 200 and unit in ('年', '年前', '年后', '代', '世纪') else 'warning'
                issues.append({
                    'severity': sev,
                    'rule': f'时间数值不一致（{unit}）',
                    'source_quote': f'「{key_src}」{n_src}{unit}',
                    'target_quote': f'「{key_tgt}」{n_tgt}{unit}',
                    'suggestion': f'二者相差 {diff}{unit}，请核实哪个数字正确后统一。',
                })

    # --------------------------------------------------------------
    # 3. 人物档案 ↔ 剧情时间线：同一人名 身份/生死 冲突
    #    例：人物"林墨，战死第20章" vs 剧情"第50章林墨率军出征"
    # --------------------------------------------------------------
    if source_dim == 'character_profiles' and target_dim in ('timeline', 'foreshadowing', 'plot_design'):
        from entity_registry import _chinese_name_extractor  # 复用已有抽取逻辑（若有）
        names_src = _extract_person_names_simple(src)
        for name in names_src:
            # 在人物卡中查找 死亡/战死/失踪/离开/闭关 等状态词
            status_src = _find_character_status(src, name)
            if not status_src:
                continue
            # 在剧情中查找该人名 + 后续动作（出征/现身/说话/指挥）
            status_tgt = _find_character_alive_actions(tgt, name)
            if status_src in ('死', '战死', '失踪', '入灭', '魂飞魄散') and status_tgt:
                issues.append({
                    'severity': 'critical',
                    'rule': f'人物「{name}」 生死/状态矛盾',
                    'source_quote': f'人物档案：{status_src}',
                    'target_quote': f'剧情/伏笔中仍有行为：{status_tgt}',
                    'suggestion': '请确认人物是否真的死亡（或假死），若已死亡需改写剧情中的出现。',
                })

    # --------------------------------------------------------------
    # 4. 文风 ↔ 大纲/人物：硬约束 vs 文风声明
    #    例：文风"严禁使用比喻/古风措辞" vs 人物描写"翩若惊鸿婉若游龙"（非精确匹配，降为 info）
    # --------------------------------------------------------------
    if source_dim == 'style_guide' and target_dim in ('character_profiles', 'worldbuilding', 'plot_design'):
        # 仅做 info 级：文风与正文语言不一致（正文通常还不存在，此处仅提示）
        strict_words = ['禁用比喻', '禁用修辞', '严禁修辞', '无修辞', '无比喻', '白描', '直给', '口语化']
        for w in strict_words:
            if w in src:
                issues.append({
                    'severity': 'note',
                    'rule': '文风风格约束提示',
                    'source_quote': _clip(f'你在文风中设置了"{w}"', 40),
                    'target_quote': '已有维度内容若有文学修辞，需同步调整以符合文风。',
                    'suggestion': '生成正文时此约束会注入 prompt 生效；此处仅作预览。',
                })
                break

    # --------------------------------------------------------------
    # 5. 通用：等级/境界冲突 （key_rules / worldbuilding / character_profiles 内部互相打）
    # --------------------------------------------------------------
    if source_dim in ('key_rules', 'worldbuilding', 'character_profiles') and target_dim in source_dim:
        for scale_src in re.findall(_SCALE_WORDS_PATTERN, src):
            key_src = _surrounding_keyword(src, scale_src, 10)
            for scale_tgt in re.findall(_SCALE_WORDS_PATTERN, tgt):
                key_tgt = _surrounding_keyword(tgt, scale_tgt, 10)
                if not key_src or not key_tgt:
                    continue
                same_subj = _has_overlap_word(key_src, key_tgt) and scale_src != scale_tgt
                if not same_subj:
                    continue
                issues.append({
                    'severity': 'warning',
                    'rule': '等级/境界数值不一致',
                    'source_quote': f'「{key_src}」→ {scale_src}',
                    'target_quote': f'「{key_tgt}」→ {scale_tgt}',
                    'suggestion': '确认哪个等级正确后统一。',
                })

    # 去重
    seen = set()
    uniq = []
    for it in issues:
        k = (it['severity'], it['rule'], it.get('source_quote', ''), it.get('target_quote', ''))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


# ---------- 冲突扫描辅助函数 ----------

import re  # noqa: E402 （文件顶部已 import，此处仅作防御性二次 import）


def _extract_head_subject(line: str, anchor: str) -> str:
    idx = line.find(anchor)
    if idx <= 0:
        return ''
    head = line[:idx][-20:]
    # 保留最后一个中文/英文名词词组（含空格过滤）
    m = re.findall(r'[\u4e00-\u9fffA-Za-z·]+', head)
    return m[-1] if m else ''


def _has_overlap_word(a: str, b: str, min_len: int = 2) -> bool:
    tokens_a = set(re.findall(r'[\u4e00-\u9fffA-Za-z]{2,}', a))
    tokens_b = set(re.findall(r'[\u4e00-\u9fffA-Za-z]{2,}', b))
    return bool(tokens_a & tokens_b)


def _line_equalish(a: str, b: str) -> bool:
    def norm(s): return re.sub(r'\s+', '', s)
    na, nb = norm(a), norm(b)
    return bool(na) and (na in nb or nb in na)


def _clip(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n] + '…'


def _surrounding_keyword(text: str, anchor: str, window: int) -> str:
    idx = text.find(anchor)
    if idx < 0:
        return ''
    s = max(0, idx - window)
    e = min(len(text), idx + len(anchor) + window)
    return re.sub(r'\s+', '', text[s:e])


def _extract_person_names_simple(text: str) -> List[str]:
    """简化人名抽取：2-4 个汉字，且首字是中文姓氏常见字。兜底用 entity_registry 模块若存在。"""
    try:
        from entity_registry import _chinese_surname_set  # type: ignore
    except Exception:
        _chinese_surname_set = set('赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东殴殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公万俟司马上官欧阳夏侯诸葛闻人东方赫连皇甫尉迟公羊澹台公冶宗政濮阳淳于单于太叔申屠公孙仲孙轩辕令狐钟离宇文长孙慕容鲜于闾丘司徒司空丌官司寇仉督子车颛孙端木巫马公西漆雕乐正壤驷公良拓跋夹谷宰父谷梁晋楚闫法汝鄢涂钦段干百里东郭南门呼延归海羊舌微生岳帅缑亢况后有琴梁丘左丘东门西门商牟佘佴伯赏南宫墨哈谯笪年爱阳佟第五言福百家姓续')
    names = []
    for m in re.finditer(r'([\u4e00-\u9fff]{2,4})', text):
        s = m.group(1)
        if len(s) < 2 or len(s) > 4:
            continue
        if s[0] in _chinese_surname_set:
            names.append(s)
    return list(dict.fromkeys(names))  # 有序去重


_CHAR_STATUS_DEATH = {'战死', '牺牲', '去世', '死亡', '身陨', '陨落', '魂飞魄散', '入灭', '圆寂', '失踪', '下落不明', '杳无音信'}
_CHAR_ALIVE_ACTION = {'率军', '出征', '现身', '出现', '说道', '说道：', '开口', '大笑', '冷笑', '点头', '摇头',
                      '指挥', '下令', '提笔', '挥剑', '拔刀', '催动', '祭出', '飞上', '踏入', '亲自', '抵达'}


def _find_character_status(text: str, name: str) -> Optional[str]:
    i = text.find(name)
    if i < 0:
        return None
    window = text[i: i + 40]
    for w in _CHAR_STATUS_DEATH:
        if w in window:
            return w
    return None


def _find_character_alive_actions(text: str, name: str) -> Optional[str]:
    actions_found = []
    i = 0
    while True:
        j = text.find(name, i)
        if j < 0:
            break
        window = text[j: j + 50]
        for w in _CHAR_ALIVE_ACTION:
            if w in window and w not in actions_found:
                actions_found.append(w)
                if len(actions_found) >= 3:
                    break
        if len(actions_found) >= 3:
            break
        i = j + 1
    return '、'.join(actions_found[:3]) if actions_found else None


def run_dim_consistency_check(source_dim: str, source_new_text: str,
                               target_dim: str, target_text: str) -> Dict[str, Any]:
    """对外统一入口：返回 {status: 'ok'|'warn'|'conflict', issues: [...]}。"""
    issues = _find_contradiction_pairs(source_new_text, source_dim, target_text, target_dim)
    critical = sum(1 for x in issues if x['severity'] == 'critical')
    warning = sum(1 for x in issues if x['severity'] == 'warning')
    if critical > 0:
        status = 'conflict'
    elif warning > 0:
        status = 'warn'
    else:
        status = 'ok'
    return {
        'status': status,
        'critical': critical,
        'warning': warning,
        'note': sum(1 for x in issues if x['severity'] == 'note'),
        'source_dim': source_dim,
        'target_dim': target_dim,
        'target_label': _DIM_LABEL_CN.get(target_dim, target_dim),
        'issues': issues,
    }


def _dump_target_dim_list(bb, target_dim: str, book_id: str) -> str:
    """把 list 型维度（chapters 等）拼接成纯文本供一致性扫描。"""
    try:
        if target_dim == 'chapters':
            from app import Chapter
            chunks = []
            for ch in Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all():
                title = ch.title or ''
                summary = (ch.summary or ch.content or '')[:800]
                chunks.append(f'【第{ch.order_index}章】{title}\n{summary}')
            return '\n\n'.join(chunks)
        if target_dim == 'character_profiles':
            from app import Character
            chunks = []
            for c in Character.query.filter_by(book_id=book_id).all():
                chunks.append(f'{c.name or "未知"}|{c.role or ""}|{c.personality or ""}|{(c.background or "")[:600]}')
            return '\n'.join(chunks)
    except Exception:
        return ''
    return ''


# ============================================================================
# 【优化2】写作流水线统一编排入口（供 app.py 的 ai_continue* 调用）
#
# 把"写一章"从端点内硬编码流水线升级为 TaskGraph 统一编排：
#   graph, runner = build_writing_pipeline(book_id, bb, chapter_num, opts)
#   → runner 已执行写前安全任务（t1_mission 伏笔任务计算）
#   → 宿主端点按原有逻辑执行各 LLM 阶段，每阶段完成后 runner.mark_stage(...)
#   → 结束时 runner.persist_plan_log(graph, 'generate_chapter') 持久化轨迹
# 任何一步抛错都不阻断写作（降级为无任务图模式），保持既有行为兼容。
# ============================================================================

def build_writing_pipeline(book_id: str, bb, chapter_num: int, **opts):
    """构建写作任务图并执行写前安全任务。

    返回 (graph, runner, mission)：
      - graph: TaskGraph（11 阶段，declared 阶段待宿主推进）
      - runner: TaskRunner（含 mark_stage / persist_plan_log）
      - mission: t1_mission 执行结果 {setup, payoff, forbidden}（失败为 None）
    任何异常返回 (None, None, None)，宿主端点降级为旧直连模式。
    """
    try:
        planner = SmartPlanner(book_id, bb)
        graph = planner.build_plan('generate_chapter', chapter_num=chapter_num, **opts)
        runner = TaskRunner(book_id)
        mission = None
        t1 = graph.tasks.get('t1_mission')
        if t1 and t1.status == 'pending':
            summary = runner.run(graph, only_auto=True)
            for item in summary.get('executed', []):
                if item.get('id') == 't1_mission':
                    mission = item.get('result')
                    break
        return graph, runner, mission
    except Exception:
        return None, None, None
