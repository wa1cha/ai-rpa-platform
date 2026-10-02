"""RPA Worker 专用业务逻辑 —— 见《API接口设计》§10。

Worker 通过 HTTP 只说三件事：**领活、我还活着、干完了（成或败）**。
这个文件把这三句话翻译成 MySQL 的状态变化 —— Redis 队列只在 `claim` 里被
碰一次，其余全部以 MySQL 为准（Redis 是加速层，不是事实来源）。

贯穿全篇的不变式：

    task_executions 里 status='RUNNING' 的那条
        ⟺ tasks 里 status='RUNNING' 的那个任务
        ⟺ 它的 attempt == task.retry_count + 1

claim / 回传 / 僵尸回收三处都按这条对齐。任何一处写歪，`attempt` 就会错位，
而错位不会当场报错 —— 它表现为「执行记录少一条」或者「截图挂到了别人身上」。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import ExecutionStatus, OrderStatus, TaskStatus
from app.core.exceptions import InvalidStateError, NotFoundError, ParamError
from app.models.task import Task
from app.repositories.order_repository import OrderRepository
from app.repositories.task_execution_repository import TaskExecutionRepository
from app.repositories.task_repository import TaskRepository
from app.schemas.rpa import (
    ClaimData,
    ClaimedTask,
    ClaimInstruction,
    HeartbeatData,
    ResultData,
    ResultRequest,
)
from app.services.notification_service import NotificationService
from app.services.queue_service import QueueService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

#: 允许上传的截图格式。只认这两种：ERP 页面截图就这俩，别的格式
#: 要么是传错了文件，要么是想往磁盘上塞点别的东西。
_ALLOWED_SCREENSHOT_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

#: 僵尸回收时写进 `task_executions.error_code` 的分类值。
#: 和 Worker 自报的 `ERP_*` 要能一眼区分开 —— 「ERP 拒绝了我」和
#: 「Worker 根本没回话」是两种完全不同的故障，混在同一个 code 里就没法统计。
_WORKER_LOST = "WORKER_LOST"

#: 回收时给 `tasks.last_error` 的文案，和 `error_code` 一起构成人工排查的起点。
_WORKER_LOST_MESSAGE = "Worker 心跳超时失联，任务已被回收"


@dataclass(slots=True)
class ReapReport:
    """一次僵尸扫描的结果。`scanned` 通常为 0 —— 非零就该有人看一眼。"""

    scanned: int = 0
    requeued: int = 0
    failed: int = 0
    task_ids: list[int] = field(default_factory=list)


class RpaService:
    def __init__(self, session: AsyncSession, queue: QueueService) -> None:
        self.session = session
        self.queue = queue
        self.tasks = TaskRepository(session)
        self.orders = OrderRepository(session)
        self.executions = TaskExecutionRepository(session)
        self.notifications = NotificationService(session)

    # ==========================================================
    # §10.1 领取任务
    # ==========================================================

    async def claim(self, worker_name: str, wait_seconds: int) -> ClaimData | None:
        """出队一个任务，置 RUNNING，开一条执行记录，返回完整上下文。

        出队委托给 `TaskService.pop_next_runnable(block_seconds=...)` ——
        那里已经做了「弹出后回查 MySQL，确认还是 QUEUED，否则丢弃」。
        `wait_seconds > 0` 时它会用 Redis 的阻塞版出队，这就是长轮询：
        Worker 不必每 100ms 问一次「有活吗」，而是挂起等这个信号。

        **已知的顺序风险，写在这里以免以后有人以为它不存在**：
        出队（Redis）发生在写库（MySQL）之前。若进程恰好在两者之间挂掉，
        这个任务就从队列里消失了，而库里还是 QUEUED —— 它会停摆到下一次
        `reconcile_queue()`。这样做是因为 claim 无法「先写库再出队」：
        不先弹出来，就不知道要认领哪一个。
        缓解手段是既有的对账脚本（重启后跑一次即可），而不是在这里加事务 ——
        Redis 和 MySQL 本来就不在同一个事务里，假装能原子才是真的危险。
        """
        task_id = await TaskService(self.session, self.queue).pop_next_runnable(
            block_seconds=wait_seconds
        )
        if task_id is None:
            return None

        # pop_next_runnable 回查过 status == 'QUEUED'，所以任务一定存在。
        task = await self.tasks.get(task_id)
        order = await self.orders.get(task.order_id)

        now = datetime.now()
        attempt = task.retry_count + 1

        task.status = TaskStatus.RUNNING.value
        task.claimed_by = worker_name
        task.heartbeat_at = now
        # 上一轮的失败原因不该挂在本轮头上，列表页会误导人。
        task.last_error = None

        self.session.add(
            self._open_execution(task.id, attempt, worker_name, now)
        )
        await self.session.commit()

        logger.info(
            "Worker %s 领取任务 %d（第 %d 次尝试，优先级 %s）",
            worker_name,
            task.id,
            attempt,
            task.priority,
        )
        return ClaimData(
            task=ClaimedTask(
                id=task.id,
                priority=task.priority,
                attempt=attempt,
                need_review=bool(task.need_review),
            ),
            order=TaskService.to_order_brief(order),
            instruction=ClaimInstruction(erp_url=settings.mock_erp_base_url),
        )

    # ==========================================================
    # §10.2 心跳
    # ==========================================================

    async def heartbeat(self, task_id: int, worker_name: str) -> HeartbeatData:
        """刷新心跳；若这活已不归本 Worker，回 `cancel=true` 让它停手。

        比的不只是状态 —— 还要比 `claimed_by`。任务被僵尸回收之后**可能已经有
        另一个 Worker 领走**，那种情况下这个 Worker 更不能继续操作同一张单，
        否则 ERP 里会出现两笔一模一样的订单（`source_order_no` 唯一索引会挡住
        第二笔，但第一笔已经脏了）。

        **不报 404/409 而是回 200 + cancel**：心跳是个高频、无人值守的调用，
        让它因为「任务没了」而抛错，Worker 端的错误处理反而更容易写错 ——
        抛错的自然反应是重试，而这里要的是立刻停手。
        """
        task = await self.tasks.get(task_id)
        if task is None:
            return HeartbeatData(task_id=task_id, status="MISSING", cancel=True)

        if task.status != TaskStatus.RUNNING.value or task.claimed_by != worker_name:
            logger.warning(
                "任务 %d 已不归 %s（status=%s claimed_by=%s），通知其停止",
                task_id,
                worker_name,
                task.status,
                task.claimed_by,
            )
            return HeartbeatData(task_id=task_id, status=task.status, cancel=True)

        task.heartbeat_at = datetime.now()
        await self.session.commit()
        return HeartbeatData(task_id=task_id, status=task.status, cancel=False)

    # ==========================================================
    # §10.3 回传结果
    # ==========================================================

    async def submit_result(self, task_id: int, payload: ResultRequest) -> ResultData:
        """成功 → SUCCESS；失败 → 有额度就回队列，没额度就 FAILED。

        先写库、后动队列（与 Phase 3 同一条原则）：重试入队必须发生在
        `status=QUEUED` 落库之后，否则队列里会出现一条指向「还不是 QUEUED
        的任务」的记录，那比反向的失败更难修。
        """
        task = await self._get_running_task(task_id, payload.worker_name)
        execution = await self.executions.get_by_attempt(task.id, task.retry_count + 1)
        if execution is None:
            raise NotFoundError("找不到本次执行记录，任务状态与执行记录不一致")

        now = datetime.now()
        execution.finished_at = now
        execution.duration_ms = payload.duration_ms

        if payload.success:
            return await self._on_success(task, execution, payload, now)
        return await self._on_failure(task, execution, payload, now)

    async def _on_success(self, task, execution, payload, now) -> ResultData:
        execution.status = ExecutionStatus.SUCCESS.value
        execution.erp_order_no = payload.erp_order_no

        task.status = TaskStatus.SUCCESS.value
        task.finished_at = now
        task.last_error = None
        # 清掉领取痕迹：完成的任务不该在后台看起来「还被某个 Worker 领者」。
        # 「谁执行的」这件事 task_executions.worker_name 记得更准。
        task.claimed_by = None
        task.heartbeat_at = None

        await self._set_order_status(task.order_id, OrderStatus.COMPLETED)
        await self.session.commit()

        logger.info(
            "任务 %d 执行成功，ERP 单号 %s（耗时 %s ms）",
            task.id,
            payload.erp_order_no,
            payload.duration_ms,
        )
        return ResultData(
            task_id=task.id, status=TaskStatus.SUCCESS.value, retry_scheduled=False
        )

    async def _on_failure(self, task, execution, payload, now) -> ResultData:
        execution.status = ExecutionStatus.FAILED.value
        execution.error_code = payload.error_code
        execution.error_message = payload.error_message

        task.last_error = payload.error_message
        task.claimed_by = None
        task.heartbeat_at = None

        if task.retry_count < task.max_retry:
            task.retry_count += 1
            task.status = TaskStatus.QUEUED.value
            task.queued_at = now
            retry_count = task.retry_count
            priority = task.priority

            await self.session.commit()
            await self.queue.enqueue(task.id, priority, now)

            logger.warning(
                "任务 %d 第 %d 次执行失败（%s），已重新入队（%d/%d）",
                task.id,
                retry_count,
                payload.error_code or "未知错误",
                retry_count,
                task.max_retry,
            )
            return ResultData(
                task_id=task.id,
                status=TaskStatus.QUEUED.value,
                retry_scheduled=True,
                retry_count=retry_count,
            )

        # 额度用尽：终态。通知和状态在同一个事务里提交 ——
        # 不允许出现「通知发了但任务还是 RUNNING」这种中间态。
        task.status = TaskStatus.FAILED.value
        task.finished_at = now
        await self._set_order_status(task.order_id, OrderStatus.FAILED)
        self.notifications.notify_task_failed(task, payload.error_message)
        await self.session.commit()

        logger.error(
            "任务 %d 重试超限（%d/%d），判定 FAILED 并转人工",
            task.id,
            task.retry_count,
            task.max_retry,
        )
        return ResultData(
            task_id=task.id,
            status=TaskStatus.FAILED.value,
            retry_scheduled=False,
            retry_count=task.retry_count,
        )

    # ==========================================================
    # 僵尸任务回收 —— 见《数据库设计》§6.1
    # ==========================================================

    async def reap_stale(self, cutoff: datetime) -> ReapReport:
        """把心跳早于 `cutoff` 的 RUNNING 任务收回来。

        由独立进程里的定时任务调用（见 `workers/zombie_reaper.py`），不挂在
        FastAPI 的 lifespan 上：回收是后台作业，不该和「能不能处理 HTTP 请求」
        绑在一起 —— Web 进程多开几个实例时会同时跑多份回收，逻辑虽然幂等，
        但没必要让每个副本都干这活。

        **与 `_on_failure` 的失败路径同构**（`retry_count + 1` 再决定入队还是终态），
        区别只有两点：`error_code` 固定为 `WORKER_LOST`，以及没有 `duration_ms`
        —— Worker 失联了，没人能告诉我们它跑了多久。

        计入 `attempt` 的那条执行记录必然是 `retry_count + 1`：这正是本模块开头
        那条不变式的第三处落点。若查不到它（理论上不该发生），不抛错 ——
        回收的目的是**让任务动起来**，为一条对不上的执行记录把整轮扫描打断，
        等于让一个卡死的任务继续卡着。
        """
        stale = await self.tasks.list_stale_running(cutoff)
        report = ReapReport(scanned=len(stale))
        if not stale:
            return report

        now = datetime.now()
        to_requeue: list[tuple[int, str, datetime]] = []

        for task in stale:
            attempt = task.retry_count + 1
            execution = await self.executions.get_by_attempt(task.id, attempt)
            if execution is not None:
                execution.status = ExecutionStatus.FAILED.value
                execution.error_code = _WORKER_LOST
                execution.error_message = _WORKER_LOST_MESSAGE
                execution.finished_at = now

            task.claimed_by = None
            task.heartbeat_at = None
            task.last_error = _WORKER_LOST_MESSAGE
            report.task_ids.append(task.id)

            if task.retry_count < task.max_retry:
                task.retry_count += 1
                task.status = TaskStatus.QUEUED.value
                task.queued_at = now
                to_requeue.append((task.id, task.priority, now))
                report.requeued += 1
            else:
                task.status = TaskStatus.FAILED.value
                task.finished_at = now
                await self._set_order_status(task.order_id, OrderStatus.FAILED)
                self.notifications.notify_task_failed(task, _WORKER_LOST_MESSAGE)
                report.failed += 1

            logger.warning(
                "回收僵尸任务 %d（第 %d 次尝试，心跳早于 %s）",
                task.id,
                attempt,
                cutoff.isoformat(timespec="seconds"),
            )

        # 先写库、后动队列 —— 与 claim / 回传同一条原则。
        await self.session.commit()
        for task_id, priority, queued_at in to_requeue:
            await self.queue.enqueue(task_id, priority, queued_at)

        return report

    # ==========================================================
    # §10.4 失败截图
    # ==========================================================

    async def save_screenshot(
        self, task_id: int, attempt: int, filename: str, content: bytes
    ) -> str:
        """把截图落到 webroot 之外，返回**取图接口**的路径。

        数据库里只存文件名（`4001.png`），不存绝对路径 —— 否则换部署目录
        （本机 / 容器里挂载点不同）时，库里全是失效路径。

        Worker 的调用顺序是 **screenshot → result**（见 §10.4），所以这时
        执行记录一定还在，状态是 RUNNING。
        """
        suffix = Path(filename).suffix.lower()
        media_type = _ALLOWED_SCREENSHOT_SUFFIXES.get(suffix)
        if media_type is None:
            raise ParamError(
                f"不支持的截图格式：{suffix or '（无扩展名）'}。"
                f"只允许 {'、'.join(sorted(_ALLOWED_SCREENSHOT_SUFFIXES))}"
            )
        if not content:
            raise ParamError("截图内容为空")
        if len(content) > settings.screenshot_max_bytes:
            raise ParamError(
                f"截图过大：{len(content)} 字节，上限 {settings.screenshot_max_bytes}"
            )

        task = await self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("任务不存在")
        execution = await self.executions.get_by_attempt(task_id, attempt)
        if execution is None:
            raise NotFoundError(f"任务 {task_id} 没有第 {attempt} 次执行记录")

        stored_name = f"{execution.id}{suffix}"
        directory: Path = settings.screenshot_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / stored_name).write_bytes(content)

        execution.screenshot_path = stored_name
        await self.session.commit()

        logger.info("已保存任务 %d 第 %d 次执行的截图：%s", task_id, attempt, stored_name)
        return self._screenshot_api_path(task_id, execution.id)

    async def read_screenshot(self, task_id: int, execution_id: int) -> tuple[Path, str]:
        """读回一张截图 —— 供**管理员鉴权**的取图接口使用。

        `screenshot_path` 是我们自己生成的文件名，但仍取 `Path(...).name`
        剥掉任何目录成分：万一以后有人从别处把值写进来，这一行就挡住了
        `../../etc/passwd` 这类路径穿越。
        """
        execution = await self.executions.get(execution_id)
        if execution is None or execution.task_id != task_id:
            raise NotFoundError("执行记录不存在")
        if not execution.screenshot_path:
            raise NotFoundError("该次执行没有截图")

        stored_name = Path(execution.screenshot_path).name
        path = settings.screenshot_dir / stored_name
        if not path.is_file():
            raise NotFoundError("截图文件已丢失")

        media_type = _ALLOWED_SCREENSHOT_SUFFIXES.get(
            Path(stored_name).suffix.lower(), "application/octet-stream"
        )
        return path, media_type

    # ==========================================================
    # 内部工具
    # ==========================================================

    @staticmethod
    def _open_execution(
        task_id: int, attempt: int, worker_name: str, started_at: datetime
    ):
        from app.models.task_execution import TaskExecution

        return TaskExecution(
            task_id=task_id,
            attempt=attempt,
            status=ExecutionStatus.RUNNING.value,
            worker_name=worker_name,
            started_at=started_at,
        )

    async def _get_running_task(self, task_id: int, worker_name: str) -> Task:
        """回传结果时校验：任务存在、正在跑、且是**这个** Worker 在跑。"""
        task = await self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("任务不存在")
        if task.status != TaskStatus.RUNNING.value:
            raise InvalidStateError(
                f"任务当前不是执行中，无法回传结果（当前状态 {task.status}）"
            )
        if task.claimed_by != worker_name:
            raise InvalidStateError(
                f"该任务不是由 {worker_name} 领取的（当前领取者 {task.claimed_by}）"
            )
        return task

    async def _set_order_status(self, order_id: int, status: OrderStatus) -> None:
        """同步订单状态。订单一定存在（`tasks.order_id` 有外键），所以不加判空。"""
        order = await self.orders.get(order_id)
        order.status = status.value

    @staticmethod
    def _screenshot_api_path(task_id: int, execution_id: int) -> str:
        return (
            f"{settings.api_v1_prefix}/tasks/{task_id}"
            f"/executions/{execution_id}/screenshot"
        )
