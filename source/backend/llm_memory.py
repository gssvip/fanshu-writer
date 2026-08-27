"""P0-4 长期记忆库（跨会话记住作者偏好/设定摘要）。

技术策略（最小改动 + 跟项目现有能力对齐）：
1) 持久化：app.py 定义的 AIUserMemory 表（db.Model），单用户场景 user_id 恒为 "default"。
2) 向量召回：优先复用 semantic_retriever.embed_batch / _cos_sim（现有 embedding 能力，启用真向量召回）；
   若 embedding 接口不可用（无配置/404/超时），自动 fallback 到"标签+词袋 Jaccard 相似度"，任何情况不打断聊天主流程。
3) 写入触发点（由外部路由直接调用这几个函数即可）：
   - explicit_remember：用户在聊天里说"记住这个/别忘了XX" → 解析上一轮对话写成记忆
   - card_adopted：落卡成功后把卡片摘要写成记忆（跨会话提醒设定不会丢）
   - auto_end_session_summary：空闲 30 分钟 / 用户删会话前 → LLM 总结 3 条以内值得记住的写入
4) 读取触发点：新会话首条消息 → retrieve_topk(message + title) → 返回给前端折叠记忆条，并注入 system。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Dict, Any


# ===========================================================
# 基础 CRUD（对 Flask app context 透明，调用方要在 app_context 内）
# ===========================================================

def add_memory(content: str, *, memory_key: str = '', book_id: Optional[str] = None,
               user_id: str = 'default', tags: Optional[Iterable[str]] = None,
               source: str = 'manual', relevance: float = 1.0) -> Any:
    """写一条记忆并提交到 DB；返回 AIUserMemory 实例（调用方可 to_dict）。

    空 content 不写，返回 None。
    """
    if not (content or '').strip():
        return None
    from app import db, AIUserMemory
    mem = AIUserMemory(
        user_id=user_id or 'default',
        book_id=book_id,
        memory_key=(memory_key or '').strip()[:120],
        content=content.strip(),
        source=(source or 'manual')[:50],
        relevance=float(relevance or 1.0),
    )
    mem.set_tags(list(tags or []))
    db.session.add(mem)
    db.session.commit()
    return mem


def soft_delete_memory(memory_id: str, user_id: str = 'default') -> bool:
    """软删：把 relevance 置 0，未来 retrieve_topk 不会再命中。"""
    from app import db, AIUserMemory
    m = AIUserMemory.query.filter_by(id=memory_id, user_id=user_id).first()
    if not m:
        return False
    m.relevance = 0.0
    db.session.commit()
    return True


def set_relevance(memory_id: str, relevance: float, user_id: str = 'default') -> bool:
    """用户点赞/点踩：调整 relevance（1.5 更易命中；0.0 等于删除）。"""
    from app import db, AIUserMemory
    m = AIUserMemory.query.filter_by(id=memory_id, user_id=user_id).first()
    if not m:
        return False
    m.relevance = max(0.0, float(relevance))
    db.session.commit()
    return True


def list_memories(*, user_id: str = 'default', book_id: Optional[str] = None,
                  source: Optional[str] = None, include_deleted: bool = False,
                  limit: int = 500) -> List[Any]:
    """列出所有记忆（给前端记忆管理面板用）。"""
    from app import AIUserMemory
    q = AIUserMemory.query.filter_by(user_id=user_id or 'default')
    if book_id:
        q = q.filter(AIUserMemory.book_id == book_id)
    else:
        # book_id=None 时：返回全局 + 该书 两条合并？ 这里默认返回 user_id 下所有（含任一书）
        pass
    if source:
        q = q.filter(AIUserMemory.source == source)
    if not include_deleted:
        q = q.filter(AIUserMemory.relevance > 0.0)
    rows = q.order_by(AIUserMemory.updated_at.desc()).limit(max(1, int(limit))).all()
    return rows


# ===========================================================
# 向量 / 关键词 召回（优先向量，失败即 fallback 词袋）
# ===========================================================

def _split_cn(text: str) -> List[str]:
    """简易中文分词：2-grams + 英文单词（省掉 jieba 依赖，Jaccard 召回精度够了）。"""
    s = text or ''
    s = re.sub(r'\s+', ' ', s)
    tokens = set(re.findall(r'[A-Za-z0-9_]{2,}', s))
    # 中文 2-gram
    for win in re.findall(r'[\u4e00-\u9fff]{2,}', s):
        for i in range(max(0, len(win) - 1)):
            tokens.add(win[i:i+2])
    return list(tokens)


def _jaccard(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def retrieve_topk(query_text: str, *, user_id: str = 'default', book_id: Optional[str] = None,
                  top_k: int = 5, ignore_ids: Optional[Iterable[str]] = None,
                  min_score: float = 0.05) -> List[Dict[str, Any]]:
    """跟当前 query 最相关的 top_k 条记忆；返回 dict 列表（含 score 字段）。

    优先级：
      1) semantic_retriever.embed_batch → cosine similarity（项目已有能力，真向量）；
      2) fallback：词袋 Jaccard + relevance 权重；
    任何异常（import 失败 / embedding 服务 404 / timeout）→ 静默 fallback 2，不抛错打断聊天。
    过滤掉 ignore_ids（用户本会话点了"忽略这条记忆"），以及 relevance<=0 的软删记忆。
    """
    ignore_set = set(ignore_ids or [])
    if not (query_text or '').strip() or top_k <= 0:
        return []
    from app import AIUserMemory
    q = AIUserMemory.query.filter_by(user_id=user_id or 'default').filter(AIUserMemory.relevance > 0)
    if book_id:
        q = q.filter(db_or_book(book_id))
    candidates = q.order_by(AIUserMemory.updated_at.desc()).limit(500).all()
    if not candidates:
        return []
    candidates = [c for c in candidates if c.id not in ignore_set]
    if not candidates:
        return []

    # 先尝试向量路径（最多 2 秒，超时立刻 fallback）
    scored: List[float] = []
    use_vector = False
    try:
        from semantic_retriever import embed_batch
        texts = [query_text] + [
            (c.memory_key + ' | ' + c.content + ' | ' + ' '.join(c.get_tags() or [])) for c in candidates
        ]
        vecs = embed_batch(texts, timeout=2.2)
        if vecs and len(vecs) == len(texts):
            qv = vecs[0]
            cands_vecs = vecs[1:]
            # cosine
            norms_q = sum(x*x for x in qv) ** 0.5 or 1e-9
            for i, cv in enumerate(cands_vecs):
                dot = sum(a*b for a, b in zip(qv, cv))
                norms_c = sum(x*x for x in cv) ** 0.5 or 1e-9
                base = dot / (norms_q * norms_c)
                rel = float(getattr(candidates[i], 'relevance', 1.0) or 1.0)
                scored.append(base * (0.7 + 0.3 * rel))  # relevance 给 ±30% 加权影响
            use_vector = True
    except Exception:
        scored = []

    if not use_vector:
        q_tok = _split_cn(query_text)
        for c in candidates:
            body = (c.memory_key or '') + ' ' + (c.content or '') + ' ' + ' '.join(c.get_tags() or [])
            j = _jaccard(q_tok, _split_cn(body))
            rel = float(getattr(c, 'relevance', 1.0) or 1.0)
            scored.append(j * (0.6 + 0.4 * rel))

    pairs = [(s, i) for i, s in enumerate(scored) if s >= min_score]
    pairs.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for s, i in pairs[:top_k]:
        c = candidates[i]
        item = c.to_dict()
        item['score'] = round(float(s), 4)
        out.append(item)
    return out


def db_or_book(book_id: str):
    """SQLAlchemy filter：某书相关记忆 + 全局记忆（book_id is None）。"""
    from app import db
    return db.or_(AIUserMemory.book_id == book_id, AIUserMemory.book_id.is_(None))


# ===========================================================
# 总结写入：会话结束时的自动总结记忆（调用方提供 LLMGateway 实例以减少配置耦合）
# ===========================================================

def summarize_and_remember_end_session(history: List[Dict[str, Any]], *, gw=None,
                                       book_id: Optional[str] = None,
                                       user_id: str = 'default',
                                       max_items: int = 3) -> int:
    """LLM 总结对话里"跨会话也值得记住的"最多 max_items 条，写入 source=auto_end_session_summary。

    - gw 为 None 或任何异常时：静默返回 0（不抛错打断任何主流程）。
    - 只写"事实/偏好/已达成结论"，禁止写闲聊废话；每条记忆 ≤200 字。
    返回实际写入的条数。
    """
    if not history or max_items <= 0:
        return 0
    if gw is None:
        return 0
    # 仅保留最近 20 条（避免太长），拼 text
    recent = list(history[-20:])
    turns = []
    for h in recent:
        r = h.get('role') or '?'
        c = (h.get('content') or '')[:1000]
        turns.append(f'{r}: {c}')
    text = '\n'.join(turns)
    prompt = f"""你是一个"值得跨会话记住"的记忆抽取器。阅读下面聊天记录，提取 1-{max_items} 条"以后作者再回来继续创作/讨论时，应该还知道的事实/偏好/约定/已落库设定摘要/已达成的结论"。
