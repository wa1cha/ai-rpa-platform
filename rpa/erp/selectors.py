"""模拟 ERP 的 DOM 契约 —— **所有选择器的唯一出处**。

页面模板改了，只改这一个文件。散落各处的选择器字符串是 RPA 项目里
最常见的腐烂方式：改一个 testid，要在五个文件里找。

## 两种定位方式并存，这是刻意的

《模拟ERP设计》§4.1 规定所有 RPA 需要定位的元素都带 `data-testid`，
但 §9.3 又刻意留了**两处不给**，逼 RPA 代码里同时存在两种定位方式：

| 位置 | 定位方式 | 为什么留着 |
| --- | --- | --- |
| `/dashboard` 的「订单管理」入口 | 结构定位（`a[href="/orders"]`） | 真实系统不会给你 testid，文本/结构定位是必备技能 |
| `/orders` 表格的状态列 | **列序号**（第 7 列） | 按列序号取值非常脆弱 —— 列顺序一改就静默取到错的单元格 |

第二处的脆弱是**要演示的东西**，不是要修的 bug。所以在流程里给了它一个
真实职责（提交后回列表复核状态），而不是写个假动作应付。

## 状态文案

状态列和详情页的状态都是**中文文案**。这里有一个绕不开的事实：
自家代码可以用枚举判断，而 RPA 只能看见渲染出来的文字。
所以下面有一张集中的映射表 —— 这是 RPA 的固有脆弱点，能做的是把它
收到一处、并且**认不出的文案直接报错，不去猜**。
"""

from __future__ import annotations

# ============================================================
# 路由
# ============================================================

PATH_LOGIN = "/login"
PATH_DASHBOARD = "/dashboard"
PATH_ORDERS = "/orders"
PATH_ORDER_NEW = "/orders/new"
PATH_INVENTORY = "/inventory"

# ============================================================
# 通用：确认对话框（base.html）
# ============================================================

#: 保存 / 提交审核都会先弹它。页面上的按钮是 `type="button"`，
#: 点它**不会**提交表单 —— 真正的提交发生在点「确定」之后：
#: app.js 里调的是原生 `form.submit()`（绕过 HTML 校验，也绕过 submit 事件）。
#: 所以「点保存 → 点确定」这两下都必须做，少一下页面上什么都没发生。
DIALOG_CONFIRM = '[data-testid="dialog-confirm"]'
BTN_CONFIRM_YES = '[data-testid="btn-confirm-yes"]'
BTN_CONFIRM_NO = '[data-testid="btn-confirm-no"]'

#: 反爬用的会话 token，渲染在 <meta> 里。Worker 走浏览器所以用不到它 ——
#: 这里只是标注它的存在：它挡的正是「用 requests 直接打 ERP 内部接口」这条捷径。
META_UI_TOKEN = 'meta[name="erp-ui-token"]'

# ============================================================
# 登录页
# ============================================================

#: 登录失败**不是** HTTP 401，而是一个 200 的登录页加上这句中文。
#: 所以「有没有登录成功」只能靠「跳没跳到 /dashboard」来判断。
MSG_LOGIN_ERROR = '[data-testid="msg-login-error"]'
INPUT_USERNAME = '[data-testid="input-username"]'
INPUT_PASSWORD = '[data-testid="input-password"]'
BTN_LOGIN = '[data-testid="btn-login"]'

# ============================================================
# 工作台
# ============================================================

#: 【§9.3 例外一】「订单管理」入口**没有** testid。
#: 用 href 定位比用文本稳一点点（文本会被翻译/改文案），
#: 但两者都是「真实系统里唯一能用的办法」这一类。
#: 刻意**不用** `page.goto("/orders")` 直达 —— 真实 RPA 就是靠点导航走的。
NAV_ORDERS = 'a[href="/orders"]'
NAV_INVENTORY = '[data-testid="nav-inventory"]'
STAT_PENDING_REVIEW = '[data-testid="stat-pending-review"]'

# ============================================================
# 订单列表
# ============================================================

ORDERS_TABLE = '[data-testid="table-orders"]'
#: 数据行。空列表时 tbody 里会有一条 colspan=8 的占位行，
#: 所以判断「搜到没有」要用 row-order-{no} 这个具体 testid，不能数行数。
ORDERS_TABLE_ROWS = '[data-testid="table-orders"] tbody tr[data-testid^="row-order-"]'
BTN_CREATE_ORDER = '[data-testid="btn-create-order"]'
INPUT_SEARCH = '[data-testid="input-search"]'
BTN_SEARCH = '[data-testid="btn-search"]'
BTN_PAGE_NEXT = '[data-testid="btn-page-next"]'

