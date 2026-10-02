"""认证接口 —— 见《API接口设计》§5。"""

import logging

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas.auth import LoginRequest, TokenData, UserDetail
from app.schemas.common import ApiResponse
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/login",
    response_model=ApiResponse[TokenData],
    summary="登录",
    description="校验用户名口令，成功返回 JWT 并更新最后登录时间。",
)
async def login(body: LoginRequest, session: SessionDep) -> ApiResponse[TokenData]:
    token = await AuthService(session).login(body.username, body.password)
    return ApiResponse.ok(token)


@router.get(
    "/me",
    response_model=ApiResponse[UserDetail],
    summary="当前用户",
)
async def me(user: CurrentUser) -> ApiResponse[UserDetail]:
    return ApiResponse.ok(UserDetail.model_validate(user))


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    summary="登出",
    description="JWT 无状态，服务端不维护黑名单；此接口只写审计日志。",
)
async def logout(user: CurrentUser) -> ApiResponse[None]:
    # 这个接口不改变任何服务端状态 —— token 在过期前依然有效，
    # 「登出」实际上是前端把 token 丢掉。留着它是因为审计上需要
    # 「谁在什么时候点了登出」这条记录，也给了前端一个明确的调用点。
    logger.info("用户登出: id=%s username=%s", user.id, user.username)
    return ApiResponse.ok(None)
