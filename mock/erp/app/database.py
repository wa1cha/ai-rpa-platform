"""模拟 ERP 的数据库连接 —— 指向**独立库** `mock_erp`。

刻意和主服务的 `backend/app/database/mysql.py` 长得几乎一样，但不复用它：
复用意味着两个库共用一个进程、一份连接池、一次 `create_all`，
「独立库」这条约束会在某次重构里悄悄失效。两个应用的连接层各自独立，
是隔离成本最低的实现方式。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine: AsyncEngine = create_async_engine(
    settings.db_url,
    echo=settings.debug,
    # ERP 是「老系统」，并发很低，池子不需要大。
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # 同主服务：异步上下文里读未刷新的对象属性必然抛 MissingGreenlet，
    # 关掉 commit 后过期可以绕开这个最常见的坑。
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """ORM 基类。

    表结构以 `database/schema/009_mock_erp.sql` 为准。启动时的 `create_all`
    只是「空库能自己长出来」的便利，不替代建表脚本 —— 已有库靠迁移脚本演进。
    """


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    await engine.dispose()
