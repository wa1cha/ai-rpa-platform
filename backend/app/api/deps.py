"""鉴权依赖 —— 所有需要登录的接口都从这里拿「当前用户」。

放在 api 层而不是 core，是因为它依赖 FastAPI 的 `Depends` 机制；
`core/security.py` 只负责「解析 token」，不关心它是从 HTTP 头来的还是别处来的。
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio.client import Redis as RedisClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthError, ForbiddenError
from app.core.security import decode_access_token
from app.database.mysql import get_db
from app.database.redis import get_redis
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.queue_service import QueueService

#: `auto_error=False` 是刻意的：默认行为是「没带头就 403」，
#: 但**未登录应该是 401** —— 403 的语义是「你身份已知但没权限」，
#: 前端据此跳登录页还是提示无权限，是两种完全不同的处理。
_bearer = HTTPBearer(auto_error=False, description="Bearer <access_token>")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """解析 token → 查库取出真实且启用的用户。

    token 里其实已经带了 `role`，但这里仍然查一次库 —— 见
    `AuthService.get_active_user` 的说明：**禁用必须立即生效**。
    """
    if credentials is None or not credentials.credentials:
        raise AuthError("请先登录")

    payload = decode_access_token(credentials.credentials)

    # `sub` 是字符串（JWT 规范要求），转回 int 才能当主键用。
    # 非数字或缺失都说明这是伪造/损坏的 token，按未认证处理 —— 不能让它
    # 变成 `int("abc")` 的 ValueError 飘到全局处理器里变成 500。
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise AuthError("身份凭证无效") from None

    return await AuthService(session).get_active_user(user_id)


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """在 `get_current_user` 之上再加一道角色判断。

    依赖链上两个端点用同一个 `get_current_user` 时，FastAPI 默认会**复用**
    同一次调用结果，所以「查库」只发生一次，不会因为多一层依赖而多打一次库。
    """
    if not AuthService.is_admin(user):
        raise ForbiddenError("该操作仅管理员可用")
    return user


async def require_worker(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """`/rpa/*` 只认 Worker 角色 —— 与 `require_admin` 对称。

    管理员来调 RPA 接口同样返回 403：那组接口的语义是「以某个 Worker 的
    身份领活、回传」，`task_executions.worker_name` 记的就是这个身份。
    让管理员账号从这儿走，等于让一个不是 Worker 的东西冒充 Worker。
    """
    if not AuthService.is_worker(user):
        raise ForbiddenError("该接口仅 RPA Worker 可用")
    return user


async def get_queue_service(
    redis: Annotated[RedisClient, Depends(get_redis)],
) -> QueueService:
    """队列服务没有状态，每次请求新建一个轻量包装即可 ——
    真正贵的连接池在 `database/redis.py` 里是全局单例。"""
    return QueueService(redis)


#: 端点上的简写。写成类型别名而不是每次都写 `Depends(...)`，
#: 是为了让函数签名里「这个接口要不要登录」一眼可见。
SessionDep = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
WorkerUser = Annotated[User, Depends(require_worker)]
QueueDep = Annotated[QueueService, Depends(get_queue_service)]
