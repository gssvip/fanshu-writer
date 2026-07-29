"""
四级大纲层级构建器（P1-4）
将 timeline（分卷 JSON）+ plot_design（五幕式总纲）升级为四级层级：
  master（总纲）→ arc（篇/卷）→ section（节）→ chapter（章）

参考：Openwrite outline_parser + OutlineHierarchy
设计原则：
  - 由 timeline 自动派生，不要求作者手动维护
  - chapter 带 dramatic_position（起/承/转/合/过渡），供节拍模板选择用
  - 老书无此字段时降级，不影响现有流程
"""
import json
import re
from typing import Dict, List, Optional, Any


# 戏剧位置枚举
DRAMATIC_POSITIONS = ['起', '承', '转', '合', '过渡']


def build_outline_hierarchy(timeline_json: str, plot_design: str = '', total_chapters: int = 0) -> Dict:
    """从 timeline JSON 构建四级大纲层级。
    timeline 格式：[{volume_index, volume, main_plot, core_conflict, ending_hook, nodes:[{title, chapters, type, summary, cool_type}]}]
    返回：{master, arcs, sections, chapters}
    """
    try:
        timeline = json.loads(timeline_json) if timeline_json else []
    except Exception:
        timeline = []

    if not timeline:
        return _empty_hierarchy()

    # 1. master 总纲（从 plot_design 提取关键信息）
    master = {
        'id': 'master',
        'node_type': 'MASTER',
        'core_theme': _extract_theme(plot_design) or '（待补充）',
        'ending_direction': _extract_ending(plot_design),
        'key_turns': _extract_key_turns(plot_design),
        'word_count_target': total_chapters * 2500 if total_chapters else 0,
    }

    # 2. arcs 篇/卷级（每卷一个 arc）
    arcs = []
    chapters = []
    for vol in timeline:
        vol_index = vol.get('volume_index', 1)
        vol_name = vol.get('volume', f'第{vol_index}卷')
        nodes = vol.get('nodes', [])
        # 卷覆盖的章节范围（从 nodes 的 chapters 字段推断）
        ch_range = _infer_chapter_range(nodes)
        arc = {
            'id': f'arc_{vol_index:02d}',
            'node_type': 'ARC',
            'parent_id': 'master',
            'arc_name': vol_name,
            'arc_theme': vol.get('main_plot', '')[:100],
            'arc_structure': _infer_arc_structure(nodes),
            'arc_emotional_arc': vol.get('core_conflict', '')[:100],
            'chapter_range': ch_range,
            'children_sections': [],
        }
        arcs.append(arc)

        # 3. sections 节级（每个 node 是一个 section）+ chapters 章级
        for i, node in enumerate(nodes):
            sec_id = f'sec_{vol_index:02d}_{i+1:02d}'
            node_chapters = _parse_node_chapters(node.get('chapters', ''))
            sec = {
                'id': sec_id,
                'node_type': 'SECTION',
                'parent_id': arc['id'],
                'purpose': node.get('title', ''),
                'section_structure': _infer_section_structure(node, i, len(nodes)),
                'section_emotional_arc': node.get('summary', '')[:100],
                'section_tension': _infer_tension(node, i, len(nodes)),
                'chapter_range': node_chapters,
                'children_chapters': [],
            }
            arc['children_sections'].append(sec_id)

            # 4. chapters 章级（node_chapters 展开为单章）
            for ch_num in node_chapters:
                position = _compute_dramatic_position(ch_num, node_chapters)
                chapter = {
                    'id': f'ch_{ch_num:03d}',
                    'node_type': 'CHAPTER',
                    'parent_id': sec_id,
                    'chapter_num': ch_num,
                    'dramatic_position': position,
                    'content_focus': node.get('title', ''),
                    'goals': [],
                    'beats': [],  # P1-5 节拍模板填充
                    'hooks': [],
                    'emotional_arc': '',
                    'estimated_words': 2500,
                }
                sec['children_chapters'].append(chapter['id'])
                chapters.append(chapter)

    return {
        'master': master,
        'arcs': arcs,
        'sections': [s for arc in arcs for s in _get_sections_from_arc(arc, chapters)],
        'chapters': chapters,
    }


def _empty_hierarchy() -> Dict:
    return {'master': None, 'arcs': [], 'sections': [], 'chapters': []}


def _extract_theme(plot_design: str) -> str:
    """从五幕式总纲提取核心主题"""
    if not plot_design:
        return ''
    # 取第一幕的核心目标作为主题近似
    m = re.search(r'第[一1]幕[：:](.+?)(?=第[二2]幕|$)', plot_design, re.S)
    return m.group(1).strip()[:100] if m else plot_design[:100]


def _extract_ending(plot_design: str) -> str:
    """提取结局走向"""
    if not plot_design:
        return ''
    m = re.search(r'第[五5]幕[：:](.+)', plot_design, re.S)
    return m.group(1).strip()[:100] if m else ''


