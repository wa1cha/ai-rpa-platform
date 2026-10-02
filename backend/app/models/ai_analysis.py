"""ai_analyses 表映射 —— 见《数据库设计》§4.4。

一个订单可以有多条（支持重新分析），**取 `created_at` 最新的一条为有效结果**。
所以「最新」这件事靠查询时的排序保证，不在这里加关系。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database.mysql import Base


class AiAnalysis(Base):
    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deadline: Mapped[str | None] = mapped_column(String(32), nullable=True)
    need_contact: Mapped[bool | None] = mapped_column(nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    action: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="SUCCESS")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
