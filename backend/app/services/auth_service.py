"""认证业务逻辑 —— 见《API接口设计》§5。"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import UserRole
from app.core.exceptions import CredentialsError, ForbiddenError
from app.core.security import create_access_token, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenData, UserBrief


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def login(self, username: str, password: str) -> TokenData:
        user = await self.users.get_by_username(username)

        # 「用户不存在」和「密码错误」返回**同一个**错误码 4002、同一句提示。
        # 分开提示等于免费送攻击者一个用户名枚举接口：他能靠提示逐个试出
        # 哪些账号存在，再针对性爆破。
        if user is None or not verify_password(password, user.password_hash):
            raise CredentialsError()

        # 但「账号被禁用」必须区分开：这不是安全问题，而是用户需要知道的运营状态，
        # 笼统报「密码错误」只会让人一直重试。
        if not user.is_active:
            raise ForbiddenError("账号已被禁用，请联系管理员")

        await self.users.touch_last_login(user, datetime.now())
        await self.session.commit()

        token = create_access_token(subject=user.id, role=user.role)
        return TokenData(
            access_token=token,
            expires_in=settings.jwt_expire_minutes * 60,
            user=UserBrief.model_validate(user),
        )

    async def get_active_user(self, user_id: int) -> User:
        """供 `deps.get_current_user` 使用：把 token 里的 id 换成真实且启用的用户。

        为什么 token 里已经有 role 了还要查库 —— 因为**禁用要立即生效**。
        角色变更可以容忍滞后（等 token 过期自然生效），但「停用账号」之后
        对方还能拿旧 token 继续操作，那是不能接受的。
        """
        user = await self.users.get(user_id)
        if user is None or not user.is_active:
            raise ForbiddenError("账号不存在或已被禁用")
        return user

    @staticmethod
    def is_admin(user: User) -> bool:
        return user.role == UserRole.ADMIN

    @staticmethod
    def is_worker(user: User) -> bool:
        return user.role == UserRole.WORKER
