"""后台作业进程 —— 一个进程跑多个周期性作业，优雅关闭。

启动方式：

    cd backend && PYTHONPATH=. python -m app.workers.main

**为什么要有这么一个进程，而不是把定时任务塞进 FastAPI 的 lifespan**：
Web 进程和后台作业的生命周期诉求相反。Web 进程要能横向多开、重启不心疼；
后台作业要的是「全局只有一份在跑」。塞进 lifespan 会同时违反两条 ——
多开几个 uvicorn worker 就有几份回收在扫同一张表，还会让「停掉一个 Web
副本」顺带停掉它那份回收。分开之后，Web 想开几个开几个，作业进程守着唯一一份。

**为什么不是每加一个作业就写一个脚本**：作业的骨架（起停、异常隔离、退避）
是完全一样的，只有「隔多久」和「干什么」不同。这里把骨架抽出来，Phase 5 的
AI Worker 只要往 `build_jobs()` 里加一行，不用再抄一遍信号处理和关闭流程。
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import configure_logging
from app.database.mysql import dispose_engine
from app.database.redis import close_redis

logger = logging.getLogger("app.workers.main")


@dataclass(slots=True, frozen=True)
class Job:
    """一个周期性作业：名字（日志用）+ 间隔 + 无参协程。

    作业自己不关心调度 —— 它只实现「跑一轮」，间隔由这里给。
    """

    name: str
    interval_seconds: int
    run: Callable[[], Awaitable[object]]


def build_jobs() -> list[Job]:
    """本进程要托管的全部作业。

    延迟导入这两个作业模块：它们都 import 了 ORM 模型，放在函数里能让这个模块
    的 import 保持极轻 —— 排查「作业进程起不来」时，先能看到 `build_jobs`
    之前的那几行日志，而不是卡在 ORM 初始化上。
    """
    from app.services.ai_reconciler import reconcile_once
    from app.workers.zombie_reaper import reap_once

    return [
        Job(
            name="zombie_reaper",
            interval_seconds=settings.zombie_scan_interval_seconds,
            run=reap_once,
        ),
        # AI 分析队列的补偿（L1 入队失败 / L2 分析超时）。与僵尸回收共用同一个
        # 扫描周期：两者都是「扫库看有没有卡住的单」，没有理由分成两个频率。
        Job(
            name="ai_reconciler",
            interval_seconds=settings.zombie_scan_interval_seconds,
            run=reconcile_once,
        ),
    ]


async def _run_job(job: Job, stop: asyncio.Event) -> None:
    """单个作业的主循环：跑一轮 → 等一个间隔（可被停止信号打断）→ 再跑。

    异常**不往外抛**：一个作业炸了不该带走整个进程，更不该带走别的作业。
    记下 traceback 后继续下一轮 —— 僵尸回收这种东西，下一轮很可能就好了
    （比如 MySQL 刚重启完）。
    """
    while not stop.is_set():
        try:
            await job.run()
        except Exception:
            logger.exception("作业 %s 本轮执行失败，%d 秒后重试", job.name, job.interval_seconds)

        try:
            # 用 `stop.wait()` 配合超时，而不是 `asyncio.sleep`：
            # 收到 SIGTERM 时立刻醒，不必等满一整个间隔才退出。
            await asyncio.wait_for(stop.wait(), timeout=job.interval_seconds)
        except TimeoutError:
            continue  # 正常到期，进入下一轮


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT / SIGTERM → 置位 stop，让主循环走到优雅关闭那一段。

    信号处理挂在事件循环上（`add_signal_handler`）而不是 `signal.signal`：
    后者是同步回调、在别的线程上下文里跑，从那儿去 set 一个 asyncio.Event
    并不安全。Windows 上 `add_signal_handler` 会抛 NotImplementedError，
    那时退回 `signal.signal` —— 本项目的部署目标是 Linux / macOS，
    这条退路只是让本地在 Windows 上也能 `Ctrl-C` 退出。
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())


async def main() -> int:
    configure_logging(logging.DEBUG if settings.debug else logging.INFO)
    jobs = build_jobs()
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    logger.info(
        "后台作业进程启动：%s",
        "、".join(f"{j.name}(每 {j.interval_seconds}s)" for j in jobs) or "（无作业）",
    )

    tasks = [asyncio.create_task(_run_job(job, stop), name=job.name) for job in jobs]
    try:
        await stop.wait()
        logger.info("收到停止信号，退出中……")
    finally:
        for task in tasks:
            task.cancel()
        # return_exceptions=True：取消会让 task 抛 CancelledError，
        # 用 gather(..., return_exceptions=True) 收干净，避免退出时刷一堆
        # 「Task was destroyed but it is pending」的噪音掩盖真正的日志。
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispose_engine()
        await close_redis()
        logger.info("后台作业进程已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
