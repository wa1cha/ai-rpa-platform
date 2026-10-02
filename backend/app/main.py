"""FastAPI 应用装配 —— 唯一创建 app 的地方。

这里做四件事，顺序即依赖顺序：
  1. 配日志（在中间件之前，保证中间件的日志有出口）
  2. 建 app + lifespan（管连接的生与死）
  3. 挂全局异常处理器（把任何异常翻译成统一响应外壳）
  4. 挂请求日志中间件 + 路由
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import RequestLoggingMiddleware, configure_logging
from app.database.mysql import dispose_engine
from app.database.redis import close_redis

configure_logging(logging.DEBUG if settings.debug else logging.INFO)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "启动 %s v%s [env=%s]", settings.app_name, settings.app_version, settings.app_env
    )
    yield
    # 优雅关闭：不关连接池的话，进程退出时 MySQL 会留下大量 TIME_WAIT 连接。
    await dispose_engine()
    await close_redis()
    logger.info("已释放 MySQL 与 Redis 连接池")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI + RPA 电商订单智能自动化平台 · 后端 API",
    lifespan=lifespan,
)


# ============================================================
# 统一响应外壳 —— 见《API接口设计》§2.3
# ============================================================


def _error_body(code: int, message: str) -> dict:
    """失败响应固定 `data: null`，前端拦截器只需判断 code 是否为 0。"""
    return {"code": int(code), "message": message, "data": None}


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """业务异常：状态码与错误码都来自异常类自身。

    这一类是**预期内**的失败（状态不允许、资源不存在……），
    用 INFO 记录即可；用 ERROR 会让真正的问题淹没在噪音里。
    """
    logger.info(
        "业务异常 %s %s -> code=%s http=%s msg=%s",
        request.method,
        request.url.path,
        int(exc.code),
        exc.http_status,
        exc.message,
    )
    return JSONResponse(status_code=exc.http_status, content=_error_body(exc.code, exc.message))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """参数校验失败。

    把 Pydantic 的错误列表压成一行塞进 message —— 契约里失败响应的
    `data` 固定为 null，所以不能把结构化错误放进 data，否则前端拦截器
    要写两套解析逻辑。字段名和原因保留在 message 里，够定位问题。
    """
    details = "; ".join(
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
        for err in exc.errors()
    )
    message = f"参数校验失败：{details}" if details else "参数校验失败"
    logger.info("参数校验失败 %s %s -> %s", request.method, request.url.path, message)
    return JSONResponse(
        status_code=400, content=_error_body(ErrorCode.PARAM_ERROR, message)
    )


#: 未显式抛 AppError 的 HTTPException（如未知路由 404、方法不允许 405）按状态码兜底映射，
#: 否则 FastAPI 会返回 `{"detail": "Not Found"}`，破坏统一外壳的约定。
_HTTP_STATUS_TO_CODE = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
}


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code)
    if code is None:
        code = ErrorCode.PARAM_ERROR if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, str(exc.detail)),
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何没被上面接住的异常。

    对外只回「服务器内部错误」，**绝不把 traceback 或异常原文返回给客户端**
    —— 那会泄露文件路径、SQL 片段等信息。真正的现场留在服务端日志里。
    """
    logger.exception(
        "未捕获异常 %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content=_error_body(ErrorCode.INTERNAL_ERROR, "服务器内部错误"),
    )


# ============================================================
# 中间件与路由
# ============================================================

# 注意：Starlette 的中间件是「后添加的先执行」，
# 这里只有一层，顺序问题留到接入 CORS（Phase 4）时再关心。
app.add_middleware(RequestLoggingMiddleware)

app.include_router(api_router, prefix=settings.api_v1_prefix)
