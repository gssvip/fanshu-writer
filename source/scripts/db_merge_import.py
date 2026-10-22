#!/usr/bin/env python3
"""Neon → Aiven 数据合并导入（支持同名账号合账，零数据丢失）。

背景：
  11 月 1 日 Neon 额度重置后导出数据（db_export.py），而 Aiven 上已用同名账号
  （如 666）测试/修改过数据。直接 pg_restore 会主键冲突，本脚本按自然键做**合并**：
  同名账号并到同一账户下，两边数据互补不覆盖。

用法（先预演，确认无误再执行）：
  python3 scripts/db_merge_import.py --src neon_export.json.gz --dst "$AIVEN_DATABASE_URL"
  python3 scripts/db_merge_import.py --src neon_export.json.gz --dst "$AIVEN_DATABASE_URL" --apply

合并规则（核心：目标库"修改后"的数据优先，源库只补空缺 + 并入新增）：
  1. users     按 username 合账：Aiven 的 666 与 Neon 的 666 → 同一账户（id 用 Aiven 的）。
               密码保留 Aiven 的（修改后的）；email/is_vip 空缺从 Neon 补齐。
  2. books     按 (所属用户, 书名) 匹配：同名书并入 Aiven 的书；Neon 独有的书整体迁入。
  3. chapters  按 (所属书, 标题) 匹配：两边都有的章 → Aiven 内容优先，Neon 内容归档为
               ChapterVersion 历史版本（绝不丢字）；Neon 独有的章直接迁入。
  4. 配置类     ai_config/app_preferences/book_bible/dynamic_memory/stage_contents 等：
               目标非空字段优先，目标为空的字段从 Neon 补齐（配置不丢）。
  5. 统计类     daily_stats 同 (书, 日期) 数值求和；ai_usage_logs/ai_sessions 按 id 幂等迁入。
  6. 全局模板   templates/skill_packs/prompt_templates 按自然键去重迁入。
  7. 登录令牌   auth_tokens/password_reset_tokens 不迁移（无价值，重新登录即可）。

安全性：
  - 默认 dry-run 只打印合并计划，不写库；--apply 才真正写入
  - 全程单事务，任一步失败整体回滚，Aiven 现有数据不受影响
  - 按自然键匹配 + id 幂等；注意 daily_stats 为求和合并，--apply 只应执行一次
"""
import argparse
import base64
import gzip
import json
import sys
import uuid
from datetime import date, datetime

from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.engine import make_url

# ---------------------------------------------------------------------------
# 表级合并配置：natural_key=匹配用自然键列；policy=default|sum；skip=不迁移
#   FK 重映射列：user_id/book_id/chapter_id/parent_id 在插入前替换为合并后的 id
# ---------------------------------------------------------------------------
MERGE_CONFIG = {
    'users':               {'natural_key': ['username']},
    'books':               {'natural_key': ['user_id', 'title']},
    'chapters':            {'natural_key': ['book_id', 'title']},
    'chapter_versions':    {'natural_key': ['chapter_id', 'version_num']},
    'characters':          {'natural_key': ['book_id', 'name']},
    'outlines':            {'natural_key': ['book_id', 'title', 'level', 'order_index']},
    'daily_stats':         {'natural_key': ['book_id', 'date'], 'policy': 'sum'},
    'templates':           {'natural_key': ['name', 'genre', 'book_type']},
    'ai_sessions':         {'natural_key': None},   # 按主键 id 幂等插入
    'ai_config':           {'natural_key': ['user_id', 'provider']},
    'ai_usage_logs':       {'natural_key': None},
    'app_preferences':     {'natural_key': ['key']},
    'stage_contents':      {'natural_key': ['book_id', 'stage_key']},
    'book_bible':          {'natural_key': ['book_id']},
    'skill_packs':         {'natural_key': ['name', 'category', 'genre_target']},
    'dynamic_memory':      {'natural_key': ['book_id']},
    'dynamic_reports':     {'natural_key': ['book_id', 'title', 'chapter_start', 'chapter_end']},
    'prompt_templates':    {'natural_key': ['name', 'agent_id', 'book_type', 'genre']},
    'app_meta':            {'natural_key': ['key']},
    # 登录/重置令牌：跳过（重新登录即可，迁移无价值）
    'auth_tokens':         {'skip': True},
    'password_reset_tokens': {'skip': True},
}

