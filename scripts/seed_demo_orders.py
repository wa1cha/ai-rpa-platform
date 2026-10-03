#!/usr/bin/env python3
"""导入样例订单，让演示库里有 7 笔订单可看。

用法：
    <PYTHON_BIN> scripts/seed_demo_orders.py

数据源是 `database/seed/04_orders_sample.csv` —— 与《部署说明》§8 里
「演示前干净状态 = 7 单全 IMPORTED」用的是同一份文件。

## 走法与 `seed_admin.py` 那批种子的关系

管理员 / Worker 账号、黑名单都由既有脚本在 Python 侧种（bcrypt 哈希算不出 SQL）。
本脚本补上最后一块：**业务数据（订单）**。订单不直接 INSERT，而是走
`ImportService.import_file()` —— 与 `POST /orders/import` 是同一条代码路径，
所以导进来的批次记录、订单状态、AI 分析入队都与真实导入完全一致。

## 幂等

只处理「orders 表一条都没有」的情况；非空即跳过。理由与其它种子一致：
运营数据一旦被人动过，脚本再跑不该把它改回去。种子只负责「从零到有」。
"""

import asyncio
import sys
from pathlib import Path

# scripts/ 不在包路径里，直接把 backend/ 挂上去，脚本才能 import app.*
ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.mysql import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.import_service import ImportService  # noqa: E402

SAMPLE_CSV = ROOT_DIR / "database" / "seed" / "04_orders_sample.csv"


async def main() -> int:
    if not SAMPLE_CSV.is_file():
        print(f"错误：找不到样例订单 {SAMPLE_CSV}", file=sys.stderr)
        return 1

    content = SAMPLE_CSV.read_bytes()

    try:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(select(func.count()).select_from(Order))
            if existing:
                print(f"orders 已有 {existing} 条，跳过导入（种子只从零种一次）")
                return 0

            admin = await session.scalar(
                select(User).where(User.username == settings.admin_username.strip())
            )
            if admin is None:
                print(
                    "错误：找不到管理员账号，先跑 scripts/seed_admin.py。",
                    file=sys.stderr,
                )
                return 1

            result = await ImportService(session).import_file(
                filename=SAMPLE_CSV.name,
                content=content,
                uploaded_by=admin.id,
            )

            print(
                f"已导入样例订单：batch_id={result.batch_id} "
                f"成功 {result.success_rows}/{result.total_rows} 行 status={result.status}"
            )
            for err in result.errors:
                print(f"  - 第 {err.row} 行 {err.order_no or ''}：{err.reason}")

            return 0 if result.failed_rows == 0 else 1
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
