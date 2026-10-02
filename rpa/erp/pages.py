"""模拟 ERP 的 Page Object 层。

流程代码只调这里的方法，**不直接写选择器** —— 选择器全在 `selectors.py`，
页面改版只改那一个文件。这一层的职责是「把页面变成一个有名字的动作集合」：
`搜索(单号)` 而不是 `page.fill('[data-testid=input-search]', ...)`。

## 只等，不睡

模拟 ERP 每次 HTML 跳转都刻意慢 1.5 秒（`MOCK_ERP_PAGE_DELAY_MS`），
所以这里**一处 `sleep` 都没有**，全部用 `wait_for_selector` / `expect_navigation`。
硬等 2 秒在演示机上「看起来能跑」，换台慢机器就随机红 —— 这正是
《模拟ERP设计》§9.1 想让人踩的坑。

## 异常转换

Playwright 的异常（`TimeoutError` / `Error`）不往上层漏：这一层把它们转成
`errors.py` 里那几个带 `error_code` 的类型化异常。这样 `main.py` 拿到的
永远是一个「能直接回传给主平台」的错误码，而不是一段 playwright 栈。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from rpa.common import errors
from rpa.common.config import RpaSettings
from rpa.erp import selectors as S

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Locator, Page, Response

logger = logging.getLogger(__name__)


class BasePage:
    """所有页面的共同底座。

    `base_url` 是模拟 ERP 的根地址（由服务端在 claim 响应里下发）。
    """

    def __init__(self, page: "Page", settings: RpaSettings, base_url: str) -> None:
        self.page = page
        self.settings = settings
        self.base_url = base_url.rstrip("/")

    # ---------- 导航 ----------

    def goto(self, path: str) -> None:
        """打开一个路径。

        `wait_until="load"` 而不是 `domcontentloaded`：页面上的确认框要靠
        `/static/app.js` 跑起来才能工作，等 load 才能保证脚本已执行。
        页面延时本身就有 1.5 秒，多等这点资源开销可以忽略。
        """
        url = f"{self.base_url}{path}"
        logger.debug("打开 %s", url)
        try:
            self.page.goto(
                url,
                wait_until="load",
                timeout=self.settings.rpa_nav_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise errors.ErpUnavailable(
                f"打开 {url} 超时（{self.settings.rpa_nav_timeout_ms}ms）"
            ) from exc
        except PlaywrightError as exc:
            # 连不上（服务没起、端口填错、连接被拒）都落到这里。
            raise errors.ErpUnavailable(f"打开 {url} 失败：{exc}") from exc

    # ---------- 等待与读取 ----------

    def wait_for(self, selector: str, *, timeout_ms: int | None = None) -> "Locator":
        timeout = timeout_ms or self.settings.rpa_action_timeout_ms
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
        except PlaywrightTimeoutError as exc:
            raise errors.PageLoadTimeout(
                f"等了 {timeout}ms 也没等到元素 {selector}"
            ) from exc
        return self.page.locator(selector).first

    def read_text(self, selector: str, *, timeout_ms: int | None = None) -> str:
        """等元素出现，读它的文本（已去首尾空白）。

        **只读 `inner_text()`，不用 `text_content()`**：前者返回的是用户
        真正看见的文本，后者会把隐藏元素和注释里的内容也算进来。
        RPA 要模仿的是人眼，所以用前者。
        """
        return self.wait_for(selector, timeout_ms=timeout_ms).inner_text().strip()

    def exists(self, selector: str) -> bool:
        """元素在不在。**立刻返回，不等待** —— 用于「有则点、没有则跳过」
        这类分支判断（如详情页的提交按钮）。"""
        return self.page.locator(selector).count() > 0

    # ---------- 共有的导航条 ----------

    def open_orders_list(self) -> "OrdersListPage":
        """点导航栏的「订单管理」进入列表页。

        导航条在**每个登录后的页面上**都有，所以任何页面对象都能做这件事 ——
        包括从详情页回到列表页做复核。

        这里点的就是 §9.3 那个**刻意没有 testid** 的入口。刻意不写
        `goto("/orders")`：真实 RPA 面对老系统就是这么点过去的，
        而且点导航顺带验证了「当前页面的顶栏是好的」。
        """
        self._click(S.NAV_ORDERS)
        self.wait_for(S.ORDERS_TABLE)
        return OrdersListPage(self.page, self.settings, self.base_url)

    # ---------- 确认对话框 ----------

    def _click(self, selector: str) -> None:
        try:
            self.page.click(selector, timeout=self.settings.rpa_action_timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise errors.PageLoadTimeout(f"点击 {selector} 超时：元素不可点") from exc

    def _click_and_wait_for_navigation(self, selector: str) -> "Response":
        return self._click_locator_and_wait_for_navigation(self.page.locator(selector).first)

    def _click_locator_and_wait_for_navigation(self, locator: "Locator") -> "Response":
        """点一个会触发整页跳转的按钮，等新文档加载完，**返回那个响应**。

        用 `expect_navigation` 而不是「点完再 sleep 一下」：它监听的是真正的
        导航事件，页面快就快返回、慢就多等。而且它对**非 200 的响应同样有效** ——
        校验失败时服务端返回的是 HTTP 400 + 一整页 HTML，故障注入时返回的是
        HTTP 503 + 一段 JSON，两者都是导航，都能被它接住。

        返回响应是给调用方**看状态码用的**：400 和 503 都是「失败」，但一个是
        数据问题（别重试）、一个是系统繁忙（该重试），只有状态码能区分它们。
        重定向链上它给的是**最后一个**响应（保存成功的 303 之后那个详情页）。
        """
        try:
            with self.page.expect_navigation(timeout=self.settings.rpa_nav_timeout_ms) as nav:
                locator.click(timeout=self.settings.rpa_action_timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise errors.PageLoadTimeout(
                f"点击后 {self.settings.rpa_nav_timeout_ms}ms 内没有发生页面跳转"
            ) from exc
        return nav.value

    def confirm_dialog(self, *, accept: bool = True) -> "Response":
        """处理确认对话框，返回跳转后的响应。

        保存和提交审核都会先弹它。注意页面上的「保存」是 `type="submit"`，
        但 app.js 拦掉了 submit 事件、改成弹框 —— **真正的提交发生在点
        「确定」之后**（app.js 调原生 `form.submit()`）。所以少点这一下，
        页面上什么都不会发生，表现为「点了保存却停在原地」。
        """
        self.wait_for(S.DIALOG_CONFIRM)
        button = S.BTN_CONFIRM_YES if accept else S.BTN_CONFIRM_NO
        return self._click_and_wait_for_navigation(button)


class LoginPage(BasePage):
    """登录页。"""

    PATH = S.PATH_LOGIN

    def open(self) -> None:
        self.goto(self.PATH)

    def is_current(self) -> bool:
        """当前是不是登录页。

        只看 URL。这是《模拟ERP设计》§9.4 推荐的做法：会话过期时**任何**
        页面都会被 303 踢回 `/login`，所以「URL 变成了 /login」就是
        「会话没了」的唯一可靠信号。
        """
        return self.page.url.rstrip("/").endswith(self.PATH)

    def submit(self, username: str, password: str) -> None:
        self.wait_for(S.INPUT_USERNAME).fill(username)
        self.page.fill(S.INPUT_PASSWORD, password)
        self._click_and_wait_for_navigation(S.BTN_LOGIN)

    def error_text(self) -> str | None:
        """登录失败时页面上的中文原因。

        登录失败**不是** HTTP 401：服务端重新渲染登录页（HTTP 200）并给出
        「用户名或密码错误」。所以判断成败只能看「跳没跳走」。
        """
        if not self.exists(S.MSG_LOGIN_ERROR):
            return None
        return self.read_text(S.MSG_LOGIN_ERROR)


class DashboardPage(BasePage):
    """工作台。登录成功后的落点，也是流程 ② 的起点。"""

    PATH = S.PATH_DASHBOARD

    def open(self) -> None:
        self.goto(self.PATH)


class OrdersListPage(BasePage):
    """订单列表。幂等预检和提交后复核都在这一页。"""

    PATH = S.PATH_ORDERS

    # ---------- 搜索 ----------

    def search(self, keyword: str) -> None:
        """用搜索框搜一个关键词。

        走真实的「填输入框 → 点搜索」而不是 `goto("/orders?keyword=...")` ——
        这一步本来就是给 RPA 练「读页面 → 操作 → 再读」的。

        注意搜索是**模糊匹配**（`LIKE %kw%`，且 ERP 单号和来源单号都匹配），
        所以搜到东西不等于就是我们要的那一笔，还得用 `find_by_source_no` 精确比对。
        """
        self.wait_for(S.INPUT_SEARCH).fill(keyword)
        self._click_and_wait_for_navigation(S.BTN_SEARCH)
        self.wait_for(S.ORDERS_TABLE)

    # ---------- 读表 ----------

    def _rows(self) -> list["Locator"]:
        return self.page.locator(S.ORDERS_TABLE_ROWS).all()

    def find_by_source_no(self, source_order_no: str) -> "Locator | None":
        """按来源单号**精确**找一行。没找到返回 None。

        这是 §7.2 幂等性的眼睛：找到了就说明这笔单已经录过了。
        """
        for row in self._rows():
            cell = row.locator(S.SOURCE_ORDER_NO_COLUMN_CELL)
            if cell.count() and cell.first.inner_text().strip() == source_order_no:
                return row
        return None

    def erp_order_no_of(self, row: "Locator") -> str:
        """读某一行的 ERP 单号（第 1 列）。"""
        return row.locator(S.ERP_ORDER_NO_COLUMN_CELL).first.inner_text().strip()

    def status_of(self, row: "Locator") -> str:
        """读某一行的状态。

        **§9.3 例外二**：这一列刻意没有 testid，只能按列序号取。
        列顺序一变（比如有人在中间插了一列），这里会拿到另一个单元格的内容，
        而且**不会报错** —— 所以下面的 `parse_status` 认不出文案时会抛异常，
        这是我们能给的最早的警报。
        """
        raw = row.locator(S.STATUS_COLUMN_CELL).first.inner_text().strip()
        return S.parse_status(raw)

    # ---------- 跳转 ----------

    def find_by_erp_order_no(self, erp_order_no: str) -> "Locator":
        selector = f'[data-testid="row-order-{erp_order_no}"]'
        if not self.exists(selector):
            raise errors.ResultReadFailed(
                f"列表页里找不到 ERP 单号 {erp_order_no} 那一行"
            )
        return self.page.locator(selector).first

    def open_detail(self, erp_order_no: str) -> "OrderDetailPage":
        """打开某一笔订单的详情页。

        点那一行里的「详情」链接，而不是手拼 URL —— 定位是**相对行**的，
        这样顺带验证了「这一行确实是那一笔订单」。
        """
        link = self.find_by_erp_order_no(erp_order_no).locator(
            f'[data-testid="link-detail-{erp_order_no}"]'
        )
        self._click_locator_and_wait_for_navigation(link)
        return OrderDetailPage(self.page, self.settings, self.base_url)

    def open_new_order(self) -> "OrderNewPage":
        self._click_and_wait_for_navigation(S.BTN_CREATE_ORDER)
        self.wait_for(S.FORM_FIELDS["source_order_no"])
        return OrderNewPage(self.page, self.settings, self.base_url)


class OrderNewPage(BasePage):
    """新建订单表单页。"""

    PATH = S.PATH_ORDER_NEW

    def fill(self, values: dict[str, str]) -> None:
        """逐项填写。`values` 的键是**表单里的 name**（与主平台下发的订单字段同名）。"""
        for name, value in values.items():
            selector = S.FORM_FIELDS.get(name)
            if selector is None:
                raise ValueError(f"表单里没有名为 {name!r} 的字段")
            self.page.fill(selector, str(value))

    def save_and_confirm(self) -> "Response":
        """点保存 → 弹出确认框 → 点确定。返回跳转后的响应。

        之后页面会变成三种样子之一：
          · 成功 → 303 跳到 `/orders/{erp_order_no}`（详情页），响应 200
          · 校验失败 → HTTP 400，重新渲染本页并在 `msg-form-error` 里给出原因
          · 服务端故障注入 → HTTP 503 + 一段 JSON
        三种都不是异常（都是正常的页面跳转），由调用方看状态码 +
        `form_error()` 区分。
        """
        self._click(S.BTN_SAVE_ORDER)
        return self.confirm_dialog(accept=True)

    def form_error(self) -> str | None:
        """服务端拒绝了这次提交时页面上的中文原因，否则 None。"""
        if not self.exists(S.MSG_FORM_ERROR):
            return None
        return self.read_text(S.MSG_FORM_ERROR)


class OrderDetailPage(BasePage):
    """订单详情页。单号从这里读回去，提交审核也从这里点。"""

    def path_of(self, erp_order_no: str) -> str:
        return f"{S.PATH_ORDERS}/{erp_order_no}"

    def open(self, erp_order_no: str) -> None:
        self.goto(self.path_of(erp_order_no))

    def erp_order_no(self) -> str:
        return self.read_text(S.TEXT_ERP_ORDER_NO)

    def status(self) -> str:
        return S.parse_status(self.read_text(S.TEXT_ORDER_STATUS))

    def has_submit_button(self) -> bool:
        """「提交审核」按钮在不在。

        服务端只在状态为 `DRAFT` 时渲染它（《模拟ERP设计》§4.6），
        所以**「按钮不在」就是「已经提交过了」** —— 这是幂等判断的依据。
        刻意不用读状态文案来判断：按钮的有无是服务端的渲染结果，
        比中文文案稳。
        """
        return self.exists(S.BTN_SUBMIT_REVIEW)

    def submit_for_review_and_confirm(self) -> "Response":
        """点「提交审核」→ 确认。返回跳转后的响应（503 也要靠它认出来）。"""
        self._click(S.BTN_SUBMIT_REVIEW)
        return self.confirm_dialog(accept=True)

    def success_banner_text(self) -> str | None:
        """「已提交审核」横幅。只在带 `?submitted=1` 时渲染。

        它只是个交叉验证 —— **不能只信它**，真正的确认是回列表读状态列
        （见 `create_order_flow` 的第 ⑨ 步）。
        """
        if not self.exists(S.MSG_SUCCESS):
            return None
        return self.read_text(S.MSG_SUCCESS)


class InventoryPage(BasePage):
    """库存查询页。只读，给 RPA 练「读表格 → 判断」。"""

    PATH = S.PATH_INVENTORY

    def open(self) -> None:
        self.goto(self.PATH)
        self.wait_for(S.INVENTORY_TABLE)

    def quantity_of(self, sku: str) -> int:
        raw = self.read_text(S.qty_cell(sku))
        try:
            return int(raw)
        except ValueError as exc:
            raise errors.ResultReadFailed(
                f"SKU {sku!r} 的库存单元格里不是整数：{raw!r}"
            ) from exc
