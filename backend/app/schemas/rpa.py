"""RPA Worker 专用接口的请求 / 响应模型 —— 见《API接口设计》§10。

这组接口的调用方是跑在另一台机器上的 RPA Worker，不是浏览器。因此：

  · 返回的是「Worker 拿去就能干活」的完整数据，不省字段；
  · 失败一律走统一外壳（`{code, message, data}`），Worker 端解析只有一条路径；
  · 「没活干」不是错误，是 `data: null` —— 用 204 或 404 会让 Worker 端多一个分支。
"""

from pydantic import BaseModel, Field, model_validator

from app.schemas.task import TaskOrderBrief


# ============================================================
# §10.1 领取任务
# ============================================================


class ClaimRequest(BaseModel):
    worker_name: str = Field(
        min_length=1, max_length=64, description="Worker 标识，写入 tasks.claimed_by"
    )
    wait_seconds: int = Field(
        default=0,
        ge=0,
        le=30,
        description="长轮询等待秒数。0 表示立即返回；>0 时没活会挂起等，避免空转",
    )


class ClaimedTask(BaseModel):
    """任务本身。`attempt` 是**本次**是第几次尝试（= 已重试次数 + 1），
    Worker 用它在失败时回传截图（§10.4 的 `attempt` 参数）。"""

    id: int
    priority: str
    attempt: int
    need_review: bool


class ClaimInstruction(BaseModel):
    """服务端下发的操作指令。

    `erp_url` 由服务端给而不是 Worker 自己配：换环境（本机 / 容器 / 另一台机器）
    只改服务端一处，Worker 端不用重新配置。
    """

    erp_url: str
    action: str = "CREATE_ORDER"
    submit_for_review: bool = True


class ClaimData(BaseModel):
    task: ClaimedTask
    order: TaskOrderBrief
    instruction: ClaimInstruction


# ============================================================
# §10.2 心跳
# ============================================================


class HeartbeatRequest(BaseModel):
    worker_name: str = Field(min_length=1, max_length=64)


class HeartbeatData(BaseModel):
    task_id: int
    status: str = Field(description="服务端此刻认为任务处于什么状态")
    cancel: bool = Field(
        default=False,
        description="true 表示这活已经不归你了（被回收/取消/已结束），应立即停止当前执行",
    )


# ============================================================
# §10.3 回传结果
# ============================================================


class ResultRequest(BaseModel):
    """成败用一个布尔分流，不拆两个接口。

    Worker 失败时也要回传；拆成两个接口，它就得在调用前先判断该打哪个，
    多一处可能判错的地方，而且两种情况的响应体还要各解析一遍。
    """

    worker_name: str = Field(min_length=1, max_length=64)
    success: bool
    erp_order_no: str | None = Field(
        default=None, max_length=64, description="成功时必填：ERP 生成的单号，对账用"
    )
    error_code: str | None = Field(
        default=None, max_length=64, description="失败时的机器可读分类，如 ERP_LOGIN_FAILED"
    )
    error_message: str | None = Field(default=None, max_length=1000, description="给人看的失败详情")
    duration_ms: int | None = Field(default=None, ge=0, description="本次执行耗时")

    @model_validator(mode="after")
    def _require_erp_order_no_on_success(self) -> "ResultRequest":
        """成功必须带 ERP 单号。

        这不是形式主义：`task_executions.erp_order_no` 是主库与 ERP 之间
        **唯一**的对账凭据（见《模拟ERP设计》§11）。少了它，这条成功记录
        事后无法核实，只能算「Worker 说它成功了」。

        失败侧不强制 `error_code`：宁可收下一条信息不全的失败，也不要
        因为 422 把 Worker 的失败回传挡回去 —— 那会让任务卡在 RUNNING
        直到被判僵尸，问题被放大而不是被记录。
        """
        if self.success and not self.erp_order_no:
            raise ValueError("success=true 时必须提供 erp_order_no（对账需要）")
        return self


class ResultData(BaseModel):
    task_id: int
    status: str
    retry_scheduled: bool = False
    retry_count: int | None = Field(default=None, description="重试后的已重试次数")


# ============================================================
# §10.4 上传失败截图
# ============================================================


class ScreenshotData(BaseModel):
    path: str = Field(
        description="取图接口路径。**不是静态文件 URL** —— 截图含客户信息，"
        "必须带管理员 JWT 才取得到"
    )
