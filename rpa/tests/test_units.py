"""RPA Worker 的纯逻辑单测。

这一层**不碰浏览器、不连任何服务** —— 全部是「给定输入 → 断言输出」。
所以它秒级跑完，也能在任何机器上跑（CI 里没有 Chromium 也没关系）。

覆盖面刻意选在「改错了不会立刻报错、但会在生产里静默犯错」的地方：

| 区域 | 静默出错会怎样 |
| --- | --- |
| 选择器契约 | 状态列位置一改，复核读到别的单元格还不报错 |
| 状态文案映射 | 认不出的文案被猜成「待审核」，去点一个不存在的按钮 |
| 异常分类 | 可重试的被记成不可重试，或者反过来 |
| 幂等判定 | 重复下单 —— 两套系统里各留一笔，事后对不上账 |
| 结果组装顺序 | 截图晚于结果回传，附件永远挂不上 |
| 配置推导 | 连到错的主平台，且看起来一切正常 |

浏览器行为本身不在这里测 —— 那是 `-m e2e` 的事。这里测的是**编排的决策**。
"""

from __future__ import annotations

import json
import threading
import time
import types

import httpx
import pytest

from rpa.common import errors
from rpa.common.api_client import ApiClient, ClaimedOrder, ClaimInstruction
from rpa.common.config import RpaSettings
from rpa.erp import selectors as S
from rpa.inventory_sync.check_stock_flow import StockCheck
from rpa.main import Heartbeat, Outcome, _report, execute_one
from rpa.order_sync import create_order_flow as flow

pytestmark = pytest.mark.unit


# ============================================================
# 1. 选择器契约
# ============================================================


def test_status_column_is_the_seventh() -> None:
    """§9.3 例外二：状态列没有 testid，只能按列序号取。

    这条断言把「0-based 下标 6」和「CSS nth-child(7)」这两个数字**钉在一起**。
    它们之间的关系（+1）在代码里是个 `+ 1`，很容易在重构时被抹掉 ——
    而抹掉之后不会报错，只会去读第 6 列。

    这个测试故意写成「两个常量之间的关系」而不是「等于 7」：改列序号是
    合法的（ERP 加了一列），但必须同时改对 nth-child 的算法。
    """
    assert S.STATUS_COLUMN_CELL == f"td:nth-child({S.STATUS_COLUMN_INDEX + 1})"
    assert S.STATUS_COLUMN_CELL == "td:nth-child(7)"
    assert S.ERP_ORDER_NO_COLUMN_CELL == "td:nth-child(1)"
    assert S.SOURCE_ORDER_NO_COLUMN_CELL == "td:nth-child(2)"


def test_nav_orders_has_no_testid() -> None:
    """§9.3 例外一：导航入口**刻意**没有 testid。

    如果哪天有人「顺手」给它加个 testid 并改掉这个常量，这个断言会红 ——
    那正是我们要的：这处非 testid 定位是设计的一部分，不是遗漏。
    """
    assert S.NAV_ORDERS == 'a[href="/orders"]'
    assert "data-testid" not in S.NAV_ORDERS


def test_form_fields_match_the_order_model() -> None:
    """表单字段表必须与 `_form_values` 的输出完全对齐。

    这是流程层（知道「填什么值」）和页面层（知道「填到哪个框」）之间的接口。
    少一个键就是「这个字段静默没填」，ERP 那边可能照样保存成功。
    """
    order = ClaimedOrder(
        id=1, order_no="A-1", customer_name="张三", phone="13800000000",
        address="某地", product_name="某商品", sku="SKU-001",
        quantity=3, amount="99.00",
    )
    assert set(flow._form_values(order)) == set(S.FORM_FIELDS)


