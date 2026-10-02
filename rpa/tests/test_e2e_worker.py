"""端到端：真的开浏览器，把一笔订单录进模拟 ERP。

跑这个需要**三样东西都在**：

  1. 主平台（`./scripts/start.sh`）
  2. 模拟 ERP（`./scripts/start_mock_erp.sh`）
  3. 本机装了 Chromium（`playwright install chromium`）

少任何一样都会**跳过**而不是失败（见 conftest 的 `live_services`）。
默认 `addopts` 里带 `-m "not e2e"`，所以平时不会跑；要跑得显式：

    python -m pytest rpa/tests -m e2e

## 为什么用子进程调 `seed_demo_task.py` 来造任务

`TaskService.generate()` 没有 HTTP 入口（那是 Phase 5 AI 的调用点），
而 Worker 侧**刻意不连数据库** —— 这条测试如果自己去连 MySQL 造任务，
就等于在测试里违反被测系统最重要的那条约束。

`seed_demo_task.py` 是那个「从 Python 侧造数据」的既有入口，它自己读
backend 的配置、自己连库。所以这里 subprocess 调它，测试自己始终站在
「Worker + HTTP」这一侧 —— 顺便也把这个脚本真跑了一遍。

## 为什么断言用管理员接口

任务状态、execution 的 `erp_order_no`、截图能不能取回来，这些只有管理员看得到
（Worker 角色连订单列表都打不开）。测试需要更宽的视角来验收，所以换个身份读。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rpa.common.api_client import ApiClient
from rpa.common.browser import BrowserSession
from rpa.common.config import RpaSettings
from rpa.erp.pages import OrdersListPage
from rpa.main import run_once

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_BIN = sys.executable


def _seed(order_no: str, sku: str = "SKU-001", quantity: int = 1) -> int:
    """跑 seed 脚本，返回它建出来的 task_id。

    子进程而不是 import：`scripts/` 不在包路径里，它自己往 sys.path 插 backend/。
    这里尊重那个边界，不去 import 它的内部函数。
    """
    result = subprocess.run(
        [
            PYTHON_BIN, "scripts/seed_demo_task.py",
            "--order-no", order_no, "--sku", sku, "--quantity", str(quantity),
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"seed 脚本失败（{result.returncode}）\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    match = re.search(r"task_id=(\d+)", result.stdout)
    assert match, f"没从 seed 输出里解析到 task_id：\n{result.stdout}"
    return int(match.group(1))


def _wait_for_final_status(admin_api, task_id: int, timeout: float = 90.0) -> dict:
    """轮询任务，直到它离开「运行中」这类中间态。

    Worker 是同步跑的，`run_once` 返回时结果**已经回传完了**，所以正常
    情况下第一次查就是终态。留这个轮询是为了兜底：万一回传走了重试路径，
    这里能等到它落定，而不是拿一个中间态去做断言。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = admin_api.get(f"/tasks/{task_id}")
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
            return last
        time.sleep(0.5)
    pytest.fail(f"任务 {task_id} 在 {timeout}s 内没到终态，最后一次是 {last!r}")


def _executions(admin_api, task_id: int) -> list[dict]:
    response = admin_api.get(f"/tasks/{task_id}/executions")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _lookup_in_erp(browser, settings: RpaSettings, source_order_no: str) -> tuple[str, str] | None:
    """回 ERP 里按来源单号**真查一遍**，返回 `(erp_order_no, 状态)`，查不到返回 None。

    「RPA 到底录没录进去」的终审在这里。主平台那边只能证明「Worker 说自己成功了」，
    所以《模拟ERP设计》把对账定义成两件事：`task_executions.erp_order_no` 有值，
    **且能在 ERP 里按 `source_order_no` 查到那一笔**。

    刻意**再走一遍界面**（点导航 → 搜索 → 读表格）而不是连 MySQL 查 `mock_erp.erp_orders`：
    一来 Worker 的依赖里本来就没有数据库驱动，二来查库只是「能在 ERP 里查到」的
    等价实现，而走界面顺带把列表页那几个选择器也验了一遍。

    用的是 Worker 自己那个浏览器和那次登录 —— 会话是复用的，这里不用重新登。
    """
    page = OrdersListPage(browser.page, settings, browser.erp_url)
    page.open_orders_list()
    page.search(source_order_no)
    row = page.find_by_source_no(source_order_no)
    if row is None:
        return None
    return page.erp_order_no_of(row), page.status_of(row)


