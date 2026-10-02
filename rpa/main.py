"""RPA Worker 的入口与编排。

职责边界分得很清楚：

| 层 | 文件 | 知道什么 |
| --- | --- | --- |
| 编排 | **本文件** | 知道 HTTP、知道任务、知道截图 |
| 流程 | `order_sync/create_order_flow.py` | 只知道页面（点了什么、读回什么） |
| 页面 | `erp/pages.py` + `erp/selectors.py` | 只知道 DOM |

这样拆的好处是流程能**单独测试**（给它一个 page 就能跑），也能在别的编排下复用
（比如以后要加一个「只查库存不落单」的 Worker，编排换掉、流程不动）。

## 一次执行的完整生命周期

```
claim 领任务
  → ensure_logged_in（会话过期就在这重登）
  → 起心跳线程
  → execute_order 跑 ①→⑨        ← 出错抛 errors.py 里的类型化异常
  → 失败：在失败现场截图           ← 页面的状态还留着，最有价值
  → 停心跳
  → 先传截图，再回传结果           ← 顺序不能反，见 _report
```

## 两个刻意的设计

**心跳是线程，不是协程。** Playwright 的同步 API 绑定在创建它的线程上、且不能在
asyncio 事件循环里跑，所以主线程必须是普通线程。心跳是唯一需要「同时」进行的东西，
而它只做 HTTP、不碰浏览器 —— 两者没有共享状态，拆到另一个线程是安全的，
也是最小代价的解法。换成 asyncio 会在每个页面操作上撒 `await`，却换不来任何
并发收益（浏览器操作本来就是串行的）。

**取消时什么都不回传。** 心跳拿到 `cancel=true` 说明这活已经不归本 Worker 了
（被僵尸回收、或任务已结束）。此时回传只会拿到 409 `4009`，把一次正常的
状态变更记成一条 Worker 报错。所以 `TaskCancelled` 刻意不继承 `RpaError`，
`execute_one` 遇到它就返回 `None`，`run_once` 见到 `None` 直接跳过回传。
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from dataclasses import dataclass

from rpa.common import errors
from rpa.common.api_client import ApiClient, ClaimData
from rpa.common.browser import BrowserSession
from rpa.common.config import RpaSettings, settings as default_settings
from rpa.common.logger import STARTED_MARKER, setup_logging
from rpa.order_sync.create_order_flow import execute_order

logger = logging.getLogger(__name__)

#: 领任务/回传失败后的重试间隔。真实故障（主平台重启）通常几秒到几十秒，
#: 5 秒足够快地恢复，又不至于把失败的日志刷屏。
_ERROR_RETRY_SECONDS = 5.0


@dataclass
class Outcome:
    """一次执行的结论。由 `execute_one()` 组装，由 `_report()` 消费。

    这个结构存在的意义是：**把「跑流程」和「回传结果」解耦**。
    `execute_one` 负责在失败现场把该抓的（错误分类、人话原因、截图）都抓齐，
    `_report` 只负责按契约发出去，不需要知道流程长什么样。
    """

    success: bool
    erp_order_no: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: 失败现场的 PNG 字节（`page.screenshot()` 直接返回 bytes，不用临时文件）。
    #: 只有失败时才有；截图失败也不影响任务判定。
    screenshot_png: bytes | None = None
    duration_ms: int = 0


class Heartbeat:
    """后台心跳线程。

    每隔 `interval_seconds` 打一次 `/rpa/tasks/{id}/heartbeat`。服务端回
    `cancel=true`（任务已被回收或结束）就 set `cancel_event`，流程会在下一次
    页面跳转之间发现它并抛 `TaskCancelled`。

    **心跳自身的网络错误只记日志，不中断执行**：短期失败无所谓；真的一直失败，
    服务端的僵尸回收会把任务捞回去重新入队 —— 那才是设计好的兜底。
    """

    def __init__(
        self,
        client: ApiClient,
        task_id: int,
        *,
        interval_seconds: int,
        cancel_event: threading.Event,
    ) -> None:
        self._client = client
        self._task_id = task_id
        self._interval = interval_seconds
        self._cancel_event = cancel_event
        self._stop = threading.Event()
        # daemon=True：即使某个心跳请求卡在超时里，进程退出时也不会被它拖住。
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{task_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """通知线程收工并等它退出。

        用 `Event.wait()` 做等待，所以 set 之后线程立刻醒来，`join()` 基本不会阻塞。
        """
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        # 第一次心跳等满一个间隔再打：刚领到任务时服务端那边必然还是 RUNNING，
        # 立刻问一次纯属多一个往返。
        while not self._stop.wait(self._interval):
            try:
                data = self._client.heartbeat(self._task_id)
            except Exception:  # noqa: BLE001 - 心跳失败绝不能让线程死掉
                logger.warning("心跳失败（忽略，连续失败由僵尸回收兜底）", exc_info=True)
                continue

            if data.cancel:
                logger.warning("服务端已把任务 %s 转走/结束，通知流程停手", self._task_id)
                self._cancel_event.set()
                return
            logger.debug("心跳 ok：task=%s status=%s", data.task_id, data.status)


def execute_one(
    client: ApiClient,
    browser: BrowserSession,
    settings: RpaSettings,
    claim: ClaimData,
) -> Outcome | None:
    """跑完领到的那一笔：起心跳 → 跑流程 → 失败截图 → 停心跳。

    返回 `None` 表示**什么都不该回传**（任务已不归本 Worker）。
    """
    task_id = claim.task.id
    instruction = claim.instruction

    cancel_event = threading.Event()
    heartbeat = Heartbeat(
        client,
        task_id,
        interval_seconds=settings.rpa_heartbeat_seconds,
        cancel_event=cancel_event,
    )

    started = time.monotonic()
    outcome: Outcome | None = None
    heartbeat.start()
    try:
        # 登录放在 try 里面：登录失败（ERP 没起、口令不对）也是一次**要回传的失败**。
        # 放在外面的话任务会一直挂在 RUNNING，只能等僵尸回收 —— 把一个问题放大成两个。
        browser.ensure_logged_in(instruction.erp_url)

        erp_order_no = execute_order(
            browser.page,
            settings,
            instruction,
            claim.order,
            should_stop=cancel_event.is_set,
        )
        outcome = Outcome(success=True, erp_order_no=erp_order_no)

    except errors.TaskCancelled:
        # 不是失败，是「这活已经不是我的了」。回传只会拿 409，反而污染日志。
        logger.warning("任务 %s 已不归本 Worker，停止执行且不回传", task_id)

    except errors.RpaError as exc:
        logger.error("任务 %s 失败（%s）：%s", task_id, exc.code, exc.message)
        if exc.permanent:
            # permanent 只影响这条日志。v1 的接口没有「别重试」这个信号，
            # 服务端仍会重试 3 次 —— 已知局限，写在 README 里。
            logger.warning("该失败属永久性（%s），但服务端仍会重试", exc.code)
        outcome = Outcome(
            success=False,
            error_code=exc.code,
            error_message=exc.message,
            screenshot_png=browser.screenshot(),
        )

    except Exception as exc:  # noqa: BLE001 - 顶层兜底，见下
        # 没预料到的异常同样要回传：不回传任务就一直挂在 RUNNING 等僵尸回收。
        # 这类错误恰恰最需要截图 —— 它就是「代码没想到」的那一类。
        logger.exception("任务 %s 出现未预期异常", task_id)
        outcome = Outcome(
            success=False,
            error_code=errors.UNEXPECTED_CODE,
            error_message=f"{type(exc).__name__}: {exc}",
            screenshot_png=browser.screenshot(),
        )

    finally:
        heartbeat.stop()

    if outcome is None:
        return None
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    return outcome


def _report(client: ApiClient, task_id: int, attempt: int, outcome: Outcome) -> None:
    """把结论回传主平台。

    **截图必须先于结果**，顺序不能反：截图接口要求该次 execution 还处于
    `RUNNING`（《API接口设计》§10.4），一旦结果回传完这次执行就闭合了，
    附件没地方挂 —— 那时再传只会拿 4xx。

    传附件失败**不该**让「回传失败原因」变成「回传不出去」：管理员最需要的是
    `error_code` / `error_message`，截图是锦上添花。所以这里吞掉并只记日志。
    """
    if outcome.screenshot_png:
        try:
            client.upload_screenshot(task_id, attempt, outcome.screenshot_png)
            logger.info("已上传失败截图（%d 字节）", len(outcome.screenshot_png))
        except errors.RpaError as exc:
            logger.warning("上传失败截图失败（不影响结果回传）：%s", exc.message)

    client.report_result(
        task_id,
        success=outcome.success,
        erp_order_no=outcome.erp_order_no,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        duration_ms=outcome.duration_ms,
    )


def run_once(
    client: ApiClient,
    browser: BrowserSession,
    settings: RpaSettings,
) -> Outcome | None:
    """领一个任务、跑完、回传。返回这次的结论；没领到任务时返回 `None`。

    `claim()` 自带长轮询，所以队列空时这里会挂起等一段（默认 20 秒），
    而不是空转打接口。
    """
    claim = client.claim()
    if claim is None:
        logger.info("队列为空，没有领到任务")
        return None

    task = claim.task
    logger.info(
        "领到任务 %s：订单 %s（priority=%s attempt=%s need_review=%s）",
        task.id,
        claim.order.order_no,
        task.priority,
        task.attempt,
        task.need_review,
    )

    outcome = execute_one(client, browser, settings, claim)
    if outcome is None:
        # 被取消了：任务已经归别人，回传只会 409。
        return None

    try:
        _report(client, task.id, task.attempt, outcome)
    except errors.RpaError as exc:
        # 回传失败（任务在跑的过程中被回收、主平台重启）。落回队列由服务端兜底，
        # 这里只记日志 —— 别让一次回传失败把长跑 Worker 带走。
        logger.error("回传任务 %s 的结果失败：%s", task.id, exc.message)
        return outcome

    if outcome.success:
        logger.info("任务 %s 完成：ERP 单号 %s", task.id, outcome.erp_order_no)
    else:
        logger.warning("任务 %s 已按失败回传：%s", task.id, outcome.error_code)
    return outcome


def run_forever(
    client: ApiClient,
    browser: BrowserSession,
    settings: RpaSettings,
) -> None:
    """领任务的主循环，直到收到中断信号。

    这一层是长跑 Worker 最外圈的护栏：领任务/回传的失败（主平台重启、网络抖动、
    甚至 httpx 直接抛的连接错误）都在这儿被挡住，歇几秒重来 —— 一个瞬时故障
    不该把进程打死。任务级的失败在 `execute_one` 里就已经变成一次回传了，
    到不了这里。
    """
    while True:
        try:
            run_once(client, browser, settings)
        except Exception:  # noqa: BLE001 - 最外层护栏，见 docstring
            logger.exception("这一轮出错，%.0f 秒后重试", _ERROR_RETRY_SECONDS)
            time.sleep(_ERROR_RETRY_SECONDS)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rpa-worker",
        description="AI-RPA 平台的 RPA Worker：从主平台领任务，用 Playwright 录进模拟 ERP。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只领一笔就跑完退出（演示/冒烟用）；默认是长跑循环",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="打开有头浏览器，演示时能看着它一步步点（默认无头）",
    )
    parser.add_argument(
        "--worker-name",
        default=None,
        help="覆盖 RPA_WORKER_NAME，便于同机起多个 Worker 做实验",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别；心跳日志只在 DEBUG 下可见",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(args.log_level)

    # 命令行开关覆盖配置：`--headed` 是演示开关，`--worker-name` 便于起多个实例。
    overrides: dict[str, object] = {}
    if args.worker_name:
        overrides["rpa_worker_name"] = args.worker_name
    if args.headed:
        overrides["rpa_headless"] = False
    settings = default_settings.model_copy(update=overrides) if overrides else default_settings

    logger.info(
        "RPA Worker 启动：name=%s 主平台=%s headless=%s",
        settings.rpa_worker_name,
        settings.api_base_url,
        settings.rpa_headless,
    )

    client = ApiClient(settings)
    browser = BrowserSession(settings)
    try:
        client.login()
        # 浏览器**不在登录主平台之前起**：主平台都连不上时开 Chromium 是白开。
        browser.start()
        logger.info(STARTED_MARKER)

        if args.once:
            run_once(client, browser, settings)
        else:
            run_forever(client, browser, settings)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出")
    except errors.RpaError as exc:
        logger.error("启动失败：%s", exc.message)
        return 2
    finally:
        browser.close()
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