def test_source_order_no_is_the_reconciliation_key() -> None:
    """主平台的 `order_no` 到 ERP 这边叫 `source_order_no`。

    名字不一样不是笔误 —— 它是两套系统之间**唯一**的关联点（没有外键），
    所以这个映射值得钉住。
    """
    order = ClaimedOrder(
        id=1, order_no="MOCK-0001", customer_name="李四", phone="13900000000",
        address="某地", product_name="某商品", sku="SKU-002",
        quantity=2, amount="10.00",
    )
    assert flow._form_values(order)["source_order_no"] == "MOCK-0001"
    # 数量要转成字符串 —— 表单填的是文本，直接把 int 传下去会在 fill 里报错。
    assert flow._form_values(order)["quantity"] == "2"


# ============================================================
# 2. 状态文案映射
# ============================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [("草稿", S.STATUS_DRAFT), ("待审核", S.STATUS_PENDING_REVIEW), ("已通过", S.STATUS_APPROVED)],
)
def test_parse_status_known_texts(text: str, expected: str) -> None:
    assert S.parse_status(text) == expected


def test_parse_status_is_whitespace_tolerant() -> None:
    """页面文本常带换行/缩进，`inner_text()` 已经 strip 过，这里再兜一层。"""
    assert S.parse_status("  待审核\n") == S.STATUS_PENDING_REVIEW


@pytest.mark.parametrize("text", ["审核中", "已完成", "", "PENDING_REVIEW"])
def test_parse_status_refuses_to_guess(text: str) -> None:
    """认不出的文案**必须报错**，不能猜。

    猜错的后果是「已通过」被当成「待审核」，于是 RPA 去点一个不存在的按钮，
    把一个数据问题变成一个诡异的超时。报错至少指向了正确的地方。
    """
    with pytest.raises(S.UnknownStatusText) as excinfo:
        S.parse_status(text)
    # 错误信息里要有「已知的有哪些」，否则排查时还得翻源码。
    assert "草稿" in str(excinfo.value)


# ============================================================
# 3. 异常分类
# ============================================================


@pytest.mark.parametrize(
    ("cls", "code", "permanent"),
    [
        (errors.LoginFailed, "ERP_LOGIN_FAILED", True),
        (errors.ErpUnavailable, "ERP_UNAVAILABLE", False),
        (errors.PageLoadTimeout, "ERP_PAGE_TIMEOUT", False),
        (errors.FormValidationRejected, "ERP_VALIDATION_REJECTED", True),
        (errors.ErpServerBusy, "ERP_SERVER_BUSY", False),
        (errors.ResultReadFailed, "ERP_RESULT_READ_FAILED", False),
        (errors.AuthRejected, "PLATFORM_AUTH_FAILED", True),
    ],
)
def test_error_codes_and_permanence(cls: type, code: str, permanent: bool) -> None:
    """每个异常类的 `code` / `permanent` 都要对得上 —— 这张表就是回传给主平台的字典。"""
    exc = cls("出事了")
    assert exc.code == code
    assert exc.permanent is permanent
    assert exc.message == "出事了"


def test_api_error_encodes_the_platform_code() -> None:
    """主平台的业务 code 要带进 Worker 的 error_code 里，否则排查时丢了线索。"""
    exc = errors.ApiError(4009, "任务不归你管", http_status=409)
    assert exc.code == "PLATFORM_4009"
    assert exc.code_value == 4009
    assert exc.http_status == 409


def test_task_cancelled_is_not_an_rpa_error() -> None:
    """取消**刻意**不继承 RpaError。

    因为它的处理方式完全不同：RpaError 会被回传成一次失败，而取消什么都不回传
    （任务已经归别人了，回传只会拿 409）。用类型把这个区别表达出来，
    比在编排里 `if isinstance(...)` 或者靠一个布尔标志可靠。
    """
    assert not issubclass(errors.TaskCancelled, errors.RpaError)
    assert not isinstance(errors.TaskCancelled(), errors.RpaError)


# ============================================================
# 4. 写操作的响应守卫
# ============================================================