@pytest.fixture
def worker(settings: RpaSettings):
    """一个起好了的 Worker：登录主平台 + 开好浏览器。

    用 `with` 保证浏览器一定被关掉 —— 测试失败时漏掉一个 Chromium 进程，
    下一次跑就会莫名其妙地慢。

    **`BrowserSession` 的 `__enter__` 本身就是 `start()`**，所以这里只开一次。
    别在 `with` 块里再补一句 `browser.start()`：第二次 `sync_playwright().start()`
    会撞上第一次留下的、仍被标记为 running 的事件循环，报
    「It looks like you are using Playwright Sync API inside the asyncio loop」——
    那个报错把人往 asyncio 的方向带，真正的原因却是重复启动。

    （机制：`start()` 把协议分发器停在一个 greenlet 里跑 `loop.run_until_complete`，
    循环于是**挂起但未停止**，`asyncio.get_running_loop()` 因此仍能拿到它。）

    先登录主平台再开浏览器：平台连不上或口令不对时立刻失败，
    免得白等一次 Chromium 启动。
    """
    client = ApiClient(settings)
    try:
        client.login()
        with BrowserSession(settings) as browser:
            yield client, browser
    finally:
        client.close()


@pytest.fixture
def settings() -> RpaSettings:
    """直接读 .env 的真实配置（含 Worker 账号、模拟 ERP 账号）。

    `rpa_headless` 保持配置里那个值：CI/无显示器环境跑不了有头，
    想看着它点就改 .env 或用 `RPA_HEADLESS=false`。
    """
    return RpaSettings()


def _cancel(admin_api, task_id: int, reason: str) -> None:
    """把测试留下的任务收走，别让它继续留在队列里。

    失败的那笔会被服务端**重新入队**（这正是设计好的重试行为）。如果测试
    不管它，下一次 `-m e2e` 就会先领到它 —— 而我们那条「Worker 领的是不是
    我们这笔」的断言会红，看起来和真正的原因毫不相关。
    """
    response = admin_api.post(f"/tasks/{task_id}/cancel", json={"reason": reason})
    assert response.status_code == 200, f"清理失败：{response.text}"


def _assert_worker_took_our_task(admin_api, task_id: int) -> None:
    """确认 Worker 领走的**就是**刚 seed 的那一笔。

    队列是 FIFO 的，领任务又没有过滤条件 —— 如果队列里还堆着别的前置任务，
    Worker 会先干那些，我们这笔一直停在 QUEUED。早点把这件事说清楚，
    比等 90 秒后拿一个含糊的超时好得多。
    """
    assert _executions(admin_api, task_id), (
        f"任务 {task_id} 没有任何执行记录 —— Worker 领走的多半是队列里别的任务。\n"
        f"e2e 的前置条件是「队列里除了刚 seed 的这一笔，不能有别的待办」。"
    )


