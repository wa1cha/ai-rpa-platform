"""认证相关的请求/响应模型 —— 见《API接口设计》§5。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class UserBrief(BaseModel):
    """嵌在登录响应里的用户信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str


class UserDetail(UserBrief):
    """`GET /auth/me` 的响应，比登录时多给一个最后登录时间。"""

    last_login_at: datetime | None = None


class TokenData(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = Field(description="有效期，单位秒")
    user: UserBrief