def _response(status: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(status=status)


def test_guard_maps_503_to_retryable_busy() -> None:
    """503 是模拟 ERP 的故障注入（MOCK_ERP_FAIL_RATE），专门用来演练重试。"""
    with pytest.raises(errors.ErpServerBusy):
        flow._guard_write_response(_response(503), "保存订单")


@pytest.mark.parametrize("status", [500, 502, 504])
def test_guard_maps_other_5xx_to_unavailable(status: int) -> None:
    with pytest.raises(errors.ErpUnavailable):
        flow._guard_write_response(_response(status), "保存订单")


@pytest.mark.parametrize("status", [200, 303, 400, 422])
def test_guard_lets_non_5xx_through(status: int) -> None:
    """**4xx 刻意不在这里报**：400 的响应体是重新渲染的表单页，
    上面有具体的中文原因（「库存不足，当前可用：0」）。在这里抛异常的话，
    回传给管理员的就只剩下一句「HTTP 400」，最有价值的那句话被丢掉了。
    所以 4xx 交给调用方读 `msg-form-error`。
    """
    flow._guard_write_response(_response(status), "保存订单")


# ============================================================
# 5. 幂等判定
# ============================================================


class _Row:
    """占位：`_handle_already_recorded` 只是把它原样传给 `erp_order_no_of`。"""


class _Detail:
    """详情页替身：只实现幂等分支真正会调的那几个方法。"""

    def __init__(self, has_submit: bool, verify_orders: "_VerifyOrders | None" = None) -> None:
        self._has_submit = has_submit
        self._verify_orders = verify_orders
        self.submit_calls = 0

    def has_submit_button(self) -> bool:
        return self._has_submit

    def submit_for_review_and_confirm(self):
        self.submit_calls += 1
        return _response(200)

    def open_orders_list(self) -> "_VerifyOrders":
        assert self._verify_orders is not None
        return self._verify_orders


class _VerifyOrders:
    """列表页替身：复核那一步用它读回状态。"""

    def __init__(self, status: str) -> None:
        self._status = status

    def search(self, keyword: str) -> None:
        return None

    def find_by_source_no(self, source_order_no: str) -> "_Row":
        return _Row()

    def status_of(self, row: "_Row") -> str:
        return self._status


class _Orders:
    """`_handle_already_recorded` 拿到的列表页替身。"""

    def __init__(self, erp_no: str, detail: _Detail) -> None:
        self._erp_no = erp_no
        self._detail = detail
        self.opened: list[str] = []

    def erp_order_no_of(self, row: _Row) -> str:
        return self._erp_no

    def open_detail(self, erp_no: str) -> _Detail:
        self.opened.append(erp_no)
        return self._detail


def _claim_order(order_no: str) -> ClaimedOrder:
    return ClaimedOrder(
        id=1, order_no=order_no, customer_name="王五", phone="13700000000",
        address="某地", product_name="某商品", sku="SKU-001",
        quantity=1, amount="5.00",
    )


def _instruction(submit: bool) -> ClaimInstruction:
    return ClaimInstruction(erp_url="http://erp.local", submit_for_review=submit)


def test_idempotent_hit_submits_only_when_the_button_is_there() -> None:
    """命中幂等时的判定依据是**按钮的有无**，不是状态文案。

    服务端只在 DRAFT 时渲染「提交审核」（《模拟ERP设计》§4.6），
    所以按钮还在 = 上一轮只录了单没提交，补上这一步。
    """
    detail = _Detail(has_submit=True, verify_orders=_VerifyOrders(S.STATUS_PENDING_REVIEW))
    orders = _Orders("ERP-1", detail)

    got = flow._handle_already_recorded(
        orders, _Row(), _instruction(submit=True), _claim_order("A-1"), check=lambda: None
    )

    assert got == "ERP-1"
    assert orders.opened == ["ERP-1"]
    assert detail.submit_calls == 1


def test_idempotent_hit_does_not_resubmit_when_already_done() -> None:
    """按钮不在 = 已经提交过了。此时**再点一次会是错的**（页面上也没得点）。"""
    detail = _Detail(has_submit=False, verify_orders=_VerifyOrders(S.STATUS_PENDING_REVIEW))
    orders = _Orders("ERP-2", detail)

    got = flow._handle_already_recorded(
        orders, _Row(), _instruction(submit=True), _claim_order("A-2"), check=lambda: None
    )

    assert got == "ERP-2"
    assert detail.submit_calls == 0


def test_idempotent_hit_skips_submit_when_not_required() -> None:
    """指令没要求提交审核时，命中也只把已有单号读回来，什么都不点。"""
    detail = _Detail(has_submit=True)
    orders = _Orders("ERP-3", detail)

    got = flow._handle_already_recorded(
        orders, _Row(), _instruction(submit=False), _claim_order("A-3"), check=lambda: None
    )

    assert got == "ERP-3"
    assert detail.submit_calls == 0


def test_verify_advanced_rejects_still_draft() -> None:
    """复核发现状态还是「草稿」→ 提交**没有生效**，必须报错而不是当成功。

    这条最重要：它是「不信横幅、去读真实状态」这个怀疑精神的落点。
    如果这里不报错，一笔根本没提交的单会被回传成 SUCCESS。
    """
    detail = _Detail(has_submit=False, verify_orders=_VerifyOrders(S.STATUS_DRAFT))
    with pytest.raises(errors.ResultReadFailed) as excinfo:
        flow._verify_advanced(detail, "A-4", check=lambda: None)
    assert "草稿" in str(excinfo.value)


def test_verify_advanced_fails_when_the_row_vanished() -> None:
    """复核时列表里找不到那笔单 —— 同样不能当成功。"""

    class _Missing(_VerifyOrders):
        def find_by_source_no(self, source_order_no: str):
            return None

    detail = _Detail(has_submit=False, verify_orders=_Missing(S.STATUS_PENDING_REVIEW))
    with pytest.raises(errors.ResultReadFailed):
        flow._verify_advanced(detail, "A-5", check=lambda: None)


# ============================================================
# 6. 纯逻辑：库存判断
# ============================================================


@pytest.mark.parametrize(
    ("available", "required", "sufficient"),
    [(50, 1, True), (8, 8, True), (0, 1, False), (7, 8, False)],
)
def test_stock_check_boundary(available: int, required: int, sufficient: bool) -> None:
    """边界就是边界：`可用 == 需要` 算够。差一个就不够。"""
    check = StockCheck(sku="SKU-001", available=available, required=required)
    assert check.sufficient is sufficient


# ============================================================
# 7. 配置推导
# ============================================================


def test_api_base_url_derives_from_host_and_port() -> None:
    """留空 RPA_API_BASE_URL 时按 APP_HOST/APP_PORT 推导，本地开发不用填两遍。"""
    settings = RpaSettings(
        rpa_api_base_url="", app_host="10.0.0.5", app_port=9000, _env_file=None
    )
    assert settings.api_base_url == "http://10.0.0.5:9000/api/v1"


def test_api_base_url_strips_trailing_slash() -> None:
    """多一个斜杠就会拼出 `//rpa/tasks/claim`，有些网关会当成不同路由。"""
    settings = RpaSettings(rpa_api_base_url="https://api.example.com/api/v1/", _env_file=None)
    assert settings.api_base_url == "https://api.example.com/api/v1"


@pytest.mark.parametrize("value", [-1, 31, 60])
def test_poll_wait_is_capped_by_the_server(value: int) -> None:
    """服务端把 wait_seconds 限制在 0~30，超了直接 400。

    在配置阶段拦下来，比等第一次 claim 收到 400 再回头查配置快得多。
    """
    with pytest.raises(ValueError, match="RPA_POLL_WAIT_SECONDS"):
        RpaSettings(rpa_poll_wait_seconds=value, _env_file=None)


@pytest.mark.parametrize("value", [0, -5])
def test_heartbeat_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError, match="RPA_HEARTBEAT_SECONDS"):
        RpaSettings(rpa_heartbeat_seconds=value, _env_file=None)


