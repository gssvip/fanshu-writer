"""通用聊天命中维度识别器。

目标：用户和智驾闲聊时，一旦内容"像"创作相关（构思/设定/大纲/文风/世界观/剧情/人物等），
就输出"命中提示"告诉前端：这段内容建议落入哪个维度，前端弹出气泡让用户一键采纳（走apply-card）。

核心思路（零LLM成本快路径 + 可选LLM验证）：
1. 快路径：多组中文关键词 + 正则匹配维度。命中立即生成 suggestion（0成本，亚毫秒级）。
2. 置信度：单关键词命中=低，短语命中=中，多维度关键词交叉命中=高。
3. suggestion 格式走 ActionCard 子集，和现有 apply-card 接口无缝对接。

被 chat_collab_bp.chat_general 路由调用，零侵入现有逻辑。
"""
from __future__ import annotations
import re
import json
import uuid
from typing import Any


# ============================================================================
# 维度命中词库：KEYWORD（只要出现其中≥1个就算命中该维度）+ PHRASE（≥1个短语或组合命中提置信度）
# ============================================================================
DIM_HIT_RULES: dict[str, dict[str, list[str]]] = {
    'concept': {
        'label': '核心构思',
        'card_type': 'SAVE_CONCEPT',
        'keywords': ['构思', '创意', '点子', '核心梗', '一句话梗', '故事梗概', '一句话简介',
                     '核心理念', '卖点', '核心卖点', '一句话故事', '故事灵感', '灵感',
                     '主线', '故事内核', '核心冲突', '选题', '题材定位'],
        'phrases': ['我想写一个', '打算写', '故事讲的是', '主角是一个', '金手指设定', '身份是'],
    },
    'worldbuilding': {
        'label': '世界观',
        'card_type': 'SAVE_WORLDSETTING',
        'keywords': ['世界观', '能量体系', '修炼体系', '异能等级', '阶位', '境界', '势力分布',
                     '社会结构', '社会分层', '国家', '宗门', '城市设定', '地理', '世界背景',
                     '时代背景', '历史设定', '货币体系', '功法等级', '武道等级', '修仙等级'],
        'phrases': ['在这个世界里', '这个世界', '大陆分为', '总共有.*种境界', '异能分.*级'],
    },
    'character_profiles': {
        'label': '人物',
        'card_type': 'SAVE_CHARACTER',
        'keywords': ['人物', '角色', '主角', '女主', '女配', '男配', '配角', '反派', '人物小传',
                     '人设', '性格', '身份', '姓名', '名字', '背景', '人物关系', '人物设定',
                     '角色设定', '画像', '外貌', '职业'],
        'phrases': ['名叫', '姓.*名', '性格.*的人', '身世', '曾经是', '和主角.*关系'],
    },
    'plot_design': {
        'label': '大纲',
        'card_type': 'SAVE_OUTLINE_NODE',
        'keywords': ['大纲', '目录', '分卷', '章节规划', '章纲', '卷纲', '情节框架', '整体结构',
                     '剧情走向', '脉络', '结构', '总纲'],
        'phrases': ['第一卷', '第.*卷', '前一百章', '第一阶段', '第二阶段', '主线剧情'],
    },
    'timeline': {
        'label': '剧情线',
        'card_type': 'SAVE_PLOT',
        'keywords': ['剧情', '事件', '情节', '主线剧情', '支线', '反转', '伏笔回收',
                     '剧情发展', '事件顺序', '这场戏', '桥段'],
        'phrases': ['然后', '接着', '之后', '突然', '结果是', '导致', '因为所以'],
    },
    'foreshadowing': {
        'label': '伏笔',
        'card_type': 'SAVE_FORESHADOW',
        'keywords': ['伏笔', '暗线', '铺垫', '铺垫好', '埋伏', '回收伏笔', '隐藏线索',
                     '草蛇灰线', '悬念', '钩子', '挖坑', '填坑'],
        'phrases': ['将来会', '以后会', '原来如此', '埋下', '未来揭晓', '后面再讲'],
    },
    'style_guide': {
        'label': '文风',
        'card_type': 'APPLY_STYLE',
        'keywords': ['文风', '笔风', '笔调', '写作风格', '节奏', '对白占比', '句式', '叙述节奏',
                     '爽文', '虐文', '种田文', '搞笑风', '轻松风', '正剧风', '压抑',
                     '短句', '长句', '段落', '描写', '第一人称', '第三人称', '文笔'],
        'phrases': ['对话多一些', '多一些场景', '节奏要快', '慢热', '幽默一点', '不要那么严肃'],
    },
    'key_rules': {
        'label': '核心规则',
        'card_type': 'SAVE_RULE',
        'keywords': ['规则', '铁律', '设定', '不能做', '绝对不能', '必须', '约定', '核心机制',
                     '金手指规则', '系统规则', '代价', '反噬'],
        'phrases': ['只要.*就', '一旦.*就', '只有.*才', '不允许', '禁止', '严格'],
    },
    'locations': {
        'label': '地点',
        'card_type': 'SAVE_LOCATION',
        'keywords': ['地点', '场景', '地方', '校园', '都市', '家族', '公司', '工厂', '秘境',
                     '副本', '废墟', '基地', '城市', '街区', '公园', '医院', '学校', '山脉'],
        'phrases': ['位于', '坐落在', '在.*市', '在.*学院', '一个.*的地方'],
    },
}

