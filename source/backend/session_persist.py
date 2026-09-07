"""会话消息持久化模块（从 chat_collab_bp.py 拆出，架构门禁：巨石禁止增长）。

职责：
  - load_session_messages：读取 AISession 消息历史（兼容旧格式）
  - _compact_history_for_persist：落盘前瘦身（PG 小包安全线以内）
  - _safe_save_session_messages：落盘 + PG SSL 断连重试
  - _save_partial_on_disconnect：SSE 客户端断开（GeneratorExit）时抢救已生成内容
  - 全量存档（ai_session_msgs 小表）：messages_json 瘦身只服务 LLM 上下文，
    前端回显/刷新恢复一律走存档表——所有智驾生成内容不管是否采纳落地都完整保留。

历史包袱说明（为何 48KB 上限）：session 存完整卡片内容时一次 UPDATE 100~300KB，
Render/Neon PG 代理会掐断 SSL 连接，故落盘前必须瘦身（卡片只留元信息）。
存档表按"每条消息一行"小包写入，天然规避该问题，内容不截断。
"""
from __future__ import annotations

import hashlib
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
# 解法：① 落盘前瘦身（卡片只留元信息；正文不再常规截断，超长改由前端折叠展示）
#       ② 断连异常 rollback → dispose 死连接 → 重查 session → 重试一次。
# ============================================================================

# 落盘前单条消息最大字符：正常消息/单章正文（≤8000字）永不截断（用户要求保留完整内容，前端折叠展示），
# 12000 仅作为异常巨型消息（如整卷 JSON 直排）的 PG 安全阀，保证"只剩最后4条"时总包也不会爆。
_PERSIST_MSG_MAX_CHARS = 12000
# 落盘前卡片内容最大字符（卡片正文其实会落地到 Chapter/BookBible，session 里仅作回显，不需要完整）
_PERSIST_CARD_CONTENT_MAX_CHARS = 120
# 落盘前最多保留的"消息条数上限"（50 轮 = 100 条 + 首条冗余 = 102；匹配通用聊天"最近50条+首条"上下文）
_PERSIST_MAX_MSGS = 102
# 总 JSON 字符硬上限：超过就继续砍中间轮次，直到 ≤ 这个值 或 只剩最后 4 条
_PERSIST_TOTAL_MAX_CHARS = 48 * 1024


