"""【AI调用账本 · 独立写入器】

为了解决以下两个问题，账本日志写入走独立 DB 连接，完全不依赖 Flask 请求事务：
  1. GeneratorExit / 用户中断 SSE 流：请求事务被回滚 → 日志跟着丢
  2. 调用方内部发生异常（例如 db.session 被置 aborted）：日志写入失败

使用方式（任意模块均可调用）：
    from ai_usage_recorder import record_ai_usage

    record_ai_usage(
        model='deepseek-chat',
        scene='chat_smart',
        task_type='creation',
        messages=[{'role':'user','content':'写个小说'}],
        response_content='第一章……',
        success=True,
        error_message='',
        duration_ms=1234,
        book_id='xxx',
        chapter_id=None,
        usage={'prompt_tokens': 12, 'completion_tokens': 345, 'total_tokens': 357},
    )
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


TABLE_NAME = 'ai_usage_logs'


# ---------------------------------------------------------------------------
# 独立连接 / 表结构（幂等 CREATE TABLE IF NOT EXISTS，与 app.py Model 保持一致）
# ---------------------------------------------------------------------------

def _ind_connect():
    """返回 (engine, SessionMaker) — 独立DB连接，与请求事务完全隔离。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    db_uri = (os.environ.get('DATABASE_URL')
              or os.environ.get('SQLALCHEMY_DATABASE_URI')
              or 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.db'))
    eng = create_engine(
        db_uri,
        pool_pre_ping=True,
        connect_args={'check_same_thread': False} if db_uri.startswith('sqlite') else {},
    )
    SM = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    return eng, SM


def _ensure_table_and_columns(sess) -> None:
    """确保 ai_usage_logs 表 + 所有列都存在（兼容老表做增量 ALTER）。

    列与 app.py AIUsageLog Model 完全对齐：
      id / book_id / chapter_id / scene / task_type / model
      prompt_chars / output_chars / input_tokens / output_tokens / total_tokens
      prompt_text / response_text
      success / error_message / duration_ms / created_at
    """
    from sqlalchemy import text as t

    # 1) 建表（幂等）
    sess.execute(t(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id VARCHAR(36) PRIMARY KEY,
            book_id VARCHAR(36),
            chapter_id VARCHAR(36),
            scene VARCHAR(64),
            task_type VARCHAR(32),
            model VARCHAR(100),
            prompt_chars INTEGER DEFAULT 0,
            output_chars INTEGER DEFAULT 0,
            success BOOLEAN,
            error_message TEXT,
            duration_ms INTEGER DEFAULT 0,
            created_at TIMESTAMP
        )
    """))
    sess.commit()

    # 2) 增量列：tokens / texts （v2 账本新增，2026-09-03 起）— 都是 IF NOT EXISTS
    add_cols = [
        ("input_tokens",   "INTEGER DEFAULT 0"),
        ("output_tokens",  "INTEGER DEFAULT 0"),
        ("total_tokens",   "INTEGER DEFAULT 0"),
        ("prompt_text",    "TEXT DEFAULT ''"),
        ("response_text",  "TEXT DEFAULT ''"),
    ]
    for col, defn in add_cols:
        try:
            sess.execute(t(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {col} {defn}"))
            sess.commit()
        except Exception:
            sess.rollback()


# ---------------------------------------------------------------------------
# 对外主函数：record_ai_usage
# ---------------------------------------------------------------------------

def record_ai_usage(
    *,
    model: str = '',
    scene: str = '',
    task_type: str = 'creation',
    messages: Optional[Iterable[dict]] = None,
    response_content: str = '',
    success: bool = True,
    error_message: str = '',
    duration_ms: int = 0,
    book_id: Optional[str] = None,
    chapter_id: Optional[str] = None,
    usage: Optional[dict] = None,
    prompt_text: Optional[str] = None,
    response_text: Optional[str] = None,
) -> str:
    """记录一次 AI 调用。失败静默，绝不打断主流程；返回 'ok' 或 'fail:...'。"""
    try:
        # 1) 字数计算
        prompt_chars = 0
        if prompt_text is None:
            if messages:
                prompt_chars = sum(len(str(m.get('content', ''))) for m in messages)
                # 同时构造 prompt_text（用户能看的完整输入摘要）：[role] content\n...
                try:
                    _parts = []
                    for m in messages or []:
                        if not isinstance(m, dict):
                            continue
                        r = str(m.get('role', '') or '')[:20]
                        c = str(m.get('content', '') or '')
                        if r:
                            _parts.append(f"[{r}]\n{c}")
                        else:
                            _parts.append(c)
                    prompt_text = "\n\n".join(_parts)[:8000]
                except Exception:
                    prompt_text = ''
            else:
                prompt_text = ''
        else:
            prompt_chars = len(prompt_text)

        if response_text is None:
            response_text = (response_content or '')[:8000]
        output_chars = len(response_content or '')

        # 2) tokens：优先从 usage dict 取（供应商官方返回最准）
        usage = usage if isinstance(usage, dict) else {}
        in_tok = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
        out_tok = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
        tot_tok = int(usage.get('total_tokens') or 0) or (in_tok + out_tok)

        # 3) 时长
        duration_ms = int(duration_ms or 0)

        # 4) 入库（独立连接）
        eng, SM = _ind_connect()
        try:
            s = SM()
            try:
                _ensure_table_and_columns(s)
                from sqlalchemy import text as t
                rid = str(uuid.uuid4())
                ts = datetime.now(timezone.utc)
                s.execute(
                    t(f"""
                        INSERT INTO {TABLE_NAME} (
                            id, book_id, chapter_id, scene, task_type, model,
                            prompt_chars, output_chars,
                            input_tokens, output_tokens, total_tokens,
                            prompt_text, response_text,
                            success, error_message, duration_ms, created_at
                        ) VALUES (
                            :id, :bid, :cid, :sc, :tt, :md,
                            :pc, :oc,
                            :it, :ot, :ttok,
                            :pt, :rt,
                            :ok, :em, :dm, :ts
                        )
                    """),
                    {
                        'id': rid,
                        'bid': book_id,
                        'cid': chapter_id,
                        'sc': (scene or '')[:64],
                        'tt': (task_type or 'creation')[:32],
                        'md': (model or '')[:100],
                        'pc': int(prompt_chars),
                        'oc': int(output_chars),
                        'it': int(in_tok),
                        'ot': int(out_tok),
                        'ttok': int(tot_tok),
                        'pt': prompt_text or '',
                        'rt': response_text or '',
                        'ok': bool(success),
                        'em': (error_message or '')[:2000],
                        'dm': int(duration_ms),
                        'ts': ts,
                    },
                )
                s.commit()
                return 'ok'
            finally:
                try: s.close()
                except Exception: pass
        finally:
            try: eng.dispose()
            except Exception: pass
    except Exception as e:
        try:
            return 'fail:' + type(e).__name__ + ':' + str(e)[:120]
        except Exception:
            return 'fail:unknown'
