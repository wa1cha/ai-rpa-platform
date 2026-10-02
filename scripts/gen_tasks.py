#!/usr/bin/env python3
"""给订单生成 RPA 任务并推入 Redis 队列。

用法：
    python scripts/gen_tasks.py                 # 给所有还没有任务的订单建任务
    python scripts/gen_tasks.py --dry-run       # 只报数，不写库不入队
    python scripts/gen_tasks.py --order-id 3 --order-id 5
    python scripts/gen_tasks.py --force-review  # 全部转人工审核（演示审核通路）
    python scripts/gen_tasks.py --reconcile     # 只照 MySQL 重建 Redis 队列

为什么不挂在导入流程里：导入是「把文件变成订单」，生成任务是「把订单变成
待执行的活」。两件事的触发时机、失败重试、幂等要求都不一样 —— 混在一起
以后想「重跑一次任务生成」就得连订单一起重导。Phase 5 接上 AI 之后，
真正的触发点会变成「分析完成」，那时这个脚本仍然是唯一入口，只是换个调用方。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# scripts/ 不在包路径里，直接把 backend/ 挂上去，脚本才能 import app.*
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.database.mysql import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.database.redis import close_redis, redis_client  # noqa: E402
from app.repositories.task_repository import TaskRepository  # noqa: E402
from app.services.queue_service import QueueService  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成任务并推入队列",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--order-id",
        type=int,
        action="append",
        dest="order_ids",
        help="只处理指定订单 id，可重复；不给则处理所有还没有任务的订单",
    )
    parser.add_argument(
        "--force-review",
        action="store_true",
        help="所有任务都进 WAITING_REVIEW（本地没有 AI 时用来看审核通路）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计会建多少张任务，不写库、不入队",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="不生成任务，只照 MySQL 的 QUEUED 状态全量重建 Redis 队列",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    queue = QueueService(redis_client)

    try:
        async with AsyncSessionLocal() as session:
            service = TaskService(session, queue)

            if args.reconcile:
                before = await queue.size()
                report = await service.reconcile_queue()
                print("队列对账完成")
                print(f"  Redis 原有      {before}")
                print(f"  Redis 重建后    {report.redis_after}")
                print(f"  MySQL 中 QUEUED {report.db_queued}")
                if before != report.db_queued:
                    print("  ^ 之前不一致，已按 MySQL 修正")
                return 0

            if args.dry_run:
                # 走的是同一个候选查询，只是不落库 —— 报的数就是真跑会建的数。
                candidates = await TaskRepository(session).list_orders_without_task(
                    order_ids=args.order_ids
                )
                print(f"[dry-run] 待建任务 {len(candidates)} 张，未写库、未入队")
                for order, analysis in candidates[:20]:
                    mark = "需审核" if args.force_review else "直接入队"
                    print(f"  order_id={order.id:<6} {order.order_no}  {mark}")
                if len(candidates) > 20:
                    print(f"  ... 其余 {len(candidates) - 20} 张省略")
                return 0

            report = await service.generate(
                order_ids=args.order_ids, force_review=args.force_review
            )

        print("任务生成完成")
        print(f"  候选订单        {report.scanned}")
        print(f"  新建任务        {report.created}")
        print(f"  已入队          {report.queued}")
        print(f"  待人工审核      {report.waiting_review}")
        print(f"  队列当前长度    {await queue.size()}")
        if report.task_ids:
            shown = report.task_ids[:10]
            tail = " ..." if len(report.task_ids) > 10 else ""
            print(f"  任务 id         {', '.join(map(str, shown))}{tail}")
        return 0
    finally:
        await dispose_engine()
        await close_redis()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
