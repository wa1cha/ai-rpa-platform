"""ai_review_logs 表映射 —— 见《数据库设计》§4.7。

一次修正产生 N 行（改了几个字段就几行），于是
「哪个字段最常被 AI 判错」可以直接 `GROUP BY field_name` 统计。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class AiReviewLog(Base):
    __tablename__ = "ai_review_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ai_analysis_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field_name: Mapped[str] = mapped_column(String(32), nullable=False)
    original_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewer: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