# ============================================================
# 8. `_unwrap` / `report_result` 的外壳处理
# ============================================================


def _fake_response(payload: object, status_code: int = 200, raw: str | None = None):
    return types.SimpleNamespace(
        status_code=status_code,
        text=raw if raw is not None else json.dumps(payload),
        json=lambda: payload,
    )


def test_unwrap_returns_data_on_code_zero() -> None:
    assert ApiClient._unwrap(_fake_response({"code": 0, "message": "ok", "data": {"a": 1}})) == {
        "a": 1
    }


def test_unwrap_raises_api_error_on_business_code() -> None:
    with pytest.raises(errors.ApiError) as excinfo:
        ApiClient._unwrap(
            _fake_response({"code": 4009, "message": "任务不归你管"}, status_code=409)
        )
    assert excinfo.value.code == "PLATFORM_4009"
    assert excinfo.value.message == "任务不归你管"
    assert excinfo.value.http_status == 409


def test_unwrap_rejects_non_json() -> None:
    """主平台挂掉时反代可能返回一整页 HTML —— 报错要说清是「不是 JSON」，
    而不是抛一个 `JSONDecodeError` 让人以为解析逻辑有问题。"""

    def _not_json():
        raise ValueError("no json")

    response = types.SimpleNamespace(
        status_code=502, text="<html>bad gateway</html>", json=_not_json
    )
    with pytest.raises(errors.RpaError, match="非 JSON"):
        ApiClient._unwrap(response)