def test_worker_records_an_order_end_to_end(admin_api, worker, settings) -> None:
    """开心路径：seed → 领任务 → 开浏览器走完 ①→⑨ → 回传 → 管理员接口验收。

    断言分三层，从外到内：
      1. 任务终态是 SUCCESS
      2. execution 落了一条 SUCCESS，且 `duration_ms` 合理
      3. 拿 Worker 回传的单号回 ERP 列表页**真查一遍**，单号对得上、状态是「待审核」

    第 3 层是关键：前两层只说明「Worker 说自己成功了」，只有第 3 层能证明
    那笔订单真的躺在 ERP 里。它走的是 Worker 自己那个浏览器（会话是复用的）。
    """
    client, browser = worker
    order_no = f"E2E{int(time.time())}"
    task_id = _seed(order_no, sku="SKU-001", quantity=1)

    outcome = run_once(client, browser, settings)

    assert outcome is not None, "没领到任务 —— 队列里应该有刚 seed 的那一笔"
    assert outcome.success is True, f"录单失败：{outcome.error_code} {outcome.error_message}"
    assert outcome.erp_order_no, "成功却没拿到 ERP 单号"
    _assert_worker_took_our_task(admin_api, task_id)

    task = _wait_for_final_status(admin_api, task_id)
    assert task["status"] == "SUCCESS"

    executions = _executions(admin_api, task_id)
    assert executions, "任务成功了却没有 execution 记录"
    latest = executions[0]
    assert latest["status"] == "SUCCESS"
    # 页面延时 1500ms ×若干次跳转，几秒是底线；这里只拦「明显不对劲」的下限。
    assert latest["duration_ms"] > 1000

    # 第 3 层：回 ERP 里查那一笔。
    #
    # 这里**不**断言 execution 上有 `erp_order_no`：§9.2 定义执行记录时就没有这个出参
    # （只有 error_code / screenshot_path 这些现场字段）—— 库里的
    # `task_executions.erp_order_no` 只落库、不出参。对账的落点就是下面这次 ERP 查询。
    found = _lookup_in_erp(browser, settings, order_no)
    assert found is not None, (
        f"ERP 里查不到 source_order_no={order_no} 的订单 —— "
        f"单号 {outcome.erp_order_no} 只是 Worker 自己说的"
    )
    erp_order_no, erp_status = found
    assert erp_order_no == outcome.erp_order_no, "ERP 里的单号与 Worker 回传的对不上"
    assert erp_status == "PENDING_REVIEW", f"订单没停在待审核，而是 {erp_status}"


def test_worker_reports_a_validation_rejection_with_a_screenshot(admin_api, worker, settings) -> None:
    """永久性失败：SKU-003 库存为 0，ERP 会在保存时拒绝。

    要验的是三件事：
      · 错误被分类成 `ERP_VALIDATION_REJECTED`（而不是一个泛泛的异常）
      · `error_message` 是 ERP 页面上那句话的**原文**（管理员最需要的一句）
      · 失败截图**传上来了**，而且能通过带鉴权的接口取回

    最后一条尤其重要：它验证了「截图先于结果回传」这个顺序。顺序反了的话，
    上传会在 execution 闭合之后发生，结果是拿不到图 —— 而任务本身看起来
    还是正常失败的，很难发现。
    """
    client, browser = worker
    order_no = f"E2E{int(time.time())}BAD"
    task_id = _seed(order_no, sku="SKU-003", quantity=1)

    outcome = run_once(client, browser, settings)

    assert outcome is not None
    assert outcome.success is False
    assert outcome.error_code == "ERP_VALIDATION_REJECTED"
    assert outcome.error_message and "库存不足" in outcome.error_message
    _assert_worker_took_our_task(admin_api, task_id)

    executions = _executions(admin_api, task_id)
    latest = executions[0]
    assert latest["error_code"] == "ERP_VALIDATION_REJECTED"
    assert latest["screenshot_path"], "失败现场没留下截图"

    # 截图接口要求管理员 JWT，且路径是带鉴权的取图路由（不是静态 URL）。
    image = admin_api.get(f"/tasks/{task_id}/executions/{latest['id']}/screenshot")
    assert image.status_code == 200, image.text
    assert image.headers["content-type"].startswith("image/png")
    # PNG 的魔数。只判「非空」不够 —— 一个 200 + 空 body 也能骗过非空检查。
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 收尾：这笔会被重试重新入队，得手动取消，否则污染下一次 e2e。
    _cancel(admin_api, task_id, reason="e2e 清理")
