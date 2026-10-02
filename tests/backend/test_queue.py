"""队列内核测试 —— `services/queue_service.py`。

分成两层，用 marker 区分：

  · `unit`        只测 `_score` / `_member` 两个纯函数。不碰 Redis，
                  所以 `pytest -m unit` 在任何机器上都能跑。
  · `integration` 真连 Redis（db 15），测出队顺序、并发语义、重建。

分开的理由：出队顺序这类逻辑**必须**在真 Redis 上验证 —— ZSET 的排序是
Redis 算的，用假客户端把结果 mock 出来，等于把「Redis 排序对不对」这个
唯一的疑问点当成已知条件，测了个寂寞。
"""

from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.enums import Priority
from app.database.redis import redis_client
from app.services.queue_service import (
    _MEMBER_WIDTH,
    _PRIORITY_SHIFT,
    QueueService,
    _member,
    _score,
)

# ============================================================
# unit —— 纯函数
# ============================================================


@pytest.mark.unit
def test_member_is_zero_padded_to_fixed_width():
    assert len(_member(1)) == _MEMBER_WIDTH
    assert _member(1) == "0" * (_MEMBER_WIDTH - 1) + "1"


@pytest.mark.unit
def test_member_lexicographic_order_matches_numeric_order():
    """补零的唯一目的：让「score 相同时按成员名字典序」等价于「按 id 数值序」。

    不补零的话 `"10" < "9"`（逐位比较，'1' < '9'），同一毫秒入队的一批任务里
    id 大的反而先出队，FIFO 被悄悄反转。这个 bug 只在同毫秒 + 位数不同时暴露，
    所以这里刻意构造了这种组合。
    """
    ids = [9, 10, 11, 99, 100, 1000]
    assert sorted(ids, key=_member) == sorted(ids)

    # 反证：裸 id 的字典序是错的 —— 如果哪天有人「简化」掉补零，这条会提醒他为什么不行
    assert sorted(ids, key=str) != sorted(ids)


@pytest.mark.unit
def test_score_same_priority_is_fifo():
    early = datetime(2026, 1, 1, 0, 0, 0)
    late = early + timedelta(seconds=1)
    assert _score(Priority.MEDIUM, early) < _score(Priority.MEDIUM, late)


@pytest.mark.unit
def test_score_higher_priority_wins_regardless_of_age():
    """优先级段在低位时间戳之上，所以「晚几年的 HIGH」仍排在「早几年的 LOW」前面。"""
    early = datetime(2026, 1, 1)
    late = datetime(2030, 1, 1)
    assert _score(Priority.HIGH, late) < _score(Priority.MEDIUM, early)
    assert _score(Priority.MEDIUM, late) < _score(Priority.LOW, early)


@pytest.mark.unit
def test_priority_shift_leaves_room_for_the_timestamp():
    """10^13 要足够大，不能让低位时间戳溢出到上一档优先级。

    本世纪内最大的毫秒时间戳约 4.1×10^12，必须严格小于 10^13。
    """
    end_of_century = int(datetime(2099, 12, 31, 23, 59, 59).timestamp() * 1000)
    assert end_of_century < _PRIORITY_SHIFT


@pytest.mark.unit
def test_max_score_stays_within_double_exact_integer_range():
    """Redis 的 ZSET score 是 double，超过 2^53 后相邻毫秒会被舍入成同一个值。

    这条断言把「安全」从口头保证变成可执行检查：以后有人调大 _PRIORITY_SHIFT
    或加一档优先级，这里会先红。
    """
    assert _score(Priority.LOW, datetime(2099, 12, 31, 23, 59, 59)) < 2**53


# ============================================================
# integration —— 真 Redis
# ============================================================


@pytest.mark.integration
async def test_enqueue_then_pop(queue):
    await queue.enqueue(42, Priority.MEDIUM)
    assert await queue.size() == 1
    assert await queue.pop_one() == 42
    assert await queue.size() == 0


@pytest.mark.integration
async def test_pop_one_on_empty_queue_returns_none(queue):
    assert await queue.pop_one() is None


@pytest.mark.integration
async def test_pop_order_follows_priority_then_time(queue):
    """HIGH 最晚入队，也必须最先出队。"""
    t = datetime(2026, 1, 1, 12, 0, 0)
    await queue.enqueue(1, Priority.LOW, t)
    await queue.enqueue(2, Priority.HIGH, t + timedelta(hours=1))
    await queue.enqueue(3, Priority.MEDIUM, t + timedelta(minutes=30))

    assert [await queue.pop_one() for _ in range(3)] == [2, 3, 1]


@pytest.mark.integration
async def test_same_millisecond_batch_pops_in_id_order(queue):
    """一批同毫秒入队的任务，出队顺序必须是 id 升序（= 创建顺序）。

    这是补零那个 bug 的回归测试：id 位数不齐（9 与 10、99 与 100）时才暴露。
    """
    same_moment = datetime(2026, 1, 1, 12, 0, 0)
    for task_id in (9, 10, 11, 100, 99, 1000):
        await queue.enqueue(task_id, Priority.MEDIUM, same_moment)

    popped = [await queue.pop_one() for _ in range(6)]
    assert popped == [9, 10, 11, 99, 100, 1000]


