"""Redis 连接层 —— 任务队列与去重状态的唯一出口。

Redis 在本项目里只承担两件事（见《需求规格》§4）：
  1. 任务队列（ZSET，score 由优先级决定）
  2. 心跳 / 限流的临时状态

它是**加速层不是事实来源**：真值永远在 MySQL（tasks.status）。
所以 Redis 挂了不该丢数据，只该让队列暂时停摆 —— 这正是 /health
把 redis 单列一项、失败时整体报 degraded 的原因。
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from redis.asyncio.client import Redis as RedisClient

from app.core.config import settings

#: 全局连接池。redis-py 的 asyncio 客户端本身线程/协程安全，共用一个即可，
#: 不要在请求里反复 from_url，否则每个请求建一个连接池，很快就打满 fd。
redis_client: RedisClient = Redis.from_url(
    settings.redis_url,
    encoding="utf-8",
    decode_responses=True,
    health_check_interval=30,
)


async def get_redis() -> AsyncGenerator[RedisClient, None]:
    """FastAPI 依赖：拿到共享客户端。

    这里**不**关闭客户端 —— 它是全局单例，关了就没了。
    真正的释放放在 lifespan 的 shutdown 里。
    """
    yield redis_client


async def close_redis() -> None:
    """进程退出时关闭连接池，见 main.py 的 lifespan。"""
    await redis_client.aclose()
