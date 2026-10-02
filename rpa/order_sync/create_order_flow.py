"""把一笔订单录进模拟 ERP —— 《模拟ERP设计》§3.1 的 ①→⑨。

```
① 打开 /login          → 填账号密码 → 提交       ← 由 BrowserSession 在启动时做一次
② 到达 /dashboard      → 点击「订单管理」          ← §9.3 例外一：无 testid
③ 到达 /orders         → 先搜一遍来源单号（幂等预检）
④ 到达 /orders/new     → 逐项填写表单 → 点保存
⑤ 弹出确认框           → 点「确定」
⑥ 跳转 /orders/{单号}  → 读取页面上的 erp_order_no
⑦ 点击「提交审核」      → 确认
⑧ 页面出现「已提交审核」→ 判定成功
⑨ 把 erp_order_no 回传主系统
```

这个模块**只负责操作页面**，不知道 HTTP、不知道任务、不知道截图：
成功了返回一个单号字符串，出错了抛 `errors.py` 里的类型化异常。
编排（心跳、截图、回传）全在 `rpa/main.py` —— 这样流程本身可以被
单独测试，也能在别的编排下复用。

## 两个刻意的设计

**幂等预检每次都要做**（③ 里的搜索）。不只是为了「重试时别重复下单」：
最需要它的场景是**上一次提交成功了、但回传结果时断网了**。那次执行在服务端
看起来是失败（最终会被判僵尸），任务会被重新入队 —— 如果没有预检，
第二次执行就会实实在在地录进第二笔。

**第 ⑨ 步的复核不看横幅**。提交审核后页面会显示「已提交审核」，
但那个横幅是由查询参数 `?submitted=1` 驱动的 —— 它只说明「跳转路径对了」，
不说明数据库真的改了。所以我们回列表页，按列序号把状态列读出来。
这既是 RPA 该有的怀疑精神，也让 §9.3 那个「按列序号定位」的练习有了真实职责。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from rpa.common import errors
from rpa.common.config import RpaSettings
from rpa.erp import selectors as S
from rpa.erp.pages import DashboardPage, OrderDetailPage, OrderNewPage, OrdersListPage

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page, Response

    from rpa.common.api_client import ClaimedOrder, ClaimInstruction

logger = logging.getLogger(__name__)


def execute_order(
    page: "Page",
    settings: RpaSettings,
    instruction: "ClaimInstruction",
    order: "ClaimedOrder",
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> str:
    """把一笔订单录进 ERP，返回 ERP 生成的单号。

    `should_stop` 是取消检查（心跳发现任务已不归本 Worker 时返回 True）。
    检查点放在**每次页面跳转之间** —— 粒度就到「一个页面往返」（含 1.5 秒
    页面延时时约 2~3 秒）。指望在元素等待的中途被打断不值得：那需要把每个
    `wait_for_selector` 拆成轮询，换来的响应速度提升很有限。
    """
    base = instruction.erp_url

    def check() -> None:
        if should_stop():
            raise errors.TaskCancelled()

    check()

    # ---------- ② 点导航进入列表页（§9.3 例外一） ----------
    dashboard = DashboardPage(page, settings, base)
    orders = dashboard.open_orders_list()
    check()

    # ---------- ③ 幂等预检：这笔单是不是已经录过了 ----------
    orders.search(order.order_no)
    existing_row = orders.find_by_source_no(order.order_no)

    if existing_row is not None:
        return _handle_already_recorded(
            orders, existing_row, instruction, order, check=check
        )

    # ---------- ④⑤ 填写并保存 ----------
    logger.info("录入订单 %s（SKU=%s × %s）", order.order_no, order.sku, order.quantity)
    form = orders.open_new_order()
    check()

    form.fill(_form_values(order))
    response = form.save_and_confirm()
    _guard_write_response(response, "保存订单")

    form_error = form.form_error()
    if form_error:
        # ERP 明确拒绝了。把页面上的原文带回去 —— 那就是管理员最该看的一句话。
        raise errors.FormValidationRejected(form_error)
    check()

    # ---------- ⑥ 读回 ERP 生成的单号 ----------
    detail = OrderDetailPage(page, settings, base)
    erp_order_no = detail.erp_order_no()
    logger.info("ERP 生成单号 %s", erp_order_no)

    if not instruction.submit_for_review:
        # 服务端没要求提交审核（v1 默认要求）。到这里就算干完了。
        logger.info("指令未要求提交审核，到此为止")
        return erp_order_no

    # ---------- ⑦ 提交审核 ----------
    submit_response = detail.submit_for_review_and_confirm()
    _guard_write_response(submit_response, "提交审核")
    check()

    # ---------- ⑧ 页面横幅（交叉验证，不是唯一依据） ----------
    banner = OrderDetailPage(page, settings, base).success_banner_text()
    if banner is None:
        raise errors.ResultReadFailed(
            f"提交审核后没有出现「已提交审核」横幅（单号 {erp_order_no}）"
        )

    # ---------- ⑨ 回列表，按列序号读状态列复核 ----------
    status = _verify_advanced(detail, order.order_no, check=check)
    logger.info("复核通过：%s 当前状态 %s", erp_order_no, status)
    return erp_order_no


# ============================================================
# 分支
# ============================================================


def _handle_already_recorded(
    orders: OrdersListPage,
    row,
    instruction: "ClaimInstruction",
    order: "ClaimedOrder",
    *,
    check: Callable[[], None],
) -> str:
    """幂等命中：这笔来源单号已经在 ERP 里了。

    这是**正常路径**，不是异常。触发它的典型场景：上一次执行提交成功了，
    但回传结果时网络断了 —— 服务端判失败、重新入队，于是我们又来了。

    此时唯一要判断的是「上回走到哪一步了」：详情页上还有「提交审核」按钮
    就说明只录了单没提交；按钮不在就说明整条流程都走完了。
    **用按钮的有无判断，不读状态文案** —— 按钮的有无是服务端渲染的结果，
    比中文文案稳（《模拟ERP设计》§4.6）。
    """
    erp_order_no = orders.erp_order_no_of(row)
    logger.warning(
        "幂等命中：来源单号 %s 已录入（ERP 单号 %s），不重复下单",
        order.order_no,
        erp_order_no,
    )
    check()

    detail = orders.open_detail(erp_order_no)
    check()

    if instruction.submit_for_review and detail.has_submit_button():
        logger.info("上一轮只录了单没提交审核，补上这一步")
        response = detail.submit_for_review_and_confirm()
        _guard_write_response(response, "补提交审核")
        check()

    if instruction.submit_for_review:
        status = _verify_advanced(detail, order.order_no, check=check)
        logger.info("复核通过：%s 当前状态 %s", erp_order_no, status)

    return erp_order_no


def _verify_advanced(detail: OrderDetailPage, source_order_no: str, *, check) -> str:
    """⑨ 回列表页复核状态，返回读到的语义状态。

    为什么不能只看「已提交审核」那个横幅：横幅由 `?submitted=1` 驱动，
    任何人手拼一个 URL 都能看到它。真正说明问题的是数据库里的状态，
    而列表页的状态列就是它的投影。

    这一读**必须**用列序号（§9.3 例外二）：列顺序变了这里会拿到别的单元格，
    于是文案认不出来，`parse_status` 抛 `UnknownStatusText` —— 那正是我们
    想要的「早报错」，而不是让一个错误的状态悄悄传下去。
    """
    check()
    orders = detail.open_orders_list()
    orders.search(source_order_no)

    row = orders.find_by_source_no(source_order_no)
    if row is None:
        raise errors.ResultReadFailed(
            f"复核时在列表里找不到来源单号 {source_order_no}"
        )

    status = orders.status_of(row)
    if status == S.STATUS_DRAFT:
        raise errors.ResultReadFailed(
            f"提交审核后 {source_order_no} 的状态仍是「草稿」——提交没有生效"
        )
    return status


def _guard_write_response(response: "Response", what: str) -> None:
    """把「写操作」的非正常 HTTP 状态翻成类型化异常。

    `expect_navigation` 会把目标文档的响应带回来（重定向链取最后一个），
    所以这里能直接看状态码。三种情况：

      · 503 → 模拟 ERP 的**故障注入**（`MOCK_ERP_FAIL_RATE`），专门用来
        演练重试，所以归到可重试的 `ERP_SERVER_BUSY`
      · 4xx → 校验拒绝。但这**不在这里报**：400 响应体是重新渲染的表单页，
        上面有具体的中文原因，由调用方读 `msg-form-error` 再抛
        `FormValidationRejected`，这样 `error_message` 才是人话
      · 其他 5xx → 服务端出问题了，可重试
    """
    if response.status == 503:
        raise errors.ErpServerBusy(
            f"{what}时模拟 ERP 返回 503「系统繁忙，请稍后重试」"
        )
    if response.status >= 500:
        raise errors.ErpUnavailable(f"{what}时模拟 ERP 返回 HTTP {response.status}")


def _form_values(order: "ClaimedOrder") -> dict[str, str]:
    """claim 下发的订单字段 → 表单字段。

    键就是表单里的 `name`，与主平台的订单字段同名（`source_order_no` 是个
    例外：主平台叫 `order_no`，到了 ERP 这边叫来源单号 —— 这个名字差异本身
    就是「对账」的语义：ERP 的 `source_order_no` 指回主库的 `orders.order_no`）。
    """
    return {
        "source_order_no": order.order_no,
        "customer_name": order.customer_name,
        "phone": order.phone,
        "address": order.address,
        "product_name": order.product_name,
        "sku": order.sku,
        "quantity": str(order.quantity),
        "amount": order.amount,
    }