# 写作关键词全集（命中任意1个+命中某维度关键词 = 提高suggestion优先级）
WRITING_TOTAL_HINTS = ['写一本', '写小说', '写故事', '创作', '写一部', '想写', '我想写一本',
                       '这本书', '这本小说', '故事里', '主角', '剧情', '世界观', '大纲', '文风']


def _score_text(text: str, rules: dict) -> tuple[float, list[str]]:
    """单维度打分：返回(0-1置信度, 命中词列表)。"""
    if not text:
        return 0.0, []
    hits = []
    for kw in rules.get('keywords', []):
        if kw and kw in text:
            hits.append(kw)
    kw_score = min(1.0, len(hits) * 0.2)
    phrase_hit = 0
    for ph in rules.get('phrases', []):
        try:
            if re.search(ph, text):
                phrase_hit += 1
                hits.append(f'[短语]{ph}')
        except re.error:
            pass
    phrase_score = min(1.0, phrase_hit * 0.35)
    conf = 0.0
    if kw_score > 0 or phrase_score > 0:
        conf = min(0.95, 0.2 + kw_score * 0.4 + phrase_score * 0.5 + (len(hits) >= 3) * 0.1)
    return conf, hits


def detect_dimension_hits(text: str, threshold: float = 0.35) -> list[dict]:
    """识别用户消息命中的维度，返回建议列表（按置信度从高到低排序）。

    返回结构（直接喂前端命中提示组件）：
      [{ dim, label, card_type, confidence, hits: [...], suggested_title }]
    """
    if not text or len(text.strip()) < 3:
        return []
    text = text.strip()
    # 用户消息里出现"扫榜/起点/番茄/七猫/实时热榜" = 工具调用意图，不建议落维度
    tool_intents = ['扫榜', '热榜', '爆款', '番茄小说', '起点中文', '七猫', '排行榜', '什么火']
    if any(ti in text for ti in tool_intents):
        return []

    global_writing_flag = any(h in text for h in WRITING_TOTAL_HINTS)
    suggestions = []
    for dim, rules in DIM_HIT_RULES.items():
        conf, hits = _score_text(text, rules)
        if global_writing_flag and conf > 0 and conf < threshold:
            conf = min(0.95, conf + 0.18)  # 上下文是"聊写书"时：降低提建议门槛
        if conf >= threshold:
            suggestions.append({
                'dim': dim,
                'label': rules['label'],
                'card_type': rules['card_type'],
                'confidence': round(conf, 3),
                'hits': hits,
                'suggested_title': f'来自通用聊天的{rules["label"]}草稿',
            })
    suggestions.sort(key=lambda x: x['confidence'], reverse=True)
    # 置信度接近且维度相似的只留最高的（比如concept/plot_design同时命中取高者，避免建议轰炸）
    dedup, seen_groups = [], []
    for s in suggestions:
        dim_group = {s['dim']}
        # 相近维度簇：concept/plot_design/timeline 三类合并；worldbuilding/key_rules合并
        if s['dim'] in ('concept', 'plot_design', 'timeline'):
            dim_group = {'concept', 'plot_design', 'timeline'}
        if s['dim'] in ('worldbuilding', 'key_rules', 'locations'):
            dim_group = {'worldbuilding', 'key_rules', 'locations'}
        if any(dim_group & g for g in seen_groups) and s['confidence'] < 0.7:
            continue
        seen_groups.append(dim_group)
        dedup.append(s)
        if len(dedup) >= 3:  # 最多3条建议，用户友好
            break
    return dedup