要求：
- 每条不超过 80 字，绝对不要把废话/客套/闲聊抽进来；
- 如果没有任何值得长期记住的内容，回复"空"字即可；
- 输出格式：JSON 数组 [{{"k": 记忆key(8-20字), "v": 记忆正文, "t": ["标签1","标签2"]}}, ...]，不要任何其他文字、不要 markdown 代码块。
聊天记录：
{text}
"""
    try:
        from llm_gateway import ModelResult
        res: ModelResult = gw.chat([{'role': 'user', 'content': prompt}], temperature=0.1, max_tokens=700)
        raw = (res.text or '').strip()
        if not raw or raw == '空':
            return 0
        # 去掉 ```json 代码块包裹（常见 LLM 乱加）
        raw = _strip_code_fence(raw)
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return 0
        n = 0
        for item in arr:
            if not isinstance(item, dict):
                continue
            v = (item.get('v') or '').strip()[:200]
            if not v:
                continue
            k = (item.get('k') or '').strip()[:120] or '跨会话事实'
            t = item.get('t') if isinstance(item.get('t'), list) else []
            t_list = [str(x).strip() for x in t if str(x).strip()][:8]
            if add_memory(v, memory_key=k, book_id=book_id, user_id=user_id, tags=t_list,
                          source='auto_end_session_summary', relevance=1.0):
                n += 1
        return n
    except Exception:
        return 0


def remember_from_user_explicit(text: str, last_ai_reply: str = '', *,
                                book_id: Optional[str] = None, user_id: str = 'default') -> Any:
    """用户显式说"记住这个/把这个存一下" → 解析内容写记忆。

    启发式：如果 text 里有具体"记住X"的描述 → 用描述；否则 fallback 用 last_ai_reply 做内容。
    """
    raw = (text or '').strip()
    content = ''
    key = '用户指定记住'
    # 正则抽取 "记住 XXX"
    m = re.search(r'记[住下录保存]+[:：\s]*([\s\S]{1,400}?)(?:[。？！!?；;\n]|$)', raw)
    if m:
        content = m.group(1).strip()
        key = '用户指定记住'
    if not content:
        content = (last_ai_reply or '').strip()[:1000]
    if not content:
        return None
    tags = ['用户指定']
    if book_id:
        tags.append('该书设定')
    return add_memory(content, memory_key=key, book_id=book_id, user_id=user_id, tags=tags,
                      source='explicit_remember', relevance=1.2)


def _strip_code_fence(s: str) -> str:
    s = (s or '').strip()
    if s.startswith('```'):
        s = re.sub(r'^```(?:json|JSON)?\s*', '', s, count=1)
        if s.endswith('```'):
            s = s[:-3].strip()
    return s.strip()
