"""会话消息持久化模块（从 chat_collab_bp.py 拆出，架构门禁：巨石禁止增长）。

职责：
  - load_session_messages：读取 AISession 消息历史（兼容旧格式）
  - _compact_history_for_persist：落盘前瘦身（PG 小包安全线以内）
  - _safe_save_session_messages：落盘 + PG SSL 断连重试
  - _save_partial_on_disconnect：SSE 客户端断开（GeneratorExit）时抢救已生成内容

历史包袱说明（为何 48KB 上限）：session 存完整卡片内容时一次 UPDATE 100~300KB，
Render/Neon PG 代理会掐断 SSL 连接，故落盘前必须瘦身（卡片只留元信息）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def load_session_messages(session) -> list[dict]:
    """从 AISession 加载消息（兼容旧 messages_json 和新 messages 关联）。"""
    msgs = []
    try:
        msgs = json.loads(session.messages_json or '[]')
    except Exception:
        msgs = []
    return msgs if isinstance(msgs, list) else []


# ============================================================================
# 会话历史瘦身 + 安全提交（解决 messages_json 150KB+ 导致 PG SSL 断连）
# 根因：session 存完整卡片内容，一次 UPDATE 100~300KB，Render/Neon PG 代理掐断 SSL 连接。
# 解法：① 落盘前瘦身（卡片只留元信息，单条截 800 字，12 轮/48KB 上限）
#       ② 断连异常 rollback → dispose 死连接 → 重查 session → 重试一次。
# ============================================================================

# 落盘前单条消息最大字符（正文写作单条 AI assistant 内容可能 6000+ 字，会超）
_PERSIST_MSG_MAX_CHARS = 800
# 落盘前卡片内容最大字符（卡片正文其实会落地到 Chapter/BookBible，session 里仅作回显，不需要完整）
_PERSIST_CARD_CONTENT_MAX_CHARS = 120
# 落盘前最多保留的"消息条数上限"（12 轮 = 24 条消息；一般够用）
_PERSIST_MAX_MSGS = 24
# 总 JSON 字符硬上限：超过就继续砍中间轮次，直到 ≤ 这个值 或 只剩最后 4 条
_PERSIST_TOTAL_MAX_CHARS = 48 * 1024


def _compact_history_for_persist(history: list) -> list:
    """落盘前瘦身：把历史消息压到 PG 小包安全线以内。

    规则（顺序执行）：
      1. 砍卡片 content：每条 cards[*].content 截断到 120 字（卡片正文已落地在 BookBible/Chapter，session 里不用冗余保存全文）
      2. 砍消息 content：每条 message.content 截断到 800 字
      3. 砍历史深度：只保留最后 24 条消息
      4. 若总 JSON 还超 48KB：循环砍中间消息，直到合规或只剩最后 4 条

    返回：新的 list（不原地修改传入 history，避免影响 SSE 正在发的卡片内容）
    """
    import copy
    if not history:
        return []
    # 深拷贝，防止改到 SSE 还在用的引用
    h = copy.deepcopy(history)
    if not isinstance(h, list):
        return []

    # Step1 + Step2：逐条瘦身
    for m in h:
        if not isinstance(m, dict):
            continue
        # 消息正文截断
        c = m.get('content')
        if isinstance(c, str) and len(c) > _PERSIST_MSG_MAX_CHARS:
            m['content'] = c[:_PERSIST_MSG_MAX_CHARS] + '\n…（会话历史超长已截断，完整内容以采纳落地后的维度/章节为准）'
        # 卡片列表内容截断（最关键，卡片 content 可能是 6000 字正文或 80KB timeline JSON）
        cards = m.get('cards')
        if isinstance(cards, list):
            for c2 in cards:
                if not isinstance(c2, dict):
                    continue
                cc = c2.get('content')
                if isinstance(cc, str) and len(cc) > _PERSIST_CARD_CONTENT_MAX_CHARS:
                    c2['content'] = cc[:_PERSIST_CARD_CONTENT_MAX_CHARS] + '…'

    # Step3：深度限制（保留最后 N 条，避免几十轮对话堆起来）
    if len(h) > _PERSIST_MAX_MSGS:
        h = h[-_PERSIST_MAX_MSGS:]

    # Step4：总字符兜底 —— 还超 48KB 就砍中间消息，保留首尾
    def _total_chars(xs):
        return len(json.dumps(xs, ensure_ascii=False))

    _safety = 0
    while _total_chars(h) > _PERSIST_TOTAL_MAX_CHARS and len(h) > 4 and _safety < 30:
        _safety += 1
        mid = len(h) // 2
        # 砍中间 2 条（一般是一对 user+assistant），加速收敛
        if mid - 1 >= 1:
            del h[mid - 1:mid + 1]
        else:
            del h[mid:mid + 1]
    return h


def _save_partial_on_disconnect(session, label: str, user_note: str, partial_content: str) -> None:
    """SSE 客户端断开（GeneratorExit）时抢救已生成的部分内容。

    线上事故（2026-08-20）：生成一章/一个设定要 1-3 分钟，移动端锁屏/切后台/网络切换
    会掐断 SSE 连接 → generator 收到 GeneratorExit → 旧实现直接丢弃已流出的正文，
    用户只能整章重新生成（再等几分钟 + 再扣一次 LLM token）。
    现把部分内容同步写入会话历史，前端断流后刷新历史即可找回。

    约束：GeneratorExit 上下文禁止 yield（会 RuntimeError），本函数必须纯同步；
    抢救失败必须吞异常——不能让它替代 GeneratorExit 逃逸。
    """
    try:
        if not partial_content or not partial_content.strip():
            return
        history = load_session_messages(session)
        history.append({'role': 'user', 'content': f'{label}：{user_note or ""}'[:120]})
        history.append({
            'role': 'assistant',
            'content': (f'【连接中断·已保留生成到一半的内容（约 {len(partial_content)} 字），'
                        f'可点击重试重新生成】\n{partial_content}'),
        })
        _safe_save_session_messages(session, history)
    except Exception:
        pass


def _safe_save_session_messages(session, history: list) -> None:
    """会话消息落盘 + 处理 PG SSL 断连（OperationalError）重试。

    流程：
      1. 对 history 做瘦身（卡片/消息截断、深度限制、48KB 总上限）
      2. 设置 session.messages_json / updated_at 并 commit
      3. 命中 OperationalError（连接被掐断）时：
         - rollback → engine.dispose() 扔僵尸连接 → 重查 session → 再 commit 1 次
    """
    from sqlalchemy.exc import OperationalError as SAOperationalError
    from app import db as _db, app as _app

    slim_history = _compact_history_for_persist(history)
    session.messages_json = json.dumps(slim_history, ensure_ascii=False)
    session.updated_at = datetime.now(timezone.utc)

    def _do_commit(sess_obj):
        _db.session.add(sess_obj)
        _db.session.commit()

    try:
        _do_commit(session)
    except SAOperationalError as e1:
        try:
            _db.session.rollback()
        except Exception:
            pass
        # dispose 扔掉池中所有连接（彻底重置 SSL 管道）
        try:
            _db.get_engine(_app).dispose()
        except Exception:
            pass
        # 新连接重查 session，再提交一次
        try:
            from app import AISession
            sess2 = AISession.query.get(session.id)
            if sess2 is None:
                raise
            sess2.messages_json = json.dumps(_compact_history_for_persist(history), ensure_ascii=False)
            sess2.updated_at = datetime.now(timezone.utc)
            # 如果调用方还改了 session.title（例如 _persist_action_session），同步过去
            if getattr(session, 'title', None):
                sess2.title = session.title
            _do_commit(sess2)
            try:
                session.messages_json = sess2.messages_json
                session.updated_at = sess2.updated_at
                if getattr(sess2, 'title', None):
                    session.title = sess2.title
            except Exception:
                pass
        except Exception as e2:
            raise RuntimeError(
                f'Session 保存失败（首次 {type(e1).__name__}: {e1}；重试 {type(e2).__name__}: {e2}）'
            )
