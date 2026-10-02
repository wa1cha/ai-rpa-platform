"""orders 表映射 —— 见《数据库设计》§4.3。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="mock")
    ordered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    customer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False, default=1)
    # 用 Numeric 而不是 float：钱绝不能用二进制浮点表示，
    # 0.1 + 0.2 != 0.3 这类误差落在金额上就是对不上账。
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    buyer_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    seller_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="IMPORTED")
    import_batch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
