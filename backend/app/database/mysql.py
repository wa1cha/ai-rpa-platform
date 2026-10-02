"""MySQL 连接层 —— 全项目唯一创建数据库连接的地方。

上层（repository / service）只依赖这里导出的 `get_db` 与 `Base`，
不允许自己 `create_engine`，否则连接池会散落各处，出问题时无从收敛。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.mysql_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
    # MySQL 默认 wait_timeout 8 小时，连接放久了会被服务端单方面掐断。
    # pre_ping 每次取连接前先探一下，recycle 则主动在到期前换掉，
    # 两者配合避免「放假回来第一个请求必报 2006 MySQL server has gone away」。
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # 提交后不过期对象：否则 service 里 commit 之后再读对象属性会触发懒加载，
    # 而异步上下文中懒加载必然抛 MissingGreenlet —— 这是异步 SQLAlchemy 最常见的坑。
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    v1 不引入 Alembic，表结构以 `database/schema/*.sql` 为准，
    模型只用于查询，因此不需要在 Base 上维护命名约定。
    """


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求一个 session。

    异常时回滚，保证不会把半截事务留给下一个请求复用同一个连接时踩到。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """进程退出时关闭连接池，见 main.py 的 lifespan。"""
    await engine.dispose()