def test_unwrap_rejects_a_body_without_the_envelope() -> None:
    with pytest.raises(errors.RpaError, match="统一外壳"):
        ApiClient._unwrap(_fake_response({"access_token": "t"}))


def _settings_with_worker() -> RpaSettings:
    return RpaSettings(
        worker_username="w", worker_password="p",
        rpa_api_base_url="http://platform.test/api/v1", _env_file=None,
    )


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://platform.test/api/v1",
        trust_env=False,
    )


def test_report_result_truncates_a_long_error_message() -> None:
    """服务端限 1000 字符，超了会 **422** —— 而失败回传被 422 挡下来，
    任务就只能卡在 RUNNING 等僵尸回收。所以截断必须发生在客户端。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "T"}})
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"code": 0, "data": {"task_id": 7, "status": "QUEUED", "retry_scheduled": True}}
        )

    client = ApiClient(_settings_with_worker(), client=_mock_client(handler))
    client.login()
    client.report_result(7, success=False, error_code="X", error_message="啊" * 5000)

    assert len(captured["body"]["error_message"]) == 1000


def test_report_result_omits_absent_fields() -> None:
    """没给的可选字段不要塞 None 进 JSON —— 服务端的 model_validator 会因为
    「success=True 但 erp_order_no 是 None」而拒掉整个请求。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "T"}})
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"code": 0, "data": {"task_id": 7, "status": "SUCCESS"}}
        )

    client = ApiClient(_settings_with_worker(), client=_mock_client(handler))
    client.login()
    client.report_result(7, success=True, erp_order_no="ERP-9", duration_ms=1234)

    body = captured["body"]
    assert body["erp_order_no"] == "ERP-9"
    assert body["duration_ms"] == 1234
    assert "error_code" not in body
    assert "error_message" not in body


