"""主平台 `/api/v1/rpa/*` 的 HTTP 客户端。

Worker 与主平台之间**只有**这一条通道：不连 Redis、不连 MySQL
（《需求规格》§关键约束）。这条约束的价值不只是「解耦好看」——
它意味着把 Redis 换成 RabbitMQ/Kafka 的时候，这个文件一个字都不用改。

## 统一外壳

主平台所有接口都返回 `{code, message, data}`，成功时 `code=0`。
失败时 HTTP 状态码和 `code` 都有意义，但**只有 `code` 是稳定的**：
HTTP 状态由框架决定，业务 code 由 `main.py` 的异常处理器写死
（4000/4001/4003/4004/4009…）。所以这里只认 `code`，
HTTP 状态只用作日志和「是不是 401 该重登」的判断。

## 「没活干」不是错误

`claim` 在队列为空时返回 `data: null` 且 HTTP 200。这是刻意的设计
（《API接口设计》§10.1），所以这里也照它办：返回 `None`，
调用方一个 `if claim is None` 就够了，不必区分「没任务」和「出错了」。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from rpa.common.config import RpaSettings, settings as default_settings
from rpa.common.errors import ApiError, AuthRejected, RpaError

logger = logging.getLogger(__name__)

#: 单次请求的默认超时。claim 的长轮询会单独放宽（见 `claim`）。
_DEFAULT_TIMEOUT_SECONDS = 30.0


# ============================================================
# 响应模型 —— 字段与服务端 schemas/rpa.py 逐一对应
# ============================================================


class ClaimedTask(BaseModel):
    id: int
    priority: str
    attempt: int
    need_review: bool


class ClaimedOrder(BaseModel):
    id: int
    order_no: str
    customer_name: str
    phone: str
    address: str
    product_name: str
    sku: str
    quantity: int
    amount: str
    buyer_message: str | None = None


class ClaimInstruction(BaseModel):
    erp_url: str
    action: str = "CREATE_ORDER"
    submit_for_review: bool = True


class ClaimData(BaseModel):
    task: ClaimedTask
    order: ClaimedOrder
    instruction: ClaimInstruction


class HeartbeatData(BaseModel):
    task_id: int
    status: str
    cancel: bool = False


class ResultData(BaseModel):
    task_id: int
    status: str
    retry_scheduled: bool = False
    retry_count: int | None = None


class ScreenshotData(BaseModel):
    path: str = Field(description="带鉴权的取图路径，不是静态 URL")


# ============================================================
# 客户端
# ============================================================


class ApiClient:
    def __init__(
        self,
        settings: RpaSettings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """`client` 参数只为测试留口子。

        生产路径永远是「自己按配置建一个」；单测要换掉传输层时才会传进来。
        """
        self._settings = settings or default_settings
        self._client = client or httpx.Client(
            base_url=self._settings.api_base_url,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            # 本机可能配了系统级 HTTP 代理，httpx 默认 trust_env=True 会读它。
            # 主平台就在 localhost，走代理只会出怪问题。
            trust_env=False,
        )
        self._token: str | None = None

    # ---------- 生命周期 ----------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------- 鉴权 ----------

    def login(self) -> None:
        """登录取 JWT。

        Worker 用的是**独立账号**（role=WORKER），不是管理员的一个别名 ——
        它只能调 `/rpa/*`，调管理接口会被 403 挡掉（《API接口设计》§10 开头）。
        """
        if not self._settings.worker_username or not self._settings.worker_password:
            raise AuthRejected(
                "没有配置 WORKER_USERNAME / WORKER_PASSWORD，无法登录主平台"
            )

        # 登录本身不能再走 _request（它带 Authorization，且 401 时不该递归重登）。
        response = self._client.post(
            "/auth/login",
            json={
                "username": self._settings.worker_username,
                "password": self._settings.worker_password,
            },
        )
        data = self._unwrap(response)
        token = data.get("access_token")
        if not token:
            raise AuthRejected(f"登录响应里没有 access_token：{data!r}")
        self._token = token
        logger.info("已登录主平台：%s", self._settings.worker_username)

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise AuthRejected("尚未登录主平台，先调用 login()")
        return {"Authorization": f"Bearer {self._token}"}

    # ---------- 四个 /rpa/* 接口 ----------

    def claim(self, wait_seconds: int | None = None) -> ClaimData | None:
        """领一个任务。队列为空时返回 None（HTTP 200 + `data: null`）。"""
        if wait_seconds is None:
            wait_seconds = self._settings.rpa_poll_wait_seconds

        data = self._request(
            "POST",
            "/rpa/tasks/claim",
            json={
                "worker_name": self._settings.rpa_worker_name,
                "wait_seconds": wait_seconds,
            },
            # 长轮询：服务端最多挂 wait_seconds 秒。客户端读超时必须比它大，
            # 否则每次稍慢一点的任务都会被本地判成超时，白丢一次 claim。
            timeout=wait_seconds + 15.0,
        )
        if data is None:
            return None
        return ClaimData.model_validate(data)

    def heartbeat(self, task_id: int) -> HeartbeatData:
        """心跳。返回 `cancel=True` 表示这活已经不归本 Worker 了。

        服务端在「任务不存在」时**也回 200**（`status="MISSING"`），
        所以这里永远不会因为心跳而抛 404 —— 那正是设计意图：
        抛错会让 Worker 的自然反应变成「重试」，而这里要的是「立刻停手」。
        """
        data = self._request(
            "POST",
            f"/rpa/tasks/{task_id}/heartbeat",
            json={"worker_name": self._settings.rpa_worker_name},
        )
        return HeartbeatData.model_validate(data)

    def report_result(
        self,
        task_id: int,
        *,
        success: bool,
        erp_order_no: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
    ) -> ResultData:
        """回传结果。

        成功时 `erp_order_no` 是**必填**的（服务端有 model_validator 强校验）：
        它是主库与 ERP 之间唯一的对账凭据，少了它这条成功记录事后无法核实。
        """
        payload: dict[str, Any] = {
            "worker_name": self._settings.rpa_worker_name,
            "success": success,
        }
        if erp_order_no is not None:
            payload["erp_order_no"] = erp_order_no
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            # 服务端限制 1000 字符。超了会被 422 挡下来 —— 而失败回传被挡，
            # 任务就只能卡在 RUNNING 等僵尸回收，问题被放大而不是被记录。
            payload["error_message"] = error_message[:1000]
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        data = self._request("POST", f"/rpa/tasks/{task_id}/result", json=payload)
        return ResultData.model_validate(data)

    def upload_screenshot(self, task_id: int, attempt: int, png: bytes) -> ScreenshotData:
        """上传失败截图。

        **必须在回传结果之前调用**：截图接口要求该次 execution 还处于
        `RUNNING`，一旦结果回传完，这次执行就闭合了，附件没地方挂
        （《API接口设计》§10.4）。
        """
        data = self._request(
            "POST",
            f"/rpa/tasks/{task_id}/screenshot",
            files={"file": ("failure.png", png, "image/png")},
            data={"attempt": str(attempt)},
        )
        return ScreenshotData.model_validate(data)

    # ---------- 内部 ----------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """发请求 + 拆外壳。

        401 时自动重登一次再重试。JWT 有效期 720 分钟，正常跑不到过期；
        但长跑 Worker 跨越重启、或者管理员改了 Worker 口令，都可能遇到。
        加这一层是便宜的保险 —— 而且只重试一次，避免口令错了变成死循环。
        """
        response = self._client.request(method, path, headers=self._headers(), **kwargs)

        if response.status_code == 401:
            logger.warning("主平台返回 401，token 可能已失效，重新登录后重试一次")
            self._token = None
            self.login()
            response = self._client.request(method, path, headers=self._headers(), **kwargs)

        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        """把 `{code, message, data}` 拆开，非 0 一律抛 ApiError。

        特意不检查 HTTP 状态码来决定成败：业务失败也是 200 之外的码，
        但主平台的异常处理器保证了**失败响应体同样是完整外壳**，
        所以解析 `code` 一条路径就能覆盖所有情况；HTTP 状态留给日志。
        """
        try:
            body = response.json()
        except ValueError:
            raise RpaError(
                f"主平台返回了非 JSON 响应（HTTP {response.status_code}）："
                f"{response.text[:200]}"
            ) from None

        if not isinstance(body, dict) or "code" not in body:
            raise RpaError(
                f"主平台响应不符合统一外壳（HTTP {response.status_code}）：{str(body)[:200]}"
            )

        code = body.get("code", 0)
        if code != 0:
            raise ApiError(
                int(code),
                body.get("message") or f"主平台返回 code={code}",
                http_status=response.status_code,
            )
        return body.get("data")
