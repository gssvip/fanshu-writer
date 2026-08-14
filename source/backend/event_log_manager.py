# -*- coding: utf-8 -*-
"""事件日志管理器（M1a）

每章正文入库后，自动抽取结构化事件追加到 BookBible.event_log_json。
事件是全书跨维度协作的"单一真相源"之一：
  - 伏笔系统通过事件找"本章埋/收了什么"
  - 实体注册表通过事件发现新人/地/物/势力
  - 上下文系统通过事件给后续章提供"最新动态"
  - 防遗忘系统通过事件检测"设定是否被违背"

设计原则：
  - 事件抽取轻量优先：先走规则/LLM 粗抽，不阻塞入库
  - 事件 id 全局唯一（e{book_id_short}_{chapter}_{seq}）
  - 与 chapter_changes_processor 的 CHANGES 互补：CHANGES 是 LLM 自我声明，EventLog 是系统二次抽取
"""
import json
import re
import uuid
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any


@dataclass
class StoryEvent:
    id: str
    chapter_num: int
    chapter_id: str
    volume_index: int
    type: str  # setup/payoff/turn/character_state/location_state/faction_state/item/time/conflict
    summary: str
    actors: List[str]
    location: str = ''
    hooks: List[str] = None  # 关联伏笔 id 列表
    weight: int = 5          # 1-10，主线事件 ≥7
    tags: List[str] = None
    extracted_at: str = ''

    def __post_init__(self):
        if self.hooks is None:
            self.hooks = []
        if self.tags is None:
            self.tags = []

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'StoryEvent':
        return cls(
            id=data.get('id', ''),
            chapter_num=data.get('chapter_num', 0),
            chapter_id=data.get('chapter_id', ''),
            volume_index=data.get('volume_index', 0),
            type=data.get('type', 'setup'),
            summary=data.get('summary', ''),
            actors=data.get('actors', []),
            location=data.get('location', ''),
            hooks=data.get('hooks', []),
            weight=data.get('weight', 5),
            tags=data.get('tags', []),
            extracted_at=data.get('extracted_at', ''),
        )


class EventLogManager:
    """管理 BookBible.event_log_json 的增删改查"""

    @staticmethod
    def load(bb) -> List[StoryEvent]:
        if not bb or not bb.event_log_json:
            return []
        try:
            arr = json.loads(bb.event_log_json)
            if isinstance(arr, list):
                return [StoryEvent.from_dict(x) for x in arr if isinstance(x, dict)]
        except Exception:
            pass
        return []

    @staticmethod
    def save(bb, events: List[StoryEvent]):
        if not bb:
            return
        bb.event_log_json = json.dumps([e.to_dict() for e in events], ensure_ascii=False)

    @staticmethod
    def find_by_chapter(events: List[StoryEvent], chapter_num: int) -> List[StoryEvent]:
        return [e for e in events if e.chapter_num == chapter_num]

    @staticmethod
    def find_by_actor(events: List[StoryEvent], actor: str, limit: int = 20) -> List[StoryEvent]:
        hits = [e for e in events if actor in e.actors]
        return sorted(hits, key=lambda x: x.chapter_num, reverse=True)[:limit]

    @staticmethod
    def find_latest(events: List[StoryEvent], before_chapter: int, limit: int = 10) -> List[StoryEvent]:
        hits = [e for e in events if e.chapter_num < before_chapter]
        return sorted(hits, key=lambda x: x.chapter_num, reverse=True)[:limit]


# ---------- 轻量规则抽取（零 LLM 成本，覆盖常见句型） ----------

_SETUP_PATTERNS = [
    re.compile(r'(.{2,20}?)似乎.{2,30}?不寻常'),
    re.compile(r'(.{2,20}?)不知道的是'),
    re.compile(r'(.{2,20}?)埋下了.*?种子'),
    re.compile(r'(.{2,20}?)暗中.{2,20}'),
    re.compile(r'那枚|那块|那柄|那本|那卷|那枚(.{2,15}?)'),
]

