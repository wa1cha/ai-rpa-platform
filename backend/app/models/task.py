"""tasks 表映射 —— 见《数据库设计》§4.5、《需求规格》§9 状态机。

状态取值一律用 `core/enums.py` 的 `TaskStatus`，这里只做映射不含校验：
数据库列是 VARCHAR，不拦非法值，拦住它的是 service 层。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    ai_analysis_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="MEDIUM")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    need_review: Mapped[bool] = mapped_column(nullable=False, default=False)
    review_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: 取消/人工处置的说明，**与 review_reason 分开**。
    #: 复用会让「CANCELLED 但 review_result 为 NULL、review_reason 有值」这种
    #: 行出现，读数据的人只能靠猜这行到底是审核意见还是取消原因。
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 僵尸任务回收的依据：Worker 领走任务后每 30 秒刷一次 heartbeat_at，
    #: 超过 5 分钟没刷就判定它挂了，任务重新入队。见《数据库设计》§6.1。
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
