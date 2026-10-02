"""业务异常与错误码 —— 与《API接口设计》§3 一一对应。

设计：HTTP 状态码表达「传输/鉴权层」结果，ErrorCode 表达「业务层」结果，两者配合。
全局异常处理器（见 main.py）负责把这里的异常翻译成统一响应结构。
"""

from enum import IntEnum


class ErrorCode(IntEnum):
    OK = 0

    # 4xxx 客户端问题
    PARAM_ERROR = 4000
    UNAUTHENTICATED = 4001
    BAD_CREDENTIALS = 4002
    FORBIDDEN = 4003
    NOT_FOUND = 4004
    INVALID_STATE = 4009
    CONFLICT = 4010
    EXCEL_PARSE_ERROR = 4020

    # 5xxx 服务端问题
    INTERNAL_ERROR = 5000
    AI_SERVICE_ERROR = 5001
    QUEUE_UNAVAILABLE = 5002


class AppError(Exception):
    """所有业务异常的基类。

    子类通过覆盖 code / http_status / default_message 来声明自己的语义，
    调用方只需 `raise NotFoundError("任务不存在")`。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500
    default_message: str = "服务器内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.message)


# ---------- 4xx ----------


class ParamError(AppError):
    code = ErrorCode.PARAM_ERROR
    http_status = 400
    default_message = "参数校验失败"


class AuthError(AppError):
    code = ErrorCode.UNAUTHENTICATED
    http_status = 401
    default_message = "未认证或登录已过期"


class CredentialsError(AppError):
    """用户名或密码错误 —— 与 AuthError 区分，便于前端提示不同文案"""

    code = ErrorCode.BAD_CREDENTIALS
    http_status = 401
    default_message = "用户名或密码错误"


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN
    http_status = 403
    default_message = "无权限访问该资源"


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404
    default_message = "资源不存在"


class InvalidStateError(AppError):
    """当前状态不允许该操作 —— 本项目最常用的错误。

    状态机是业务的核心约束，任何违反状态流转的请求都必须被明确拒绝，
    而不是静默改数据。例如对 SUCCESS 的任务调 retry。
    """

    code = ErrorCode.INVALID_STATE
    http_status = 409
    default_message = "当前状态不允许该操作"


class ConflictError(AppError):
    """唯一键冲突，如订单号重复导入"""

    code = ErrorCode.CONFLICT
    http_status = 409
    default_message = "数据冲突"


class ExcelParseError(AppError):
    code = ErrorCode.EXCEL_PARSE_ERROR
    http_status = 422
    default_message = "Excel 文件解析失败"


# ---------- 5xx ----------


class AIServiceError(AppError):
    code = ErrorCode.AI_SERVICE_ERROR
    http_status = 502
    default_message = "AI 服务调用失败"


class QueueUnavailableError(AppError):
    code = ErrorCode.QUEUE_UNAVAILABLE
    http_status = 503
    default_message = "任务队列不可用"