_TURN_PATTERNS = [
    re.compile(r'(.{2,15}?)终于.{2,30}(?:发现|明白|知道|察觉)'),
    re.compile(r'(.{2,15}?)万万没想到'),
    re.compile(r'(.{2,15}?)突然.{2,20}(?:出现|杀出|降临|现身)'),
    re.compile(r'(.{2,15}?)反(?:转|水|叛)'),
]


def _extract_actors_heuristic(text: str, known_actors: List[str]) -> List[str]:
    """基于已知人物名做整词匹配，返回本章出现的人物"""
    actors = []
    for name in known_actors:
        if name and len(name) >= 2 and name in text:
            actors.append(name)
    # 去重，保持出现顺序
    seen = set()
    result = []
    for a in actors:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


def _extract_locations_heuristic(text: str, known_locations: List[str]) -> str:
    """返回本章最可能的主要地点（简单取第一个命中）"""
    for loc in known_locations:
        if loc and loc in text:
            return loc
    return ''


def rule_extract_events(chapter_num: int, chapter_id: str, volume_index: int,
                        content: str, known_actors: List[str] = None,
                        known_locations: List[str] = None) -> List[StoryEvent]:
    """基于规则快速抽取事件。用于 LLM 不可用时兜底，或作为 LLM 抽取的候选。"""
    events = []
    known_actors = known_actors or []
    known_locations = known_locations or []
    actors = _extract_actors_heuristic(content, known_actors)
    location = _extract_locations_heuristic(content, known_locations)

    # 1. 转折事件：取前 3 个匹配
    for i, pat in enumerate(_TURN_PATTERNS):
        for m in pat.finditer(content):
            events.append(StoryEvent(
                id=f'e{chapter_num}_{len(events):03d}',
                chapter_num=chapter_num,
                chapter_id=chapter_id,
                volume_index=volume_index,
                type='turn',
                summary=m.group(0)[:120],
                actors=actors[:3],
                location=location,
                weight=8,
                tags=['heuristic', 'turn'],
            ))
            if len(events) >= 5:
                break
        if len(events) >= 5:
            break

    # 2. 埋伏事件：取前 2 个
    for i, pat in enumerate(_SETUP_PATTERNS):
        for m in pat.finditer(content):
            events.append(StoryEvent(
                id=f'e{chapter_num}_{len(events):03d}',
                chapter_num=chapter_num,
                chapter_id=chapter_id,
                volume_index=volume_index,
                type='setup',
                summary=m.group(0)[:120],
                actors=actors[:3],
                location=location,
                weight=6,
                tags=['heuristic', 'setup'],
            ))
            if len(events) >= 3:
                break
        if len(events) >= 3:
            break

    # 3. 若什么都没抽到，生成一个"本章概要"占位事件，保证 EventLog 持续可用
    if not events and content:
        summary = content[:100].replace('\n', ' ') + '…' if len(content) > 100 else content
        events.append(StoryEvent(
            id=f'e{chapter_num}_{len(events):03d}',
            chapter_num=chapter_num,
            chapter_id=chapter_id,
            volume_index=volume_index,
            type='chapter_summary',
            summary=summary,
            actors=actors[:3],
            location=location,
            weight=3,
            tags=['fallback'],
        ))

    return events[:6]


# ---------- LLM 抽取（精确但成本高，章节入库后异步/同步调用） ----------

LLM_EXTRACT_PROMPT = """你是小说事件抽取器。请从以下章节正文中抽取关键事件，输出 JSON 数组。

事件类型：
- setup：埋下伏笔/线索/未解释的异常（读者当时不懂，后续会回收）
- payoff：回收/揭示前文伏笔
- turn：剧情转折、身份揭露、重大决定、意外发生
- character_state：角色状态/关系/立场显著变化
- location_state：地点状态/归属变化
- item：重要物品/功法/法宝出现、转移、损毁
- conflict：冲突升级或解决

输出格式（严格 JSON 数组，不要解释）：
[
  {
    "type": "setup",
    "summary": "事件一句话摘要（30字内）",
    "actors": ["角色名1", "角色名2"],
    "location": "地点名",
    "weight": 7,
    "tags": ["伏笔"]
  }
]

要求：
1. 只抽最重要的 3-6 个事件，避免琐碎
2. 有伏笔感的内容优先标 setup
3. 回收前文谜底的标 payoff
4. weight 1-10：主线转折 8-10，普通事件 3-5

章节正文：
{content}
"""


