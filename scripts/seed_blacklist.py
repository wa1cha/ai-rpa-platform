#!/usr/bin/env python3
"""种一条示例黑名单，让「客户在黑名单 → 直接标记异常」这条硬规则可被演示触达。

用法：
    .venv 里的 python scripts/seed_blacklist.py

为什么走 Python 而不是 `database/seed/*.sql`：那些 SQL 只被 `docker-compose.yml`
挂进 initdb.d，**本机 brew 的 MySQL 不加载它们**；本机的种子一直是 Python 脚本
（`seed_admin.py` / `seed_worker.py`），这里与它们保持同一口径。

为什么只种「表里一条都没有才种」而不是「按手机号 upsert」：与两处既有种子一致 ——
运营数据一旦被人改过（停用、改了原因），脚本再跑不该把它改回去。种子只负责
「从零到有」，之后的维护是运营的事。
"""

import asyncio
import sys
from pathlib import Path

# scripts/ 不在包路径里，直接把 backend/ 挂上去，脚本才能 import app.*
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select  # noqa: E402

from app.database.mysql import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.models.customer_blacklist import CustomerBlacklist  # noqa: E402

#: 取自 `database/seed/04_orders_sample.csv` 最后一行 —— 演示时那一单会命中这条规则。
SEED_ROWS: list[tuple[str, str, str]] = [
    ("13800007777", "吴敏", "示例黑名单：历史退款纠纷"),
]


async def main() -> int:
    try:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(
                select(func.count()).select_from(CustomerBlacklist)
            )
            if existing:
                print(f"customer_blacklist 已有 {existing} 条，跳过（种子只从零种一次）")
                return 0

            session.add_all(
                CustomerBlacklist(phone=phone, customer_name=name, reason=reason)
                for phone, name, reason in SEED_ROWS
            )
            await session.commit()
            print(f"已种入 {len(SEED_ROWS)} 条黑名单：")
            for phone, name, reason in SEED_ROWS:
                print(f"  - {phone}  {name}  （{reason}）")
            return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