def test_claim_returns_none_when_the_queue_is_empty() -> None:
    """「没活干」不是错误：HTTP 200 + `data: null`。调用方一个 `if claim is None` 就够。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "T"}})
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": None})

    client = ApiClient(_settings_with_worker(), client=_mock_client(handler))
    client.login()
    assert client.claim(wait_seconds=0) is None


def test_claim_parses_the_full_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "T"}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "task": {"id": 42, "priority": "HIGH", "attempt": 2, "need_review": False},
                    "order": {
                        "id": 7, "order_no": "M-1", "customer_name": "张三",
                        "phone": "13800000000", "address": "某地", "product_name": "某商品",
                        "sku": "SKU-001", "quantity": 1, "amount": "9.90",
                    },
                    "instruction": {"erp_url": "http://erp.local/", "action": "CREATE_ORDER"},
                },
            },
        )

    client = ApiClient(_settings_with_worker(), client=_mock_client(handler))
    client.login()
    claim = client.claim(wait_seconds=0)

    assert claim is not None
    assert claim.task.id == 42
    assert claim.task.attempt == 2
    assert claim.order.order_no == "M-1"
    # submit_for_review 缺省必须是 True：v1 的默认行为是录完就提交审核，
    # 让「服务端忘了下发这个字段」变成「少做一步」比反过来安全。
    assert claim.instruction.submit_for_review is True


# ============================================================
# 9. 结果组装：截图必须先于结果
# ============================================================


class _RecordingClient:
    """只记调用顺序的客户端替身。"""

    def __init__(self, fail_screenshot: bool = False) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._fail_screenshot = fail_screenshot

    def upload_screenshot(self, task_id: int, attempt: int, png: bytes) -> None:
        self.calls.append(("screenshot", (task_id, attempt, len(png))))
        if self._fail_screenshot:
            raise errors.ErpServerBusy("传图失败")

    def report_result(self, task_id: int, **kwargs) -> None:
        self.calls.append(("result", (task_id, kwargs)))


def test_screenshot_is_uploaded_before_the_result() -> None:
    """顺序不能反：截图接口要求该次 execution 还在 RUNNING，
    结果一回传这次执行就闭合了，附件没地方挂。"""
    client = _RecordingClient()
    outcome = Outcome(success=False, error_code="X", error_message="坏了",
                      screenshot_png=b"PNGDATA")

    _report(client, task_id=1, attempt=1, outcome=outcome)

    assert [name for name, _ in client.calls] == ["screenshot", "result"]


def test_no_screenshot_means_no_upload_call() -> None:
    """成功的那次执行**不该**传图 —— 别在成功路径上多打一次接口。"""
    client = _RecordingClient()
    _report(client, task_id=1, attempt=1, outcome=Outcome(success=True, erp_order_no="ERP-1"))
    assert [name for name, _ in client.calls] == ["result"]


def test_a_failed_screenshot_upload_does_not_block_the_result() -> None:
    """传附件失败不该把「回传失败原因」变成「回传不出去」：
    管理员最需要的是 error_code/error_message，截图是锦上添花。"""
    client = _RecordingClient(fail_screenshot=True)
    _report(
        client, task_id=1, attempt=1,
        outcome=Outcome(success=False, error_code="X", error_message="坏了",
                        screenshot_png=b"PNGDATA"),
    )
    assert [name for name, _ in client.calls] == ["screenshot", "result"]


# ============================================================
# 10. `execute_one`：把流程的异常翻成结论
# ============================================================


class _StubBrowser:
    def __init__(self, screenshot: bytes | None = b"PNG") -> None:
        self.page = object()
        self._screenshot = screenshot
        self.logged_in_with: str | None = None

    def ensure_logged_in(self, erp_url: str) -> None:
        self.logged_in_with = erp_url

    def screenshot(self, *, full_page: bool = True) -> bytes | None:
        return self._screenshot


def _claim_data():
    from rpa.common.api_client import ClaimData, ClaimedTask

    return ClaimData(
        task=ClaimedTask(id=9, priority="MEDIUM", attempt=2, need_review=False),
        order=_claim_order("A-9"),
        instruction=_instruction(submit=True),
    )


def _run_execute_one(monkeypatch, raiser, *, screenshot: bytes | None = b"PNG"):
    def fake_execute_order(page, settings, instruction, order, *, should_stop):
        if raiser is not None:
            raise raiser
        return "ERP-OK"

    monkeypatch.setattr(flow, "execute_order", fake_execute_order)
    import rpa.main as main_module

    monkeypatch.setattr(main_module, "execute_order", fake_execute_order)
    browser = _StubBrowser(screenshot)
    client = _RecordingClient()
    outcome = execute_one(client, browser, RpaSettings(_env_file=None), _claim_data())
    return outcome, browser


def test_execute_one_reports_a_typed_failure_with_a_screenshot(monkeypatch) -> None:
    outcome, browser = _run_execute_one(
        monkeypatch, errors.FormValidationRejected("库存不足，当前可用：0")
    )

    assert outcome is not None
    assert outcome.success is False
    assert outcome.error_code == "ERP_VALIDATION_REJECTED"
    # 管理员在后台看到的就是 ERP 页面上那句话的原文。
    assert outcome.error_message == "库存不足，当前可用：0"
    assert outcome.screenshot_png == b"PNG"
    # 截图必须发生在**失败现场**，也就是流程抛异常之后、浏览器还没动的时候。
    assert browser.logged_in_with == "http://erp.local"
    assert outcome.duration_ms >= 0


def test_execute_one_maps_an_unexpected_error_to_worker_unexpected(monkeypatch) -> None:
    outcome, _ = _run_execute_one(monkeypatch, RuntimeError("谁能想到"))

    assert outcome is not None
    assert outcome.success is False
    assert outcome.error_code == errors.UNEXPECTED_CODE
    assert "RuntimeError" in (outcome.error_message or "")


def test_execute_one_returns_none_on_cancellation(monkeypatch) -> None:
    """取消时返回 None = 什么都不回传。

    任务已经归别人了，回传只会拿到 409 `4009`，把一次正常的状态变更
    记成一条 Worker 报错 —— 反而污染日志。
    """
    outcome, _ = _run_execute_one(monkeypatch, errors.TaskCancelled())
    assert outcome is None


def test_execute_one_reports_success_with_the_erp_order_no(monkeypatch) -> None:
    outcome, _ = _run_execute_one(monkeypatch, None)

    assert outcome is not None
    assert outcome.success is True
    assert outcome.erp_order_no == "ERP-OK"
    assert outcome.error_code is None


# ============================================================
# 11. 心跳线程
# ============================================================


class _HeartbeatClient:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.calls = 0

    def heartbeat(self, task_id: int):
        self.calls += 1
        nxt = self._results.pop(0) if self._results else None
        if isinstance(nxt, Exception):
            raise nxt
        from rpa.common.api_client import HeartbeatData

        return HeartbeatData(task_id=task_id, status="RUNNING", cancel=bool(nxt))


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_heartbeat_sets_cancel_when_the_server_says_so() -> None:
    cancel_event = threading.Event()
    client = _HeartbeatClient([True])
    hb = Heartbeat(client, 5, interval_seconds=0.05, cancel_event=cancel_event)

    hb.start()
    assert _wait_for(cancel_event.is_set)
    hb.stop()
    assert client.calls >= 1


def test_heartbeat_survives_a_network_error() -> None:
    """心跳自身的网络错误只记日志、不中断执行 —— 真的一直失败，
    服务端的僵尸回收会把任务捞回去，那才是设计好的兜底。

    这里让第一次心跳抛错、第二次说 cancel，用来证明线程**没被异常打死**。
    """
    cancel_event = threading.Event()
    client = _HeartbeatClient([errors.ErpUnavailable("连不上"), True])
    hb = Heartbeat(client, 5, interval_seconds=0.05, cancel_event=cancel_event)

    hb.start()
    assert _wait_for(cancel_event.is_set)
    hb.stop()
    assert client.calls >= 2


def test_heartbeat_stop_is_prompt() -> None:
    """停止不能等满一个间隔 —— 那会让每个任务多花几十秒才收工。

    线程里等的是 `Event.wait(interval)`，stop() 一 set 它就醒了。
    """
    client = _HeartbeatClient([])
    hb = Heartbeat(client, 5, interval_seconds=30, cancel_event=threading.Event())
    hb.start()
    started = time.monotonic()
    hb.stop()
    assert time.monotonic() - started < 2.0