def llm_extract_events(chapter_num: int, chapter_id: str, volume_index: int,
                       content: str, gw=None,
                       base_url: str = '', api_key: str = '', model: str = '') -> List[StoryEvent]:
    """用 LLM 抽取事件。gw 为 LLMGateway 实例，或传 base_url/api_key/model 内部创建。"""
    if not content or len(content) < 50:
        return []
    try:
        if gw is None:
            from llm_gateway import LLMGateway
            gw = LLMGateway(base_url, api_key, model)
        prompt = LLM_EXTRACT_PROMPT.format(content=content[:3000])
        messages = [{'role': 'system', 'content': prompt},
                    {'role': 'user', 'content': '请抽取事件，返回 JSON 数组'}]
        resp = gw.chat(messages, temperature=0.3, max_tokens=1200)
        if not resp:
            return []
        # 尝试从 resp 中解析 JSON
        raw = resp.strip()
        if raw.startswith('```'):
            m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
            if m:
                raw = m.group(1).strip()
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return []
        events = []
        for idx, item in enumerate(arr):
            if not isinstance(item, dict):
                continue
            events.append(StoryEvent(
                id=f'e{chapter_num}_{idx:03d}',
                chapter_num=chapter_num,
                chapter_id=chapter_id,
                volume_index=volume_index,
                type=item.get('type', 'setup'),
                summary=item.get('summary', '')[:120],
                actors=item.get('actors', [])[:5],
                location=item.get('location', ''),
                hooks=item.get('hooks', []),
                weight=max(1, min(10, int(item.get('weight', 5)))),
                tags=item.get('tags', ['llm']),
            ))
        return events
    except Exception:
        return []


# ---------- 统一入口：章节入库后调用 ----------

def append_chapter_events(bb, chapter, content: str,
                          known_actors: List[str] = None, known_locations: List[str] = None,
                          use_llm: bool = False, gw=None,
                          base_url: str = '', api_key: str = '', model: str = '') -> Dict:
    """章节入库后：抽取事件 → 追加 EventLog → 更新 Chapter.events_extracted_json。
    返回 {'events_added': int, 'event_ids': [...]}
    """
    if not bb or not chapter:
        return {'events_added': 0, 'event_ids': []}

    chapter_num = chapter.order_index
    chapter_id = chapter.id
    volume_index = 0
    # 尝试从 parent_id 算卷号；parent_id 是卷章节 id
    try:
        from app import Chapter
        vol_ch = Chapter.query.get(chapter.parent_id) if chapter.parent_id else None
        if vol_ch and vol_ch.is_volume:
            # 卷标题可能含卷号
            m = re.search(r'(\d+)', vol_ch.title or '')
            volume_index = int(m.group(1)) if m else 0
    except Exception:
        pass

    # 抽取
    if use_llm and base_url and api_key and model:
        events = llm_extract_events(
            chapter_num, chapter_id, volume_index, content,
            gw=gw, base_url=base_url, api_key=api_key, model=model)
    else:
        events = rule_extract_events(
            chapter_num, chapter_id, volume_index, content,
            known_actors=known_actors, known_locations=known_locations)

    if not events:
        return {'events_added': 0, 'event_ids': []}

    # 追加
    all_events = EventLogManager.load(bb)
    # 先去重：同一章已存在的事件先移除（覆盖更新）
    all_events = [e for e in all_events if e.chapter_num != chapter_num]
    all_events.extend(events)
    all_events.sort(key=lambda e: (e.chapter_num, e.id))
    EventLogManager.save(bb, all_events)

    # 更新章节索引
    event_ids = [e.id for e in events]
    chapter.events_extracted_json = json.dumps(event_ids, ensure_ascii=False)

    # M1b: 事件入库后同步到实体注册表
    try:
        from entity_registry import register_event_entities, register_chapter_entities
        register_event_entities(bb, [e.to_dict() for e in events], source_chapter=chapter_num)
        register_chapter_entities(bb, chapter_num, content, known_actors=known_actors)
    except Exception:
        pass

    return {'events_added': len(events), 'event_ids': event_ids}
