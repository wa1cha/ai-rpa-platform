"""users 表映射 —— 见《数据库设计》§4.1。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="ADMIN")
    # 映射成 bool 而不是 int：列在库里是 TINYINT(1)，但业务上它就是真假，
    # 让它跨过 ORM 边界时变成真的 True/False，省得每个调用点都写 `== 1`。
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 这两个字段由数据库的 DEFAULT / ON UPDATE 维护，应用层只读不写。
    # 写成 server_default 是为了让「谁在写」这件事唯一 —— 否则应用和数据库
    # 各写一次，两端时钟不一致时 updated_at 会倒退。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