def _extract_key_turns(plot_design: str) -> List[str]:
    """提取关键转折点"""
    if not plot_design:
        return []
    turns = []
    for m in re.finditer(r'第([一二三四五1-5])幕[：:]\s*(.+?)(?=第[一二三四五1-5]幕|$)', plot_design, re.S):
        turns.append(m.group(2).strip()[:50])
    return turns[:5]


def _infer_chapter_range(nodes: List) -> str:
    """从 nodes 推断卷覆盖的章节范围"""
    all_chs = []
    for node in nodes:
        all_chs.extend(_parse_node_chapters(node.get('chapters', '')))
    if not all_chs:
        return ''
    return f'{min(all_chs)}-{max(all_chs)}'


def _parse_node_chapters(chapters_str: str) -> List[int]:
    """解析 node 的 chapters 字段为章号列表。
    格式如 '10-12' → [10,11,12]，'10' → [10]"""
    if not chapters_str:
        return []
    chapters_str = str(chapters_str).strip()
    m = re.match(r'(\d+)\s*-\s*(\d+)', chapters_str)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return list(range(start, end + 1))
    m = re.match(r'(\d+)', chapters_str)
    return [int(m.group(1))] if m else []


def _infer_arc_structure(nodes: List) -> str:
    """推断卷的起承转合结构"""
    if not nodes:
        return ''
    n = len(nodes)
    parts = []
    for i, node in enumerate(nodes):
        pos = _position_by_index(i, n)
        title = node.get('title', f'节点{i+1}')[:10]
        parts.append(f'{pos}({title})')
    return '→'.join(parts)


def _infer_section_structure(node: Dict, idx: int, total: int) -> str:
    """推断节的戏剧结构"""
    pos = _position_by_index(idx, total)
    title = node.get('title', '')[:15]
    return f'{pos}({title})'


def _infer_tension(node: Dict, idx: int, total: int) -> str:
    """推断张力走向"""
    if total <= 1:
        return 'low'
    if idx == 0:
        return 'low→rising'
    if idx < total - 1:
        return 'rising→peak'
    return 'peak→falling'


def _position_by_index(idx: int, total: int) -> str:
    """按索引位置返回戏剧位置"""
    if total <= 0:
        return '过渡'
    if total == 1:
        return '合'
    ratio = idx / total
    if ratio < 0.25:
        return '起'
    elif ratio < 0.5:
        return '承'
    elif ratio < 0.75:
        return '转'
    else:
        return '合'


def _compute_dramatic_position(ch_num: int, chapter_list: List[int]) -> str:
    """计算单章的戏剧位置"""
    if not chapter_list:
        return '过渡'
    try:
        idx = chapter_list.index(ch_num)
    except ValueError:
        return '过渡'
    return _position_by_index(idx, len(chapter_list))


def _get_sections_from_arc(arc: Dict, all_chapters: List) -> List:
    """从 arc 提取 sections（arc 内只存了 id，这里不重复存）"""
    return []  # sections 在 build 时已独立收集


def get_dramatic_context(hierarchy: Dict, chapter_num: int) -> Dict:
    """获取指定章节的戏剧位置上下文（供章节 prompt 注入用）"""
    if not hierarchy or not hierarchy.get('chapters'):
        return {}
    for ch in hierarchy['chapters']:
        if ch.get('chapter_num') == chapter_num:
            # 找到所属 section 和 arc
            sec = None
            for s in hierarchy.get('sections', []):
                if s['id'] == ch['parent_id']:
                    sec = s
                    break
            arc = None
            if sec:
                for a in hierarchy.get('arcs', []):
                    if a['id'] == sec['parent_id']:
                        arc = a
                        break
            return {
                'chapter': ch,
                'section': sec,
                'arc': arc,
                'dramatic_position': ch.get('dramatic_position', ''),
            }
    return {}


def build_dramatic_position_prompt(hierarchy: Dict, chapter_num: int) -> str:
    """构建戏剧位置 prompt 片段（供章节生成注入用）"""
    ctx = get_dramatic_context(hierarchy, chapter_num)
    if not ctx or not ctx.get('dramatic_position'):
        return ''
    ch = ctx['chapter']
    sec = ctx.get('section') or {}
    arc = ctx.get('arc') or {}
    lines = [f'【本章戏剧位置】{ch["dramatic_position"]}']
    if arc.get('arc_name'):
        lines.append(f'所属卷：{arc["arc_name"]}（{arc.get("arc_theme", "")[:30]}）')
    if sec.get('purpose'):
        lines.append(f'所属节：{sec["purpose"]}')
    if sec.get('section_tension'):
        lines.append(f'张力走向：{sec["section_tension"]}')
    return '\n'.join(lines)
