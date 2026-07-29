"""
节拍模板加载器（P1-5）
按章节戏剧位置加载节拍模板，构建 prompt 注入片段。

参考：Openwrite beat_templates
设计原则：
  - 配置与代码分离，模板可热更新
  - 字数决定节拍数，戏剧位置决定节拍内容
  - 伏笔融入规则与 P0-2 DAG 联动
"""
import os
import yaml
from typing import Dict, List, Optional, Any

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'beat_templates.yaml')
_template_cache: Dict = {}


def _load_templates() -> Dict:
    """加载节拍模板配置（带缓存）"""
    global _template_cache
    if _template_cache:
        return _template_cache
    try:
        with open(_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            _template_cache = yaml.safe_load(f) or {}
    except Exception:
        _template_cache = {'templates': {}, 'word_count_beats': {'default': 4}}
    return _template_cache


def get_beat_count(word_count: int) -> int:
    """根据字数返回节拍数"""
    cfg = _load_templates()
    wc_map = cfg.get('word_count_beats', {})
    # 找到不超过 word_count 的最大档位
    applicable = [(int(k), v) for k, v in wc_map.items() if k != 'default' and int(k) <= word_count]
    if applicable:
        return max(applicable)[1]
    return wc_map.get('default', 4)


def build_beat_prompt(dramatic_position: str, word_count: int = 2500) -> str:
    """构建节拍模板 prompt 片段。
    dramatic_position: 起/承/转/合/过渡
    word_count: 目标字数"""
    if not dramatic_position:
        return ''
    cfg = _load_templates()
    templates = cfg.get('templates', {})
    template = templates.get(dramatic_position)
    if not template:
        return ''

    beat_count = get_beat_count(word_count)
    beats = template.get('beats', [])[:beat_count]

    lines = [f'【章内节拍模板·{dramatic_position}】{template.get("name", "")}']
    lines.append(f'说明：{template.get("description", "")}')
    lines.append('请按以下节拍展开本章（节拍数为字数决定，每拍有字数预算和写作指引）：')
    for i, beat in enumerate(beats, 1):
        name = beat.get('name', f'节拍{i}')
        budget = beat.get('budget', 500)
        guide = beat.get('guide', '')
        lines.append(f'{i}. 【{name}】（约{budget}字）{guide}')

    # 伏笔融入规则
    fs_rules = cfg.get('foreshadowing_rules', {})
    if dramatic_position in fs_rules:
        lines.append(f'伏笔融入：{fs_rules[dramatic_position]}')

    return '\n'.join(lines)


def build_scene_layer_prompt(scene_type: str) -> str:
    """构建场景叠加层 prompt（如战斗/对话/探索/揭示）"""
    if not scene_type:
        return ''
    cfg = _load_templates()
    layers = cfg.get('scene_layers', {})
    layer = layers.get(scene_type)
    if not layer:
        return ''
    return f'【场景叠加·{layer.get("name", "")}】{layer.get("guide", "")}'
