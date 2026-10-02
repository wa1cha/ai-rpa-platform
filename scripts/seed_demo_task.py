#!/usr/bin/env python3
"""造一笔演示订单，并给它建一个 RPA 任务（落成 QUEUED 并进 Redis 队列）。

用法：
    <PYTHON_BIN> scripts/seed_demo_task.py
    <PYTHON_BIN> scripts/seed_demo_task.py --order-no MOCK20260928001
    <PYTHON_BIN> scripts/seed_demo_task.py --sku SKU-003      # 库存 0，用来验校验拒绝
    <PYTHON_BIN> scripts/seed_demo_task.py --sku SKU-001 --quantity 3

跑完打印 task_id 和 order_no，接着就能让 Worker 去领它。

## 为什么需要这个脚本

`TaskService.generate()` **没有 HTTP 接口** —— 它的正式调用方是 Phase 5 的 AI
（分析完成后触发，《需求规格》§8.4）。本地 dev 环境里既没有 AI、也没有导入
文件，想让 Worker 有活干就只剩「从 Python 侧造」这一条路。

## 和 `gen_tasks.py` 的分工

| 脚本 | 输入 | 用途 |
| --- | --- | --- |
| `gen_tasks.py` | **已有**订单 | 「任务生成」的正式入口，Phase 5 之后由分析完成事件调用 |
| `seed_demo_task.py` | **凭空造**一笔订单 | 只在本地演示/验证时用 |

两者底层调的是**同一个** `TaskService.generate()` —— 这里不是第二个入口，
只是给演示造一个起点。所以 `--order-no` 指向一个已有订单时，它不会重复建单，
会直接复用那笔订单。

## 幂等

`generate()` 只处理「**还没有任务**」的订单。所以同一个 `--order-no` 跑第二遍
不会多出一张任务 —— 脚本会把这情况明确报出来并返回非 0，而不是假装成功。
"""

import argparse
import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# scripts/ 不在包路径里，直接把 backend/ 挂上去，脚本才能 import app.*
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.database.mysql import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.database.redis import close_redis, redis_client  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services.queue_service import QueueService  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="造一笔演示订单并建 RPA 任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--order-no",
        default=None,
        help="来源单号（会写进 ERP 的 source_order_no）；不给就按时间自动生成",
    )
    parser.add_argument("--sku", default="SKU-001", help="库存里有的 SKU 才能录入成功")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--amount", default="199.00")
    parser.add_argument("--customer-name", default="演示用户")
    parser.add_argument("--phone", default="13800000000")
    parser.add_argument("--address", default="浙江省杭州市西湖区演示路 1 号")
    parser.add_argument("--product-name", default="演示商品")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    order_no = args.order_no or f"DEMO{datetime.now():%Y%m%d%H%M%S}"
    queue = QueueService(redis_client)

    try:
        async with AsyncSessionLocal() as session:
            order = await session.scalar(select(Order).where(Order.order_no == order_no))
            if order is None:
                order = Order(
                    order_no=order_no,
                    platform="mock",
                    ordered_at=datetime.now(),
                    customer_name=args.customer_name,
                    phone=args.phone,
                    address=args.address,
                    product_name=args.product_name,
                    sku=args.sku,
                    quantity=args.quantity,
                    amount=Decimal(args.amount),
                    status="IMPORTED",
                )
                session.add(order)
                await session.commit()
                await session.refresh(order)
                print(
                    f"已造订单  id={order.id}  order_no={order.order_no}  "
                    f"{order.sku} × {order.quantity}"
                )
            else:
                print(
                    f"订单已存在，直接复用  id={order.id}  order_no={order.order_no}  "
                    f"{order.sku} × {order.quantity}  status={order.status}"
                )

            report = await TaskService(session, queue).generate(order_ids=[order.id])

            if not report.task_ids:
                # 走到这里说明这笔订单已经有任务了 —— generate 只处理「还没有任务」
                # 的订单，是为了防止重复建任务（同一个订单同时排两次队）。
                existing = await session.scalar(
                    select(Task)
                    .where(Task.order_id == order.id)
                    .order_by(Task.id.desc())
                    .limit(1)
                )
                print(
                    f"没有新建任务：这笔订单已经有任务了"
                    f"（task_id={existing.id} status={existing.status} "
                    f"retry_count={existing.retry_count}）"
                )
                print("  换一个 --order-no 重跑；或者如果任务已经跑完，换个单号再造一笔。")
                return 1

            task_id = report.task_ids[0]
            print(
                f"已建任务  task_id={task_id}  order_no={order.order_no}  "
                f"（队列长度 {await queue.size()}）"
            )
            print()
            print("下一步：")
            print("  ./scripts/run_rpa_worker.sh --foreground --headed")
            return 0
    finally:
        await dispose_engine()
        await close_redis()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