#: 【§9.3 例外二】状态列**没有** testid，只能按列序号取。
#: 0-based 下标 6 = 第 7 列（列顺序见 orders_list.html 的 <th>：
#: ERP单号 / 来源单号 / 客户 / 商品 / 数量 / 金额 / **状态** / 操作）。
#: 用 CSS 的 `:nth-child` 所以要 +1。
STATUS_COLUMN_INDEX = 6
STATUS_COLUMN_CELL = f"td:nth-child({STATUS_COLUMN_INDEX + 1})"

#: 第 1 列（0-based 0）是 ERP 单号 —— 幂等命中时要把它读出来。
ERP_ORDER_NO_COLUMN_INDEX = 0
ERP_ORDER_NO_COLUMN_CELL = f"td:nth-child({ERP_ORDER_NO_COLUMN_INDEX + 1})"

#: 第 2 列（0-based 1）是来源单号。
#: 搜索框是**模糊匹配**（`LIKE %kw%`，且同时匹配 ERP 单号和来源单号），
#: 所以搜出来的行不一定就是我们那一笔 —— 必须再按这一列做一次**精确**比对，
#: 否则「MOCK2026001」的搜索结果里混进「MOCK20260010」是很正常的事。
SOURCE_ORDER_NO_COLUMN_INDEX = 1
SOURCE_ORDER_NO_COLUMN_CELL = f"td:nth-child({SOURCE_ORDER_NO_COLUMN_INDEX + 1})"

# ============================================================
# 新建订单
# ============================================================

#: 表单字段：`表单里的 name` → `定位用的 testid`。
#: 用 name 作 key 是因为「填什么值」是流程的事，而「填到哪个框」是页面的事；
#: 这样流程侧只认 name（和主平台下发的订单字段同名），页面改版只改这张表。
FORM_FIELDS: dict[str, str] = {
    "source_order_no": '[data-testid="input-source-order-no"]',
    "customer_name": '[data-testid="input-customer-name"]',
    "phone": '[data-testid="input-phone"]',
    "address": '[data-testid="input-address"]',
    "product_name": '[data-testid="input-product-name"]',
    "sku": '[data-testid="input-sku"]',
    "quantity": '[data-testid="input-quantity"]',
    "amount": '[data-testid="input-amount"]',
}

BTN_SAVE_ORDER = '[data-testid="btn-save-order"]'
#: 校验失败时服务端重新渲染本页（HTTP 400）+ 这句中文原因。
#: 我们把它的**原文**回传给主平台，因为那正是管理员最该看到的一句话。
MSG_FORM_ERROR = '[data-testid="msg-form-error"]'
#: SKU 失焦时前端会实时查库存，把结果写在这里。Worker 不依赖它 ——
#: 服务端才是权威（§7.2 库存只校验不扣减），这里只是标注它的存在。
MSG_STOCK_HINT = '[data-testid="msg-stock-hint"]'

# ============================================================
# 订单详情
# ============================================================

TEXT_ERP_ORDER_NO = '[data-testid="text-erp-order-no"]'
TEXT_ORDER_STATUS = '[data-testid="text-order-status"]'
BTN_SUBMIT_REVIEW = '[data-testid="btn-submit-review"]'
#: 只有 `?submitted=1` 时才渲染。**不能只信它** —— 见 §9.3 的复核步骤。
MSG_SUCCESS = '[data-testid="msg-success"]'

# ============================================================
# 库存查询（只读）
# ============================================================

INVENTORY_TABLE = '[data-testid="table-inventory"]'


def qty_cell(sku: str) -> str:
    """某个 SKU 的库存单元格。"""
    return f'[data-testid="cell-qty-{sku}"]'


# ============================================================
# 状态文案 → 语义
# ============================================================

STATUS_DRAFT = "DRAFT"
STATUS_PENDING_REVIEW = "PENDING_REVIEW"
STATUS_APPROVED = "APPROVED"

#: 文案出自 `mock/erp/app/deps.py` 的 `status_label`。
#: **认不出的文案要报错，不要猜**：猜错会让「已通过」被当成「待审核」，
#: 于是 RPA 去点一个不存在的按钮，把一个数据问题变成一个诡异的超时。
STATUS_TEXT: dict[str, str] = {
    "草稿": STATUS_DRAFT,
    "待审核": STATUS_PENDING_REVIEW,
    "已通过": STATUS_APPROVED,
}


class UnknownStatusText(ValueError):
    """页面上出现了映射表里没有的状态文案。"""


def parse_status(text: str) -> str:
    """把页面上的中文状态翻译成语义值。"""
    normalized = text.strip()
    try:
        return STATUS_TEXT[normalized]
    except KeyError:
        raise UnknownStatusText(
            f"页面上出现了未知的订单状态文案：{normalized!r}。"
            f"已知的是 {list(STATUS_TEXT)} —— 要么 ERP 改了文案，"
            f"要么整个状态列的位置变了（§9.3 的按列序号定位最容易这样静默失效）。"
        ) from None