@pytest.mark.integration
async def test_enqueue_overwrites_score_so_retry_goes_to_the_back(queue):
    """重试会重新给 queued_at，任务应当排到队尾，而不是仗着创建得早插队。"""
    t = datetime(2026, 1, 1, 12, 0, 0)
    await queue.enqueue(1, Priority.MEDIUM, t)
    await queue.enqueue(2, Priority.MEDIUM, t + timedelta(seconds=1))
    # 1 号重试：新时刻比 2 号更晚
    await queue.enqueue(1, Priority.MEDIUM, t + timedelta(seconds=2))

    assert await queue.size() == 2
    assert [await queue.pop_one() for _ in range(2)] == [2, 1]


@pytest.mark.integration
async def test_remove_reports_whether_member_existed(queue):
    await queue.enqueue(5, Priority.MEDIUM)
    assert await queue.remove(5) is True
    assert await queue.remove(5) is False


@pytest.mark.integration
async def test_peek_does_not_dequeue(queue):
    for task_id in (3, 1, 2):
        await queue.enqueue(task_id, Priority.MEDIUM, datetime(2026, 1, 1))
    assert await queue.peek(2) == [1, 2]
    assert await queue.size() == 3


@pytest.mark.integration
async def test_rebuild_discards_dirty_members_and_restores_from_rows(queue):
    """重建是「先清空再写入」，用来把 Redis 里 MySQL 已经不认的残留一并抹掉。"""
    await queue.enqueue(900, Priority.HIGH, datetime(2026, 1, 1))
    await queue.enqueue(901, Priority.LOW, datetime(2026, 1, 1))

    rows = [
        (7, Priority.MEDIUM.value, datetime(2026, 1, 1)),
        (8, Priority.HIGH.value, datetime(2026, 1, 1)),
    ]
    assert await queue.rebuild(rows) == 2
    assert await queue.size() == 2
    assert await queue.peek(10) == [8, 7]  # 8 是 HIGH，排在 7 前面


@pytest.mark.integration
async def test_rebuild_with_no_rows_empties_the_queue(queue):
    await queue.enqueue(1, Priority.MEDIUM)
    assert await queue.rebuild([]) == 0
    assert await queue.size() == 0


@pytest.mark.integration
async def test_rebuild_tolerates_null_queued_at(queue):
    """历史数据里 queued_at 可能为 NULL；重建不该因此炸掉整条队列。"""
    assert await queue.rebuild([(1, Priority.MEDIUM.value, None)]) == 1
    assert await queue.peek(10) == [1]


# ============================================================
# enqueue_fifo —— AI 分析队列用的纯 FIFO 入队
# ============================================================


@pytest.mark.integration
async def test_enqueue_fifo_orders_by_enqueue_time(queue):
    """没有优先级，只按入队时刻排。

    分析前根本不知道订单的紧急度 —— 那正是分析的产出 —— 所以分析队列
    只能 FIFO，这也是它必须是独立 key 的原因（语义与任务队列不同）。
    """
    t = datetime(2026, 1, 1, 12, 0, 0)
    await queue.enqueue_fifo(3, t)
    await queue.enqueue_fifo(1, t + timedelta(seconds=1))
    await queue.enqueue_fifo(2, t + timedelta(seconds=2))

    assert [await queue.pop_one() for _ in range(3)] == [3, 1, 2]


@pytest.mark.integration
async def test_enqueue_fifo_is_idempotent(queue):
    """补偿任务会反复重入队同一批 order_id，靠 ZADD 覆盖才不会堆出重复项。

    这是补偿机制能成立的前提：换成 LIST 的 RPUSH，这里会变成 size == 3，
    同一单被分析三次。
    """
    t = datetime(2026, 1, 1, 12, 0, 0)
    for _ in range(3):
        await queue.enqueue_fifo(7, t)

    assert await queue.size() == 1
    assert await queue.peek(10) == [7]


@pytest.mark.integration
async def test_enqueue_fifo_same_millisecond_pops_in_id_order(queue):
    """同毫秒入队时退化成按成员名字典序 —— 定长补零让它等于 id 升序（= 导入顺序）。"""
    same = datetime(2026, 1, 1, 12, 0, 0)
    for order_id in (10, 9, 100):
        await queue.enqueue_fifo(order_id, same)

    assert [await queue.pop_one() for _ in range(3)] == [9, 10, 100]


@pytest.mark.integration
async def test_queue_service_custom_key_isolates_from_the_task_queue(queue):
    """传了 key 就只写那个 key —— AI 分析队列与任务队列必须是两个独立 ZSET。"""
    ai_queue = QueueService(redis_client, key=settings.ai_queue_key)
    await ai_queue.enqueue_fifo(5, datetime(2026, 1, 1))

    assert await ai_queue.size() == 1
    assert await queue.size() == 0  # 默认（任务）队列没被碰过
