#!/usr/bin/env python3
"""数据库通用导出工具（Neon → Aiven 迁移的第一步）。

用途：
  11 月 1 日 Neon 月度算力额度重置后，把 Neon 全库导出为一个 JSON.gz 备份文件；
  该文件既是迁移数据源，也是"账号数据不能删除"的离线备份。

用法：
  python3 scripts/db_export.py --url "$NEON_DATABASE_URL" --out neon_export.json.gz
  python3 scripts/db_export.py --url "$AIVEN_DATABASE_URL" --out aiven_backup.json.gz

特性：
  - 反射实际表结构（不依赖 models.py），源库与目标库 schema 有差异也能导
  - 全表导出（含 users/books/chapters/book_bible/ai_config 等 20 张业务表）
  - datetime/date/Decimal/bytes 全类型无损序列化，导入端原样还原
  - 只读操作，绝不写源库
"""
import argparse
import base64
import gzip
import json
import sys
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import MetaData, Table, create_engine, inspect, select


def normalize_url(url: str) -> str:
    """兼容 Render/Heroku 风格的 postgres:// 前缀（SQLAlchemy 2.x 需要 postgresql://）。"""
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


def make_engine(url: str):
    """建引擎。PostgreSQL 时加 IPv4 hostaddr 兜底（规避 IPv6 不可达，与 app.py 同策略）。"""
    url = normalize_url(url)
    connect_args = {}
    if url.startswith(('postgresql://', 'postgresql+psycopg2://')):
        try:
            from sqlalchemy.engine import make_url
            import socket
            host = make_url(url).host
            if host:
                for _fam, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
                        host, None, socket.AF_INET, socket.SOCK_STREAM):
                    connect_args['hostaddr'] = sockaddr[0]
                    break
        except Exception:
            pass  # 解析失败就走原始 DNS，不影响导出
    return create_engine(url, connect_args=connect_args)


def json_val(v):
    """任意列值 → JSON 安全值（导入端按列类型还原）。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, memoryview):
        v = bytes(v)
    if isinstance(v, (bytes, bytearray)):
        return {'__bytes__': base64.b64encode(bytes(v)).decode('ascii')}
    return str(v)


def export_db(url: str, out_path: str) -> dict:
    """把 url 指向的库全表导出到 out_path（JSON.gz）。返回 {表名: 行数}。"""
    engine = make_engine(url)
    insp = inspect(engine)
    data = {}
    counts = {}
    with engine.connect() as conn:
        for tname in sorted(insp.get_table_names()):
            meta = MetaData()
            table = Table(tname, meta, autoload_with=engine)
            rows = [dict(r) for r in conn.execute(select(table)).mappings().all()]
            data[tname] = [{k: json_val(v) for k, v in row.items()} for row in rows]
            counts[tname] = len(rows)
    payload = {
        'meta': {
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'tables': counts,
        },
        'data': data,
    }
    with gzip.open(out_path, 'wt', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    engine.dispose()
    return counts


def main():
    ap = argparse.ArgumentParser(description='导出数据库全部表到 JSON.gz 备份')
    ap.add_argument('--url', required=True, help='源数据库连接串（如 Neon 的 DATABASE_URL）')
    ap.add_argument('--out', required=True, help='输出文件路径（建议 *.json.gz）')
    args = ap.parse_args()

    counts = export_db(args.url, args.out)
    total = sum(counts.values())
    print(f'✅ 导出完成 → {args.out}')
    for t, n in counts.items():
        if n:
            print(f'   {t}: {n} 行')
    print(f'   合计 {total} 行 / {len(counts)} 张表')
    return 0


if __name__ == '__main__':
    sys.exit(main())