def _compact_history_for_persist(history: list) -> list:
    """落盘前瘦身：把历史消息压到 PG 小包安全线以内。

    规则（顺序执行）：
      1. 砍卡片 content：每条 cards[*].content 截断到 120 字（卡片正文已落地在 BookBible/Chapter，session 里不用冗余保存全文）
      2. 消息正文不截断（保留完整内容，超长由前端折叠展示）；仅超 12000 字的异常巨型消息才截断（PG 安全阀）
      3. 砍历史深度：只保留最后 102 条消息
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
        # 消息正文：正常保留完整内容（前端折叠展示）；仅异常巨型消息（>12000字）才截断作 PG 安全阀
        c = m.get('content')
        if isinstance(c, str) and len(c) > _PERSIST_MSG_MAX_CHARS:
            m['content'] = c[:_PERSIST_MSG_MAX_CHARS] + '\n…（异常超长消息已截断）'
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


# ============================================================================
# 全量消息存档（ai_session_msgs 小表）
#
# 背景：messages_json 是"瘦身版"（卡片120字/102条/48KB），只够喂 LLM 上下文；
# 用户要求：智驾生成的所有内容（含未采纳卡片的完整正文）刷新后仍完整可见。
# 方案：每条消息一行写入独立小表（单行 ≤ 单条消息大小，小包 UPDATE/INSERT 天然
#       规避 PG SSL 断连），消息打 _seq 序号与 messages_json 对齐：
#   - _archive_sync：_safe_save 时把 history 全量同步进存档（新消息 INSERT、
#     变更消息 UPDATE；messages_json 瘦身砍掉的旧消息在存档里继续保留，不删）
#   - _archive_load_full：GET /sessions/<id>/messages 存档优先读取
#   - _archive_truncate_after：重新生成（truncate_history_to）时删掉被丢弃的尾部
#   - _archive_replace_all：PUT 全量覆盖 messages 时整体重建
#   - _archive_update_card_status：采纳/忽略/编辑改卡片状态时同步存档
#   - _archive_delete_session / _archive_count_map：删会话清理 / 列表计数
# ============================================================================

_ARCHIVE_TABLE = 'ai_session_msgs'


def _archive_ind_connect():
    """复用圆桌的独立连接工厂（独立于请求 db.session，不受回滚/GeneratorExit 影响；
    chat_collab_bp 顶层 import 本模块，故此处必须函数内延迟导入避免循环）。"""
    from blueprints.chat_collab_bp import _rt_ind_connect
    return _rt_ind_connect()


def _archive_ensure_table(ind_sess) -> None:
    """幂等建存档表（session_id+seq 联合主键，PG/SQLite 双兼容）。"""
    from sqlalchemy import text as _t
    ind_sess.execute(_t(f"""
        CREATE TABLE IF NOT EXISTS {_ARCHIVE_TABLE} (
            session_id VARCHAR(36) NOT NULL,
            seq INTEGER NOT NULL,
            role VARCHAR(16),
            content TEXT,
            cards_json TEXT,
            msg_hash VARCHAR(48),
            updated_at TEXT,
            PRIMARY KEY (session_id, seq)
        )
    """))
    ind_sess.commit()


def _archive_msg_hash(role: str, content: str, cards_json: str) -> str:
    return hashlib.sha1((role + '\x00' + content + '\x00' + cards_json)
                        .encode('utf-8', 'replace')).hexdigest()


def _archive_sync(session, history: list) -> None:
    """把 history（完整版）同步进存档表：
    - 无 _seq 的消息 → 分配递增 _seq 并 INSERT（_seq 同时写回消息 dict，
      随 messages_json 落盘，下轮 load 后可对齐）
    - 有 _seq 且内容变化的 → UPDATE（卡片状态变更等）
    - 存档里多出的行不删（messages_json 瘦身砍掉的旧消息继续保留在存档）
    每行都是单条消息的小包，不会触发 PG SSL 断连；失败静默不影响主流程。
    """
    sid = str(getattr(session, 'id', '') or '').strip()
    if not sid or not isinstance(history, list) or not history:
        return
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        rows = s.execute(
            _t(f"SELECT seq, msg_hash FROM {_ARCHIVE_TABLE} WHERE session_id = :sid"),
            {'sid': sid}).fetchall()
        existing: dict[int, str] = {}
        for r in rows:
            try:
                existing[int(r[0])] = str(r[1] or '')
            except Exception:
                pass
        next_seq = (max(existing) + 1) if existing else 1
        now = datetime.now(timezone.utc).isoformat()
        for m in history:
            if not isinstance(m, dict):
                continue
            role = str(m.get('role') or 'user')
            content = m.get('content')
            if not isinstance(content, str):
                content = '' if content is None else str(content)
            cards = m.get('cards')
            cards_json = ''
            if isinstance(cards, list) and cards:
                try:
                    cards_json = json.dumps(cards, ensure_ascii=False)
                except Exception:
                    cards_json = ''
            h = _archive_msg_hash(role, content, cards_json)
            seq = m.get('_seq')
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                seq = None
            if seq is None:
                seq = next_seq
                next_seq += 1
                m['_seq'] = seq
            if seq in existing:
                if existing[seq] == h:
                    continue
                s.execute(
                    _t(f"UPDATE {_ARCHIVE_TABLE} SET role = :r, content = :c, cards_json = :cj, "
                       f"msg_hash = :h, updated_at = :t WHERE session_id = :sid AND seq = :sq"),
                    {'r': role, 'c': content, 'cj': cards_json, 'h': h, 't': now, 'sid': sid, 'sq': seq})
            else:
                if seq >= next_seq:
                    next_seq = seq + 1
                s.execute(
                    _t(f"INSERT INTO {_ARCHIVE_TABLE} (session_id, seq, role, content, cards_json, msg_hash, updated_at) "
                       f"VALUES (:sid, :sq, :r, :c, :cj, :h, :t)"),
                    {'sid': sid, 'sq': seq, 'r': role, 'c': content, 'cj': cards_json, 'h': h, 't': now})
        s.commit()
    except Exception:
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_load_full(session_id) -> list | None:
    """读全量存档（按 seq 升序，含完整卡片内容）；无存档返回 None（调用方回退 messages_json）。"""
    sid = str(session_id or '').strip()
    if not sid:
        return None
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        rows = s.execute(
            _t(f"SELECT seq, role, content, cards_json FROM {_ARCHIVE_TABLE} "
               f"WHERE session_id = :sid ORDER BY seq"),
            {'sid': sid}).fetchall()
        msgs: list[dict] = []
        for r in rows:
            m: dict = {'role': (r[1] or 'user'), 'content': (r[2] or ''), '_seq': int(r[0])}
            if r[3]:
                try:
                    cards = json.loads(r[3])
                    if isinstance(cards, list) and cards:
                        m['cards'] = cards
                except Exception:
                    pass
            msgs.append(m)
        return msgs if msgs else None
    except Exception:
        return None
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_truncate_after(session, kept_history: list) -> None:
    """重新生成（truncate_history_to）时：删除存档里 seq 大于保留部分最大 _seq 的行，
    避免"旧回复 + 新回复"重复回显。保留部分无 _seq（从未存档）时不删任何行。"""
    sid = str(getattr(session, 'id', '') or '').strip()
    if not sid or not isinstance(kept_history, list):
        return
    max_seq = 0
    for m in kept_history:
        if isinstance(m, dict):
            v = m.get('_seq')
            if isinstance(v, int) and not isinstance(v, bool) and v > max_seq:
                max_seq = v
    if max_seq <= 0:
        return
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        s.execute(_t(f"DELETE FROM {_ARCHIVE_TABLE} WHERE session_id = :sid AND seq > :sq"),
                  {'sid': sid, 'sq': max_seq})
        s.commit()
    except Exception:
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_replace_all(session_id, messages: list) -> None:
    """PUT 全量覆盖会话消息后：存档整体重建（删全部行 + 按序重插 1..N）。
    会给 messages 里的 dict 原地打上 _seq（调用方随后落 messages_json，保持两边对齐）。"""
    sid = str(session_id or '').strip()
    if not sid or not isinstance(messages, list):
        return
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        s.execute(_t(f"DELETE FROM {_ARCHIVE_TABLE} WHERE session_id = :sid"), {'sid': sid})
        now = datetime.now(timezone.utc).isoformat()
        seq = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            seq += 1
            m.pop('_seq', None)
            m['_seq'] = seq
            role = str(m.get('role') or 'user')
            content = m.get('content')
            if not isinstance(content, str):
                content = '' if content is None else str(content)
            cards = m.get('cards')
            cards_json = ''
            if isinstance(cards, list) and cards:
                try:
                    cards_json = json.dumps(cards, ensure_ascii=False)
                except Exception:
                    cards_json = ''
            s.execute(
                _t(f"INSERT INTO {_ARCHIVE_TABLE} (session_id, seq, role, content, cards_json, msg_hash, updated_at) "
                   f"VALUES (:sid, :sq, :r, :c, :cj, :h, :t)"),
                {'sid': sid, 'sq': seq, 'r': role, 'c': content, 'cj': cards_json,
                 'h': _archive_msg_hash(role, content, cards_json), 't': now})
        s.commit()
    except Exception:
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_update_card_status(session_id, card_id, new_status, new_content=None) -> None:
    """采纳/编辑/忽略卡片后：把存档里对应卡片的 status（和编辑后的 content）同步更新。
    覆盖 messages_json 瘦身后已被裁掉的老消息里的卡片（否则刷新后老卡片又显示待采纳）。"""
    sid = str(session_id or '').strip()
    if not sid or not card_id:
        return
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        # cards_json LIKE 预过滤（卡片 id 唯一，命中才解析更新）
        rows = s.execute(
            _t(f"SELECT seq, role, content, cards_json FROM {_ARCHIVE_TABLE} "
               f"WHERE session_id = :sid AND cards_json LIKE :pat"),
            {'sid': sid, 'pat': f'%{card_id}%'}).fetchall()
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for r in rows:
            try:
                cards = json.loads(r[3] or '[]')
            except Exception:
                continue
            if not isinstance(cards, list):
                continue
            hit = False
            for c in cards:
                if isinstance(c, dict) and c.get('id') == card_id:
                    c['status'] = new_status
                    if new_content is not None:
                        c['content'] = new_content
                    hit = True
            if not hit:
                continue
            changed = True
            cards_json = json.dumps(cards, ensure_ascii=False)
            s.execute(
                _t(f"UPDATE {_ARCHIVE_TABLE} SET cards_json = :cj, msg_hash = :h, updated_at = :t "
                   f"WHERE session_id = :sid AND seq = :sq"),
                {'cj': cards_json, 'h': _archive_msg_hash(r[1] or 'user', r[2] or '', cards_json),
                 't': now, 'sid': sid, 'sq': int(r[0])})
        if changed:
            s.commit()
    except Exception:
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_delete_session(session_id) -> None:
    """删除会话时清理存档行（避免孤儿数据堆积）。"""
    sid = str(session_id or '').strip()
    if not sid:
        return
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        s.execute(_t(f"DELETE FROM {_ARCHIVE_TABLE} WHERE session_id = :sid"), {'sid': sid})
        s.commit()
    except Exception:
        try:
            if s is not None:
                s.rollback()
        except Exception:
            pass
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


def _archive_count_map(session_ids: list) -> dict:
    """批量统计各会话的存档消息数（会话列表 message_count 用；失败返回空 dict 走旧计数）。"""
    ids = [str(x).strip() for x in (session_ids or []) if str(x or '').strip()]
    if not ids:
        return {}
    eng = None
    s = None
    try:
        eng, _SM = _archive_ind_connect()
        s = _SM()
        from sqlalchemy import text as _t
        _archive_ensure_table(s)
        ph = ','.join(f':i{k}' for k in range(len(ids)))
        rows = s.execute(
            _t(f"SELECT session_id, COUNT(*) FROM {_ARCHIVE_TABLE} WHERE session_id IN ({ph}) GROUP BY session_id"),
            {f'i{k}': v for k, v in enumerate(ids)}).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
    except Exception:
        return {}
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass
        try:
            if eng is not None:
                eng.dispose()
        except Exception:
            pass


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
      0. 全量同步进存档表（先于瘦身：给消息打 _seq 并完整落 ai_session_msgs，
         瘦身只影响 messages_json/LLM 上下文，前端回显不受影响）
      1. 对 history 做瘦身（卡片/消息截断、深度限制、48KB 总上限）
      2. 设置 session.messages_json / updated_at 并 commit
      3. 命中 OperationalError（连接被掐断）时：
         - rollback → engine.dispose() 扔僵尸连接 → 重查 session → 再 commit 1 次
    """
    from sqlalchemy.exc import OperationalError as SAOperationalError
    from app import db as _db, app as _app

    # 全量存档（独立连接小包写入；_seq 注入 history 消息 dict，随下方 messages_json 一并落盘）
    _archive_sync(session, history)

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
