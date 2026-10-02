"""任务队列 —— 整个 Phase 3 的核心。

**这个文件只跟 Redis 打交道，一行 MySQL 都不碰。** 分层上它比别的 service
薄：没有仓储、没有事务，因为队列的正确性不靠事务靠命令原子性。

为什么是 ZSET 而不是 LIST
-------------------------
LIST 只能 FIFO，没法表达「HIGH 插到队首」。ZSET 每个成员带一个 score，
按 score 升序出队就成了优先级队列：

    score = 优先级权重 × 10^13 + 入队时刻的毫秒数

- 高 8 位是优先级：weight 为 0/1/2，所以 HIGH 段整体排在 MEDIUM 之前，
  与分数里具体的时间戳无关 —— 这就是「插队」。
- 低位是时间戳：同优先级内先入队的先出，保证 FIFO，不会饿死。
- 用 **毫秒** 而不是秒：一秒钟内导入几百单时，同优先级仍能分出先后。

为什么 10^13 而不是 10^12
-------------------------
2026 年的毫秒时间戳约 1.79×10^12。若用 10^12，时间戳会溢出到上一档优先级，
LOW 的单子能挤到 MEDIUM 前面。10^13 留了约 5 倍余量，本世纪内都够用。

为什么这些数字在 double 里不会丢精度
------------------------------------
最大 score ≈ 2×10^13 + 1.79×10^12 ≈ 2.18×10^13，远小于 2^53 ≈ 9.007×10^15 ——
即 double 能精确表示的最大整数。这是**验算过才敢用**的：Redis 的 ZSET
score 就是 double，一旦超过 2^53，相邻毫秒可能被舍入成同一个值。

**Redis 不是事实来源。** 任务状态的真值在 MySQL 的 `tasks.status`。
Redis 丢了顶多让队列停摆，用 `rebuild()` 照 MySQL 重建即可 ——
这也是为什么 `TaskService.pop_next_runnable` 出队后还要回查一次数据库。
"""

from collections.abc import Iterable
from datetime import datetime

from redis.asyncio.client import Redis as RedisClient

from app.core.config import settings
from app.core.enums import Priority

#: 优先级权重占的位数。改这个值等于让队列里已有的 score 全部失效，
#: 所以它和 `_score()` 是绑死的，不能用配置项。
_PRIORITY_SHIFT = 10**13

#: 成员定长补零的宽度。20 位足够放下 BIGINT 主键，几位数都按同一位数存。
_MEMBER_WIDTH = 20


def _member(member_id: int) -> str:
    """id 在 ZSET 里的成员名 —— **必须定长补零**。

    任务队列的成员是 task_id，AI 分析队列的是 order_id，规则一样。

    ZSET 里 score 相同的成员按**成员名的字典序**排。直接用裸 id 做成员名时，
    `"10" < "9"`（字符串比较逐位来，'1' < '9'），于是同一毫秒内入队的成员
    会出现「id 大的先出队」，把 FIFO 悄悄反过来。

    补成定长后字典序与数值序一致，score 相同时的兜底顺序就等于
    「id 小的先出」—— 而 id 是自增的，也就是**创建顺序**，
    正是我们想要的。

    这个 bug 很隐蔽：只有同一毫秒入队的一批成员里同时存在位数不同的 id
    时才暴露，单测和手工点几下都碰不到。
    """
    return f"{member_id:0{_MEMBER_WIDTH}d}"


def _score(priority: Priority, queued_at: datetime) -> int:
    """把「优先级 + 入队时刻」压成一个可比较的整数。"""
    return priority.weight * _PRIORITY_SHIFT + int(queued_at.timestamp() * 1000)


