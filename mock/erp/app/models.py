"""模拟 ERP 的三张表。

对应 `database/schema/009_mock_erp.sql`。模型里的约束（尤其是两个唯一键）
必须和建表脚本一字不差 —— 否则「空库由 `create_all` 自己长出来」和
「老库由迁移脚本演进」会得到两套不同的表，唯一索引只在其中一套里存在。
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ErpOrderStatus(StrEnum):
    """ERP 侧订单状态机（《模拟ERP设计》§7.1）。

    只有三态。不做「出库」「完成」—— 那是需求规格里明确排除的 v1 范围，
    多一个状态就多一条 RPA 要判断的分支。
    """

    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"


class ErpUser(Base):
    """ERP 操作员。RPA 用它登录，和人用同一个账号。"""

    __tablename__ = "erp_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class ErpOrder(Base):
    """ERP 订单。

    `source_order_no` 是与主系统的**唯一关联点**，但它没有外键 —— 两个库
    不能有外键关系（《模拟ERP设计》§6）。对账全靠 RPA 把这个业务标识带过去。

    它是唯一键：同一来源订单号只能录入一次（§7.2）。应用层查重是为了给出
    「该来源订单号已录入」这句人话，这条约束才是防并发插入穿透的底线。
    """

    __tablename__ = "erp_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: ERP 自己生成的单号，格式 ERP+YYYYMMDD+4 位当日序号。RPA 提交后要读回去。
    erp_order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    customer_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    address: Mapped[str] = mapped_column(String(512), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 金额用定点数。用 float 会让 0.1+0.2 这种误差混进财务数据。
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ErpOrderStatus.DRAFT.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, server_default=func.now()
    )


class ErpInventory(Base):
    """库存。v1 **只校验不扣减**（《模拟ERP设计》§12）——

    扣减会让并发的多个 RPA 任务互相争用同一行库存，而 v1 只需要
    「库存不足」这个异常分支可被触发。
    """

    __tablename__ = "erp_inventory"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
