"""统一响应外壳与分页约定 —— 见《API接口设计》§2。

所有接口都返回 `{code, message, data}`。前端只写一次响应拦截器：
看 `code` 判成败，从 `data` 取业务数据。代价是多一层嵌套，v1 接受。

HTTP 状态码表达「传输/鉴权层」结果，`code` 表达「业务层」结果，两者配合 ——
所以 404 的响应用的是 HTTP 404 而不是 200，别为了「统一」把状态码压平。
"""

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

OK_CODE = 0
OK_MESSAGE = "ok"


class ApiResponse(BaseModel, Generic[T]):
    """成功响应。失败响应由 main.py 的全局异常处理器构造，结构一致。"""

    code: int = Field(default=OK_CODE, description="0 表示成功，非 0 为业务错误码")
    message: str = Field(default=OK_MESSAGE, description="提示信息，成功时固定 ok")
    data: T | None = Field(default=None, description="业务数据")

    @classmethod
    def ok(cls, data: T | None = None) -> "ApiResponse[T]":
        return cls(data=data)


class Page(BaseModel, Generic[T]):
    """分页数据载荷，放进 `data` 里。见《API接口设计》§2.2。"""

    items: list[T]
    total: int = Field(description="符合条件的总条数，不是本页条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")


class PageParams:
    """分页查询依赖，用法：`params: PageParams = Depends()`。

    page_size 上限 100 是硬限制：不设上限的话，前端一个 `page_size=999999`
    就能把整张订单表拉进内存，这类问题在演示环境不会出现、上线后必炸。

    校验元数据写成 `Annotated[int, Query(...)]` 而不是 `page: int = Query(...)`：
    后者在**直接 `PageParams()` 构造时**（比如测试、脚本里）拿到的是 Query 对象
    而不是数字，直到某处对它做算术才炸。Annotated 写法让默认值就是真 int，
    FastAPI 仍然能从注解里读到校验规则，两边都对。
    """

    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
        page_size: Annotated[
            int, Query(ge=1, le=100, description="每页条数，最大 100")
        ] = 20,
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        """换算成 SQL 的 OFFSET。放在这里，避免每个 repository 各写一遍。"""
        return (self.page - 1) * self.page_size


# ============================================================
# 系统 / 健康检查（《API接口设计》§13.1）
# ============================================================


class HealthChecks(BaseModel):
    """每项依赖的检查结果：正常为 "ok"，异常为错误信息原文。

    保留错误原文而不是统一成 "error"，是因为排查时「连接被拒绝」和
    「认证失败」是两件事，丢了原文就得再去翻日志。
    """

    mysql: str
    redis: str
    ai: str


class HealthData(BaseModel):
    status: str = Field(description="healthy / degraded，任一依赖异常即为 degraded")
    checks: HealthChecks
    version: str