class QueueService:
    def __init__(self, redis: RedisClient, key: str | None = None) -> None:
        self.redis = redis
        #: 默认是任务队列。AI 分析队列把 `settings.ai_queue_key` 传进来复用同一套
        #: ZSET 机制 —— 两个队列的差别只有 score（优先级 vs 纯 FIFO）和 member
        #: （task_id vs order_id），Redis 侧的操作完全一样，没必要写第二份。
        self.key = key or settings.task_queue_key

    async def enqueue(
        self,
        task_id: int,
        priority: Priority | str,
        queued_at: datetime | None = None,
    ) -> None:
        """推入队列。已在队列里则**覆盖** score（ZADD 的默认语义）。

        覆盖正是我们要的：重试时 `queued_at` 会重新赋值，任务因此排到队尾，
        而不是仗着「创建得早」插到前面去。
        """
        priority = Priority(priority)
        if queued_at is None:
            # 兜底：正常路径上 service 一定先写 queued_at 再入队。
            # 真走到这里说明有脏数据，用当前时刻让它排到队尾，不静默丢弃。
            queued_at = datetime.now()
        await self.redis.zadd(self.key, {_member(task_id): _score(priority, queued_at)})

    async def enqueue_fifo(self, member_id: int, at: datetime | None = None) -> None:
        """按 FIFO 推入队列 —— **没有优先级**，只按入队时刻排。

        AI 分析队列用它。分析前不知道订单的紧急度（那正是分析的产出），所以
        排序只能靠入队时间。score 就是毫秒时间戳：同毫秒的成员退化成按成员名
        字典序，`_member` 的定长补零保证那等于「id 小的先出」，而 order_id
        是自增的，也就是导入顺序。

        **幂等是这个方法最重要的性质**：ZADD 对已存在的成员是覆盖而非追加。
        补偿任务会反复重入队同一批 order_id，靠这一点才不会堆出重复项
        （换成 LIST 的 RPUSH 就会），同一单也就不会被分析多次。
        """
        await self.enqueue_fifo_many([member_id], at)

    async def enqueue_fifo_many(
        self, member_ids: Iterable[int], at: datetime | None = None
    ) -> int:
        """一次 ZADD 推入一批 FIFO 成员，返回推入个数。

        导入路径一次可能几百上千单，逐条 `enqueue_fifo` 就是几百上千次网络往返；
        而它们本来就该算「同一时刻入队」—— 一次 `ZADD` 全部塞进去。

        **整批共用一个 score**，这也是想要的：score 只精确到毫秒，同一次导入的
        相对顺序由成员名兜底，而定长补零让那等于 id 升序，也就是文件里的行序。
        反过来，要是逐条调用（不传 `at`）让循环里各取一次 `datetime.now()`，
        它们的先后就成了「碰运气」的微秒序，同一份文件两次导入都不保证一致。

        幂等性与 `enqueue_fifo` 相同（ZADD 覆盖），空列表直接返回 0 ——
        不能发一次空 `ZADD`，参数个数不对 Redis 会报错。
        """
        ids = list(member_ids)
        if not ids:
            return 0
        if at is None:
            at = datetime.now()
        score = int(at.timestamp() * 1000)
        await self.redis.zadd(self.key, {_member(i): score for i in ids})
        return len(ids)

    async def pop_one(self, block_seconds: float = 0) -> int | None:
        """取出 score 最小的任务，队列空则返回 None。

        用 `ZPOPMIN` 而不是「先 ZRANGE 读、再 ZREM 删」：后者是两条命令，
        两个 Worker 同时读到同一个 id 时会重复执行。ZPOPMIN 在 Redis 里
        是单命令原子的，天然互斥。

        `block_seconds > 0` 时改用阻塞版 `BZPOPMIN`：Worker 空转轮询
        （每 100ms 问一次「有活吗」）会把 MySQL 和 FastAPI 打满，长轮询让请求
        挂起等「有活干」这个信号。**默认 0 = 立即返回**，与引入长轮询之前
        的行为完全一致，所以老的调用方不受影响。

        两条命令的返回形状**不一样**，这是最容易写错的地方：
          · `ZPOPMIN`  → `[(member, score)]`
          · `BZPOPMIN` → `(key, member, score)`，超时则 `None`

        `decode_responses=True` 已开启，返回的是 str，这里转回 int。
        """
        if block_seconds > 0:
            popped = await self.redis.bzpopmin(self.key, timeout=block_seconds)
            if not popped:
                return None
            _key, member, _score_value = popped
            return int(member)

        popped = await self.redis.zpopmin(self.key, 1)
        if not popped:
            return None
        member, _score_value = popped[0]
        return int(member)

    async def remove(self, task_id: int) -> bool:
        """把任务摘出队列，返回是否真的摘掉了一个。

        取消 / 审核驳回时调用。返回 bool 是为了让调用方知道「本来就不在队列里」
        和「刚被摘掉」的区别 —— 前者无害，但值得记一笔日志。
        """
        return bool(await self.redis.zrem(self.key, _member(task_id)))

    async def size(self) -> int:
        return int(await self.redis.zcard(self.key))

    async def peek(self, count: int = 10) -> list[int]:
        """看一眼接下来会出队哪些任务，**不出队**。排查和演示用。"""
        members = await self.redis.zrange(self.key, 0, count - 1)
        return [int(m) for m in members]

    async def rebuild(self, rows: Iterable[tuple[int, str, datetime | None]]) -> int:
        """按 MySQL 的事实重建整个队列，返回重建出的成员数。

        **不是增量补齐，是先清空再全量写入。** 增量补的想法很诱人
        （「只把缺的加进去」），但它解决不了「队列里多了一条 MySQL 已经
        不是 QUEUED 的记录」这种脏数据 —— 而队列变脏最常见的原因就是这个。
        全量重建的代价是几百次 ZADD，一次 pipeline 就发完了。

        用 `transaction=True` 的 pipeline：`DELETE` 和 `ZADD` 之间若被打断，
        队列就是**空的**，那比脏更糟（任务全部停摆）。MULTI 保证要么都生效、
        要么都不生效。

        调用前提：**没有 Worker 在跑**。否则清空的那一瞬间正在出队的 Worker
        会以为没任务了。Phase 3 还没有 Worker，Phase 4 起要在文档里写清楚
        这属于停机维护操作。
        """
        mapping = {
            _member(task_id): _score(Priority(priority), queued_at or datetime.now())
            for task_id, priority, queued_at in rows
        }

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self.key)
            if mapping:
                # 空 mapping 会让 ZADD 因参数个数不对而报错，所以必须先判空。
                pipe.zadd(self.key, mapping)
            await pipe.execute()

        return len(mapping)