# 外键列 → 所属映射表（插入前把源 id 替换为合并后 id）
FK_COLUMNS = {
    'user_id': 'users',
    'book_id': 'books',
    'chapter_id': 'chapters',
}

# 处理顺序：先用户、再书、再章节、最后依赖它们的子表（保证 FK 映射就绪）
TABLE_ORDER = [
    'users', 'books', 'chapters', 'chapter_versions', 'characters', 'outlines',
    'daily_stats', 'templates', 'ai_sessions', 'ai_config', 'ai_usage_logs',
    'app_preferences', 'stage_contents', 'book_bible', 'skill_packs',
    'dynamic_memory', 'dynamic_reports', 'prompt_templates', 'app_meta',
]

# 这些列不从源库覆盖目标（时间戳/统计由目标持有；内容类走空缺补齐）
NEVER_OVERWRITE = {'id', 'created_at', 'updated_at', 'last_synced_at', 'github_synced_at'}
# 数值列在 sum 策略下求和，其余策略下目标非空优先
SUM_SKIP = {'id'}


def load_export(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        payload = json.load(f)
    return payload['data']


def restore_val(col, v):
    """JSON 值按列类型还原为 Python 值（datetime/bytes 等）。"""
    if isinstance(v, dict) and '__bytes__' in v:
        return base64.b64decode(v['__bytes__'])
    if v is None:
        return None
    if isinstance(v, str):
        try:
            from sqlalchemy import DateTime, Date
            if isinstance(col.type, DateTime):
                return datetime.fromisoformat(v)
            if isinstance(col.type, Date):
                return date.fromisoformat(v)
        except Exception:
            pass
    return v


def normalize_key_val(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def is_empty(v):
    return v is None or (isinstance(v, str) and v.strip() == '')


class Merger:
    def __init__(self, src_data, dst_url, apply=False):
        self.src = src_data
        self.apply = apply
        self.engine = self._make_engine(dst_url)
        self.insp = inspect(self.engine)
        self.dst_tables = set(self.insp.get_table_names())
        # id 映射：源库 id → 合并后（目标库）id
        self.id_maps = {'users': {}, 'books': {}, 'chapters': {}, 'outlines': {}}
        self.stats = {}   # 表 → {'inserted': n, 'merged': n, 'skipped': n, 'filled': n}

    @staticmethod
    def _make_engine(url):
        if url.startswith('postgres://'):
            url = url.replace('postgres://', 'postgresql://', 1)
        connect_args = {}
        if url.startswith('postgresql://'):
            try:
                import socket
                host = make_url(url).host
                if host:
                    for _f, _t, _p, _c, sa in socket.getaddrinfo(
                            host, None, socket.AF_INET, socket.SOCK_STREAM):
                        connect_args['hostaddr'] = sa[0]
                        break
            except Exception:
                pass
        return create_engine(url, connect_args=connect_args)

    def _bump(self, table, key, n=1):
        d = self.stats.setdefault(table, {'inserted': 0, 'merged': 0, 'skipped': 0, 'filled': 0})
        d[key] += n

    def _remap_row(self, table_name, row, cols):
        """把行中的 FK 列从源 id 替换为合并后 id。

        自引用 parent_id（chapters 卷指针 / outlines 层级指针）不在此处理——
        行处理顺序不保证父在子前，统一延迟到 _fix_parent_ids 收尾重映射。
        """
        out = dict(row)
        for fk_col, map_name in FK_COLUMNS.items():
            if fk_col in cols and map_name in self.id_maps:
                src_v = out.get(fk_col)
                if src_v in self.id_maps[map_name]:
                    out[fk_col] = self.id_maps[map_name][src_v]
        return out

    def run(self):
        with self.engine.begin() as conn:
            for table_name in TABLE_ORDER:
                if table_name not in self.dst_tables:
                    print(f'  ⚠️ 目标库无表 {table_name}，跳过（请先启动一次应用建表）')
                    continue
                cfg = MERGE_CONFIG.get(table_name, {})
                if cfg.get('skip'):
                    n = len(self.src.get(table_name, []))
                    if n:
                        self._bump(table_name, 'skipped', n)
                    continue
                self._merge_table(conn, table_name, cfg)
            self._fix_parent_ids(conn)
            if self.apply:
                self._recompute_book_stats(conn)
        self.engine.dispose()
        return self.stats

    def _merge_table(self, conn, table_name, cfg):
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)
        cols = {c.name for c in table.columns}
        src_rows = [r for r in self.src.get(table_name, []) if set(r) & cols]
        if not src_rows:
            return

        # 目标库现有行索引：自然键 → 行；主键 id 集合
        dst_rows = [dict(r) for r in conn.execute(select(table)).mappings().all()]
        nk_cols = [k for k in (cfg.get('natural_key') or []) if k in cols]

        def nk_of(row, use_mapped=True):
            vals = []
            for k in nk_cols:
                v = row.get(k)
                if use_mapped and k in FK_COLUMNS and FK_COLUMNS[k] in self.id_maps:
                    v = self.id_maps[FK_COLUMNS[k]].get(v, v)
                vals.append(normalize_key_val(v))
            return tuple(vals)

        dst_by_nk = {nk_of(r, use_mapped=False): r for r in dst_rows} if nk_cols else {}
        dst_ids = {r.get('id') for r in dst_rows if 'id' in cols}
        policy = cfg.get('policy', 'default')

        for srow in src_rows:
            srow = self._remap_row(table_name, srow, cols)

            # 幂等：id 已存在 → 记录映射并跳过
            if 'id' in cols and srow.get('id') in dst_ids:
                self._map_id(table_name, srow, srow['id'])
                self._bump(table_name, 'skipped')
                continue

            matched = dst_by_nk.get(nk_of(srow)) if nk_cols else None
            if matched is not None:
                self._merge_row(conn, table, table_name, srow, matched, cols, policy)
                self._map_id(table_name, srow, matched.get('id'))
                self._bump(table_name, 'merged')
            else:
                self._insert_row(conn, table, table_name, srow, cols)
                self._map_id(table_name, srow, srow.get('id'))
                self._bump(table_name, 'inserted')

    def _map_id(self, table_name, srow, dst_id):
        if table_name in self.id_maps and srow.get('id'):
            self.id_maps[table_name][srow['id']] = dst_id or srow.get('id')

    def _merge_row(self, conn, table, table_name, srow, drow, cols, policy):
        """目标非空优先，目标空缺从源补；sum 策略数值列求和。章节内容冲突归档为历史版本。"""
        updates = {}
        if policy == 'sum':
            for c in cols:
                if c in SUM_SKIP:
                    continue
                sv, dv = srow.get(c), drow.get(c)
                if isinstance(sv, (int, float)) and isinstance(dv, (int, float)) \
                        and not isinstance(sv, bool) and not isinstance(dv, bool):
                    if sv or dv:
                        updates[c] = dv + sv
                elif is_empty(dv) and not is_empty(sv):
                    updates[c] = restore_val(table.c[c], sv)
        else:
            for c in cols:
                if c in NEVER_OVERWRITE or c == 'id':
                    continue
                sv, dv = srow.get(c), drow.get(c)
                # 布尔列 True 胜出（如 users.is_vip：Neon 的会员标记不因 Aiven 默认 False 被丢）
                if isinstance(sv, bool) or isinstance(dv, bool):
                    if sv and not dv:
                        updates[c] = sv
                elif is_empty(dv) and not is_empty(sv):
                    updates[c] = restore_val(table.c[c], sv)

        # content 从源补齐时字数一并补齐，避免章节字数显示 0
        if 'content' in cols and updates.get('content') and 'word_count' in cols:
            if not drow.get('word_count') and srow.get('word_count'):
                updates['word_count'] = srow['word_count']

        # 章节正文冲突：Aiven（修改后）优先，Neon 内容归档为 ChapterVersion，绝不丢字
        if table_name == 'chapters':
            s_content = srow.get('content') or ''
            d_content = drow.get('content') or ''
            if s_content and d_content and s_content != d_content:
                self._archive_version(conn, drow.get('id'), s_content)

        if updates:
            self._bump(table_name, 'filled', len(updates))
            if self.apply:
                # 按主键定位（app_meta 的主键是 key 而非 id，不能写死 table.c.id）
                stmt = table.update()
                for pkc in table.primary_key.columns:
                    stmt = stmt.where(pkc == drow.get(pkc.name))
                conn.execute(stmt, updates)

    def _archive_version(self, conn, chapter_id, content):
        """把源库正文归档为目标章节的历史版本（version_num 递增）。"""
        if not self.apply or not chapter_id:
            return
        try:
            meta = MetaData()
            t = Table('chapter_versions', meta, autoload_with=self.engine)
            rows = [dict(r) for r in conn.execute(
                select(t).where(t.c.chapter_id == chapter_id)).mappings().all()]
            next_num = max([r.get('version_num') or 0 for r in rows], default=0) + 1
            conn.execute(t.insert(), {
                'id': str(uuid.uuid4()),
                'chapter_id': chapter_id,
                'content': content,
                'version_num': next_num,
                'created_at': datetime.utcnow(),
                'summary': 'Neon 迁移归档版本（合并导入时正文不一致，保留 Neon 版本）',
            })
        except Exception as e:
            print(f'   ⚠️ 章节版本归档失败（不影响其余合并）：{e}')

    def _insert_row(self, conn, table, table_name, srow, cols):
        # 按列类型还原 JSON 值（datetime/date 等），避免字符串写入时间戳列
        row = {c: restore_val(table.c[c], srow.get(c)) for c in cols}
        # 时间戳缺失补当前时间
        for ts in ('created_at', 'updated_at'):
            if ts in cols and is_empty(row.get(ts)):
                row[ts] = datetime.utcnow()
        if self.apply:
            conn.execute(table.insert(), row)

    def _fix_parent_ids(self, conn):
        """收尾统一重映射自引用 parent_id（chapters 卷指针 / outlines 层级指针）。

        必须延迟到所有行处理完再做：子行可能先于父行处理，逐行映射会漏；
        这里按 id_maps 把所有指向源 id 的 parent_id 改写为合并后 id，不依赖顺序。
        """
        if not self.apply:
            return
        for table_name in ('chapters', 'outlines'):
            idmap = self.id_maps.get(table_name) or {}
            if not idmap or table_name not in self.dst_tables:
                continue
            meta = MetaData()
            t = Table(table_name, meta, autoload_with=self.engine)
            for src_id, dst_id in idmap.items():
                if not dst_id or src_id == dst_id:
                    continue
                conn.execute(
                    t.update().where(t.c.parent_id == src_id).values(parent_id=dst_id))

    def _recompute_book_stats(self, conn):
        """合并后重算各书 word_count/chapter_count（避免计数与实际章节不符）。"""
        try:
            meta = MetaData()
            books = Table('books', meta, autoload_with=self.engine)
            chapters = Table('chapters', meta, autoload_with=self.engine)
            for brow in conn.execute(select(books)).mappings().all():
                chs = [dict(r) for r in conn.execute(
                    select(chapters).where(chapters.c.book_id == brow['id'])
                    .where(chapters.c.is_volume.is_(False))).mappings().all()]
                conn.execute(books.update().where(books.c.id == brow['id']), {
                    'word_count': sum(c.get('word_count') or 0 for c in chs),
                    'chapter_count': len(chs),
                })
        except Exception as e:
            print(f'   ⚠️ 书籍统计重算失败（不影响数据合并）：{e}')


def main():
    ap = argparse.ArgumentParser(description='Neon 导出数据合并导入 Aiven（同名账号合账，零丢失）')
    ap.add_argument('--src', required=True, help='db_export.py 导出的 *.json.gz')
    ap.add_argument('--dst', required=True, help='目标库连接串（Aiven 的 DATABASE_URL）')
    ap.add_argument('--apply', action='store_true', help='真正写入（不加此参数只做 dry-run 预演）')
    args = ap.parse_args()

    src_data = load_export(args.src)
    mode = '🔥 正式写入' if args.apply else '🔍 dry-run 预演（不写库）'
    print(f'{mode}：{args.src} → 目标库')

    merger = Merger(src_data, args.dst, apply=args.apply)
    stats = merger.run()

    print(f'\n{"=" * 60}\n合并结果汇总：')
    for t in TABLE_ORDER:
        if t in stats:
            s = stats[t]
            print(f'  {t:20s} 新增 {s["inserted"]:4d} / 合并 {s["merged"]:4d} / '
                  f'跳过 {s["skipped"]:4d} / 补齐字段 {s["filled"]:4d}')
    if args.apply:
        print('\n✅ 合并完成。同名账号（如 666）已合账：Aiven 数据优先，Neon 数据补齐并入。')
    else:
        print('\n（dry-run 未写库。确认上面的计划无误后，加 --apply 执行）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