def wrap_message_with_context(user_message: str, book_title: str = '', bb_snapshot: str = '') -> str:
    """给通用聊天用户消息加前导引用（当命中写作相关关键词时），注入当前书 bible 快照。"""
    if not user_message:
        return user_message
    writing_talk = any(h in user_message for h in WRITING_TOTAL_HINTS)
    if not writing_talk:
        return user_message
    pre = '（以下为系统从当前作品库提取的背景资料，仅当你回答写作相关问题时参考；闲聊类问题直接忽略。\n'
    if book_title:
        pre += f'当前作品：《{book_title}》\n'
    if bb_snapshot:
        pre += f'已填充维度摘要：{bb_snapshot}\n'
    pre += '————————\n【作者原话】\n'
    return pre + user_message


def build_general_chat_system_prompt() -> str:
    """通用聊天模式system prompt：
    - 不强制创作上下文，可聊任何话题；
    - 涉及写作时给出精准回答+必要时产落地卡片；
    - 用户提到"扫榜/爆款/趋势"时明确引导用户走 Step1 实时扫榜工具（不瞎编榜单）。
    """
    return '''你是一个名为「智驾」的创作协作伙伴（通用聊天模式）。

一、闲聊自由
- 可讨论任何话题：生活、天气、学习、闲聊、娱乐、编程、科普，不限于创作。
- 回答要简洁、自然、有人情味，不要说教式长文。
- 不要把所有回答都往写作上扯，除非用户明确聊创作。

二、命中创作话题时的行为
- 用户聊构思、人物、剧情、世界观、文风、大纲时：给出具体、可落地的建议。
- 讨论出明确可落地的结论时，在回复末尾使用落地卡片协议，格式为：
  [[CARD:卡片类型|标题|具体内容]]
  支持的卡片类型和使用规则与创作模式完全一致，包括：
  SAVE_CONCEPT(核心构思) / SAVE_WORLDSETTING(世界观) / SAVE_CHARACTER(人物) /
  SAVE_OUTLINE_NODE(大纲) / SAVE_PLOT(剧情线) / SAVE_FORESHADOW(伏笔) /
  SAVE_RULE(核心规则) / APPLY_STYLE(文风) / SAVE_LOCATION(地点) / SAVE_CHAPTER(章节)。
- 不要每条回复都产卡片；只有真正形成可直接写入设定库的结论时才产。

三、爆款与扫榜
- 当用户问"现在什么火""帮我扫番茄/起点/七猫榜""帮我找爆款""生成方案前先看趋势"这类需求时，
  **绝对不要自己编榜单**；直接回答：「我帮你走实时扫榜工具，请先告诉我你想扫的题材（如都市异能、系统文、玄幻高武），可选给3-5本参考书名」。
  然后等用户补充题材，用户给题材后回答「正在实时联网扫榜…（这里交给 Step1 工具接管，不要自己输出内容）」。
- 若用户已明确给题材且请求扫榜，回复里仅说明"已识别到扫榜需求，进入Step1"，随后停止生成文本，由 Step1 工具接管输出。

四、不要
- 不要在用户没聊创作时主动提创作。
- 不要输出自检清单、协作口吻、修改说明、"我来给你分析下"这类AI腔。
- 不要堆砌解释句、路标词。
- 不要编造排行榜、书籍数据、读者评论，一旦涉及"什么火"请坚持走 Step1 联网工具。
'''.strip()
