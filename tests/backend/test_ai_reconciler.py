"""AI 分析队列的补偿 —— `services/ai_reconciler.py`。

这一层的全部价值在于**它能在不依赖任何 AI 代码的前提下被验证**：补偿只认
「库里的状态 + 时间」，不认分析是怎么做的。所以 L1/L2 的每条腿都能用
「造一张状态陈旧/新鲜的单 → 跑一轮 → 看队列和状态」测到位。

这里刻意直接调 `reconcile_ai_queue` 并传入固定的 cutoff，不走
`reconcile_once`：后者会自己开 session、自己算「现在」，测它就得真的等
60 秒 / 300 秒，或者去改全局配置 —— 时间边界被抽成参数就是为了避开这个。
"""

from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.enums import OrderStatus
from app.database.redis import redis_client
from app.services.ai_reconciler import reconcile_ai_queue, reconcile_once
from app.services.queue_service import QueueService

#: 所有用例共用的「现在」。固定住，才能让 cutoff 与 updated_at 的先后可复现。
NOW = datetime(2026, 1, 1, 12, 0, 0)
OLD = NOW - timedelta(minutes=10)

#: 两条腿的分界线。都比 OLD 晚、比 NOW 早，所以 OLD 的单命中、NOW 的单不命中。
GRACE = NOW - timedelta(seconds=60)
TIMEOUT = NOW - timedelta(minutes=5)


def _reconcile(session, queue):
    return reconcile_ai_queue(
        session, queue, grace_cutoff=GRACE, analyzing_cutoff=TIMEOUT
    )


# ============================================================
# L1 —— IMPORTED 超时 → 重新入队
# ============================================================


@pytest.mark.integration
async def test_l1_requeues_only_imported_orders_past_the_grace(
    db_session, make_order, queue_ai
):
    """两个条件同时满足才命中：**状态是 IMPORTED**、**且够陈旧**。

    造一张新鲜的单混进去 —— 它代表「导入刚成功、入队正在进行」的正常路径，
    补偿绝不能抢先去重推它（虽然 ZADD 幂等、重推也安全，但那说明判据错了）。
    """
    stuck = await make_order(status=OrderStatus.IMPORTED.value, updated_at=OLD)
    fresh = await make_order(status=OrderStatus.IMPORTED.value, updated_at=NOW)

    report = await _reconcile(db_session, queue_ai)

    assert report.requeued_imported == 1
    assert report.order_ids == [stuck.id]
    assert await queue_ai.peek(10) == [stuck.id]
    assert fresh.id not in await queue_ai.peek(10)


# ============================================================
# L2 —— ANALYZING 超时 → 置回 IMPORTED 再入队
# ============================================================


@pytest.mark.integration
async def test_l2_resets_a_dead_analysis_and_requeues_it(
    db_session, make_order, queue_ai
):
    """分析进程挂在 ANALYZING 上，补偿要**两步都做**：状态回退 + 重新入队。

    只回退不入队 → 单永远躺在 IMPORTED 等下一轮（白等一个周期）；
    只入队不回退 → worker 出队后 CAS 会因状态不是 IMPORTED 而丢弃（白跑一趟）。
    """
    stuck = await make_order(status=OrderStatus.ANALYZING.value, updated_at=OLD)

    report = await _reconcile(db_session, queue_ai)

    assert report.reset_analyzing == 1
    await db_session.refresh(stuck)
    assert stuck.status == OrderStatus.IMPORTED.value
    assert await queue_ai.peek(10) == [stuck.id]


@pytest.mark.integration
async def test_l2_leaves_a_finished_analysis_alone(db_session, make_order, queue_ai):
    """已经 ANALYZED 的单，哪怕够陈旧也不能被拉回重做 ——

    否则一次补偿就把人家已经拿到的分析结论作废、逼着重新分析一遍。
    （CAS 那道闸挡的是同一件事的竞态版本：扫描时还是 ANALYZING、CAS 时已变成
    ANALYZED。这里从查询层验证同一条边界。）
    """
    await make_order(status=OrderStatus.ANALYZED.value, updated_at=OLD)

    report = await _reconcile(db_session, queue_ai)

    assert report.reset_analyzing == 0
    assert await queue_ai.size() == 0


# ============================================================
# 整体性质
# ============================================================


@pytest.mark.integration
async def test_reconcile_is_a_noop_when_nothing_is_stale(db_session, queue_ai):
    """正常运行时命中数恒为 0 —— 补偿不该在没人卡住的时候动任何东西。"""
    report = await _reconcile(db_session, queue_ai)

    assert (report.requeued_imported, report.reset_analyzing) == (0, 0)
    assert report.order_ids == []
    assert await queue_ai.size() == 0


@pytest.mark.integration
async def test_reconcile_is_idempotent(db_session, make_order, queue_ai):
    """同一张单在一轮轮扫描里会被反复扫到（在它被分析掉之前）。

    靠 ZADD 覆盖才不堆重复项、同一单也就不会被分析多次 —— 这是「队列本身
    必须幂等，补偿机制才成立」这条前提的落点。
    """
    stuck = await make_order(status=OrderStatus.IMPORTED.value, updated_at=OLD)

    await _reconcile(db_session, queue_ai)
    await _reconcile(db_session, queue_ai)

    assert await queue_ai.size() == 1
    assert await queue_ai.peek(10) == [stuck.id]


@pytest.mark.integration
async def test_reconcile_once_uses_the_configured_queue_key(make_order):
    """覆盖调度真正调用的入口 `reconcile_once`：它自己开 session、自己按配置算
    cutoff、自己连 `AI_QUEUE_KEY`。

    上面几条都是把 cutoff 和 queue 注入进去的，测不到「默认配置接错了」——
    比如 `ai_queue_key` 被写成 `task_queue_key`（两个 key 串在一起是这类补偿
    最隐蔽的坏法）。这条不注入任何东西，用**真实配置**跑一轮，代价是必须按
    真实 `now` 造一张够陈旧的单。
    """
    stale = await make_order(
        status=OrderStatus.IMPORTED.value,
        updated_at=datetime.now()
        - timedelta(seconds=settings.ai_enqueue_grace_seconds + 10),
    )

    report = await reconcile_once()

    assert report.requeued_imported == 1
    assert report.order_ids == [stale.id]
    assert await QueueService(
        redis_client, key=settings.ai_queue_key
    ).peek(10) == [stale.id]
