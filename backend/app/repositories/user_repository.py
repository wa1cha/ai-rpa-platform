"""users 表的数据访问。"""

from datetime import datetime

from sqlalchemy import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        """登录用。

        注意这里**不过滤 is_active** —— 先查出用户再判断是否禁用，
        是为了能返回「账号已被禁用」而不是笼统的「用户名或密码错误」。
        如果这里过滤掉，被禁用的用户会看到误导性的提示。
        """
        return await self.session.scalar(select(User).where(User.username == username))

    async def touch_last_login(self, user: User, when: datetime) -> None:
        user.last_login_at = when
        # 不 commit：登录动作还要签 token 等后续步骤，提交时机交给 service。
        self.session.add(user)
