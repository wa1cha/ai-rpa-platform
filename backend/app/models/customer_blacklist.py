"""customer_blacklist 表映射 —— 硬规则「客户在黑名单 → 直接标记异常」的数据来源。

见 `database/schema/010_customer_blacklist.sql` 的注释：v1 没有「客户」实体，
订单上只有 `customer_name` / `phone` 两个标识，所以黑名单就是「一份手机号集合」，
手机号唯一键正是它天然的主键语义。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class CustomerBlacklist(Base):
    __tablename__ = "customer_blacklist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    customer_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 停用而不是删除：名单要能回溯「谁曾经在名单里、为什么」。
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
