"""Worker 的异常层级与 `error_code` 分类。

回传给主平台的 `error_code` 是一个**机器可读**的分类，它要能回答两个问题：
「是哪一步坏的」和「再跑一次有没有可能不一样」。所以每个异常类都带 `code`，
以及一个 `permanent` 标记。

关于 `permanent`：**它不改变控制流**。v1 的接口没有「不可重试」这个信号，
服务端收到失败一律按 `retry_count < max_retry` 重试（《API接口设计》§10.3）。
`permanent` 只用于日志 —— 打一条 warn 说明「这次失败重试也不会有不同结果，
但服务端仍会重试 3 次」。这是刻意选的：与其改一个已经冻结的接口契约，
不如把局限写明白。详见 README 的「已知局限」。
"""

from __future__ import annotations


class RpaError(Exception):
    """Worker 侧所有「该回传失败」的异常基类。

    `message` 会原样进 `task_executions.error_message`，所以它应该是
    **给管理员看的一句话**，不是栈信息。技术细节走日志。
    """

    code: str = "RPA_ERROR"
    permanent: bool = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class LoginFailed(RpaError):
    """登录模拟 ERP 失败。

    注意登录失败在模拟 ERP 里**不是** HTTP 401，而是一个 200 的登录页
    加上「用户名或密码错误」。所以这个异常只能由「登录后没跳到
    /dashboard」来触发，不能靠状态码判断。
    """

    code = "ERP_LOGIN_FAILED"
    permanent = True


class ErpUnavailable(RpaError):
    """连不上模拟 ERP，或导航整体超时。多半是服务没起、端口填错。"""

    code = "ERP_UNAVAILABLE"
    permanent = False


class PageLoadTimeout(RpaError):
    """页面打开了，但某个该出现的元素一直没出现。

    这是 RPA 最常见的失败：页面结构变了、或者接口慢过了超时。
    再跑一次**有**可能成功，所以是可重试的。
    """

    code = "ERP_PAGE_TIMEOUT"
    permanent = False


class FormValidationRejected(RpaError):
    """ERP 服务端明确拒绝了这次录入（HTTP 400 + 页面上的中文原因）。

    典型是「库存不足，当前可用：0」「商品编码不存在」「手机号格式不正确」。
    这类失败重试 3 次结果完全一样 —— 数据没变，规则也没变。

    `message` 直接取页面上 `msg-form-error` 的原文，因为那句话就是
    管理员最该看到的东西；我们不去二次加工它。
    """

    code = "ERP_VALIDATION_REJECTED"
    permanent = True


class ErpServerBusy(RpaError):
    """ERP 返回 503「系统繁忙，请稍后重试」。

    这是模拟 ERP 的**故障注入**（`MOCK_ERP_FAIL_RATE`）产生的，专门用来
    演练重试链路，所以它一定是可重试的。
    """

    code = "ERP_SERVER_BUSY"
    permanent = False


class ResultReadFailed(RpaError):
    """单号读不出来 / 成功横幅没出现。

    走到这一步说明单子**可能已经录进去了**，只是我们没能确认。
    这种情况下一次执行会先走幂等预检，把已存在的那条读回来 —— 这正是
    为什么幂等预检要放在每次执行的最前面（《模拟ERP设计》§7.2）。
    """

    code = "ERP_RESULT_READ_FAILED"
    permanent = False


class AuthRejected(RpaError):
    """主平台拒绝了 Worker 的凭据或身份（登录失败 / 409 不归你管）。"""

    code = "PLATFORM_AUTH_FAILED"
    permanent = True


class ApiError(RpaError):
    """主平台返回了非 0 的业务 code（`{code, message, data}` 外壳里的 code）。"""

    def __init__(self, code_value: int, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message, code=f"PLATFORM_{code_value}")
        self.code_value = code_value
        self.http_status = http_status


class TaskCancelled(Exception):
    """这活已经不属于本 Worker 了（被回收 / 已结束 / 被取消）。

    **刻意不继承 RpaError**：它不是「失败」，而是一个不该回传结果的信号。
    任务已经被服务端转手或终结，再回传只会拿到 409 `4009`
    （`rpa_service.py` 会以「状态不是 RUNNING 或 claimed_by 对不上」拒绝），
    把一次正常的状态变更记成一条 Worker 报错，反而污染日志。
    """


#: 兜底：任何没被上面覆盖的异常都用它。用 `RpaError` 的默认 code 会导致
#: 所有未知错误长得一样，所以单列一个更明确的名字。
UNEXPECTED_CODE = "WORKER_UNEXPECTED"
