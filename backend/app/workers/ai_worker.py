"""AI 分析 Worker —— 出队、分析、建任务的**一轮**。

它是 `workers/main.py` 里的一个 `Job`，不是一个独立进程：AI 分析本来就慢，
没必要为它单开一个进程去跟僵尸回收抢生命周期（见 `workers/main.py` 开头）。

一轮的工作方式：**排空队列**。每次最多取 `AI_WORKER_CONCURRENCY` 个并发分析，
取满一批 `asyncio.gather` 跑完再取下一批，直到队列空。所以 `Job` 的 interval
只在「队列空、没事干」时才起作用 —— 有活的时候一轮就把活干完了。

每单**自开 session**（与 `ai_reconciler.reconcile_once` 同形）：并发分析时不能
共享会话，否则一个事务里交织着几个订单的写入。CAS 保证「两个 worker 抢同一单」
不会真的分析两次。
"""

import asyncio
import logging
from dataclasses import dataclass

from app.core.config import settings
from app.core.enums import OrderStatus
from app.database.mysql import AsyncSessionLocal
from app.database.redis import redis_client
from app.repositories.customer_blacklist_repository import CustomerBlacklistRepository
from app.services.ai_service import AiAnalysisService
from app.services.queue_service import QueueService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AnalyzeReport:
    """一轮分析的结果。`failed` 是「AI 调用失败但已兜底推进」的条数，
    不是「这单没处理」—— 它照样会生成任务。"""

    popped: int = 0
    analyzed: int = 0
    failed: int = 0
    skipped: int = 0
    tasks_created: int = 0


@dataclass(slots=True)
class _ItemResult:
    skipped: bool
    failed: bool
    tasks_created: int


def _build_client():
    """按配置建 LLM 客户端。**延迟 import** 让本模块在没有 AI 依赖时也能 import。"""
    from ai.client import OrderAnalysisClient, OrderAnalysisConfig

    return OrderAnalysisClient(
        OrderAnalysisConfig(
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            timeout=float(settings.ai_timeout_seconds),
        )
    )


async def _analyze_one(order_id: int, analyzer, task_queue: QueueService, blacklisted: set[str]):
    """分析一单，成功则**顺手把任务建出来**（让 L3 只捞真的漏网的）。

    自开 session：并发批里每单一事务，互不干扰。
    """
    async with AsyncSessionLocal() as session:
        outcome = await AiAnalysisService(session, analyzer).analyze_order(
            order_id, blacklisted_phones=blacklisted
        )
        if outcome.skipped:
            return _ItemResult(skipped=True, failed=False, tasks_created=0)

        # 分析后订单已到 ANALYZED。只对 ANALYZED 建任务，不会把 IMPORTED 的
        # 单也建出任务来（那会绕过分析）。
        report = await TaskService(session, task_queue).generate(
            order_ids=[order_id], statuses=[OrderStatus.ANALYZED]
        )
        return _ItemResult(
            skipped=False,
            failed=outcome.failed,
            tasks_created=len(report.task_ids),
        )


async def _analyze_one_safe(
    order_id: int, analyzer, task_queue: QueueService, blacklisted: set[str]
) -> _ItemResult | None:
    """单条异常**不带走整批**：记 traceback，返回 None。

    订单若已置成 ANALYZING 而这里炸了，它会停在 ANALYZING —— 由 L2 补偿
    （`ai_reconciler`）在超时后置回 IMPORTED 重排队。这正是那一条腿存在的意义。
    """
    try:
        return await _analyze_one(order_id, analyzer, task_queue, blacklisted)
    except Exception:
        logger.exception("订单 %d AI 分析出错，等待补偿重排", order_id)
        return None


async def analyze_once(*, analyzer=None) -> AnalyzeReport:
    """跑一轮分析。

    `analyzer` 是给测试注入假模型用的；生产路径传 None，按配置自建客户端
    （此时由本函数负责关闭它）。
    """
    report = AnalyzeReport()

    if analyzer is None and not settings.ai_api_key:
        # 没有 key 不是错误：本地没配 AI 时作业进程照样要活着（僵尸回收还在跑）。
        logger.warning("未配置 AI_API_KEY，本轮跳过 AI 分析")
        return report

    owns_analyzer = analyzer is None
    client = analyzer or _build_client()
    ai_queue = QueueService(redis_client, key=settings.ai_queue_key)
    task_queue = QueueService(redis_client)

    try:
        # 黑名单一轮只查一次库，整批分析复用同一个集合。
        async with AsyncSessionLocal() as session:
            blacklisted = await CustomerBlacklistRepository(session).list_active_phones()

        while True:
            batch: list[int] = []
            while len(batch) < settings.ai_worker_concurrency:
                order_id = await ai_queue.pop_one()
                if order_id is None:
                    break
                batch.append(order_id)
            if not batch:
                break

            report.popped += len(batch)
            results = await asyncio.gather(
                *(
                    _analyze_one_safe(oid, client, task_queue, blacklisted)
                    for oid in batch
                )
            )
            for item in results:
                if item is None:
                    continue
                if item.skipped:
                    report.skipped += 1
                    continue
                report.analyzed += 1
                if item.failed:
                    report.failed += 1
                report.tasks_created += item.tasks_created
    finally:
        if owns_analyzer and hasattr(client, "aclose"):
            await client.aclose()

    if report.popped:
        logger.info(
            "AI 分析一轮：出队 %d，完成 %d（其中失败兜底 %d），跳过 %d，建任务 %d",
            report.popped,
            report.analyzed,
            report.failed,
            report.skipped,
            report.tasks_created,
        )
    return report
