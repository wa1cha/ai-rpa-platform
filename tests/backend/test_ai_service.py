"""单笔分析的链路 —— `services/ai_service.py` 的 `analyze_order`。

用 `fake_analyzer`（conftest 里的桩）注入，所以这一层**不联网**，
但仍连真库：要验的正是「CAS 认领、写行、状态推进」这些**数据库语义**。
`analyze_order` 的四条关键性质，每条一个用例：

  1. 正常：CAS 抢到 → 写 SUCCESS 行 → 订单 ANALYZED；
  2. CAS 没抢到（别人已认领/已推进）→ SKIPPED，**不重复写行**；
  3. LLM 彻底失败 → 写 FAILED 行 + 保守兜底值 → 订单**照样**推到 ANALYZED；
  4. 硬规则命中 → 即使 AI 判 LOW，落库的也是 HIGH。
"""

import pytest
from sqlalchemy import func, select

from ai.llm.base import LLMError
from app.core.enums import AiAnalysisStatus, OrderStatus
from app.models.ai_analysis import AiAnalysis
from app.services.ai_service import AiAnalysisService

pytestmark = pytest.mark.integration


async def _db_status(session, order_id: int) -> str:
    from app.models.order import Order

    result = await session.execute(select(Order.status).where(Order.id == order_id))
    return result.scalar_one()


async def _analyses_for(session, order_id: int) -> list[AiAnalysis]:
    rows = await session.execute(
        select(AiAnalysis).where(AiAnalysis.order_id == order_id)
    )
    return list(rows.scalars().all())


# ============================================================
# 正常路径
# ============================================================


async def test_analyze_writes_a_success_row_and_advances_the_order(
    db_session, make_order, fake_analyzer
):
    order = await make_order()

    outcome = await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        order.id, blacklisted_phones=set()
    )

    assert outcome.skipped is False
    assert outcome.failed is False
    assert outcome.analysis_id is not None
    assert await _db_status(db_session, order.id) == OrderStatus.ANALYZED.value

    analysis = await db_session.get(AiAnalysis, outcome.analysis_id)
    assert analysis.status == AiAnalysisStatus.SUCCESS.value
    assert analysis.risk_level == "LOW"
    assert analysis.priority == "MEDIUM"
    assert analysis.model_name == "fake-model"
    assert analysis.prompt_version == "v2"
    assert analysis.duration_ms is not None
    assert analysis.error_message is None
    # 桩被真的调用了一次 —— 断言链路没在哪个分支里把它跳过
    assert len(fake_analyzer.calls) == 1


async def test_analyze_maps_the_order_into_the_ai_input(
    db_session, make_order, fake_analyzer
):
    """backend 的唯一映射点：`Order` → `OrderInput` 的七个字段要对得上，
    否则模型拿到的是错的数据，而这一点在单测里最容易漏。"""
    order = await make_order(
        product_name="无线耳机",
        quantity=3,
        buyer_message="请发顺丰",
        seller_note="已确认",
    )

    await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        order.id, blacklisted_phones=set()
    )

    sent = fake_analyzer.calls[0]
    assert sent.order_no == order.order_no
    assert sent.product_name == "无线耳机"
    assert sent.quantity == 3
    assert sent.buyer_message == "请发顺丰"
    assert sent.seller_note == "已确认"


# ============================================================
# CAS —— 至少一次投递的另一半
# ============================================================


async def test_analyze_skips_when_the_order_is_not_imported(
    db_session, make_order, fake_analyzer
):
    """订单已不在 IMPORTED（别人分析完了 / 已推进）→ CAS 拿不到 → SKIPPED。

    这是防「同一单被两个 worker 各分析一遍」的那道闸。关键断言不只是
    `skipped=True`，还有**一行分析都不该写**、**模型一次都不该调**。
    """
    order = await make_order(status=OrderStatus.ANALYZED.value)

    outcome = await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        order.id, blacklisted_phones=set()
    )

    assert outcome.skipped is True
    assert outcome.analysis_id is None
    assert fake_analyzer.calls == []
    assert await _analyses_for(db_session, order.id) == []
    # 状态也不能被它改坏
    assert await _db_status(db_session, order.id) == OrderStatus.ANALYZED.value


# ============================================================
# LLM 失败 —— 闭环不断
# ============================================================


async def test_llm_failure_still_writes_a_row_and_advances(
    db_session, make_order, fake_analyzer
):
    """LLM 彻底失败：写 FAILED 行、业务字段用保守兜底、订单照样 ANALYZED。

    `risk_level` 绝不能留 NULL —— `TaskService._decide` 把 NULL 当作
    「无需审核」放行，那等于 AI 一挂就把风险单自动放出去了。
    """
    fake_analyzer.error = LLMError("上游 500，重试后仍失败")
    order = await make_order()

    outcome = await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        order.id, blacklisted_phones=set()
    )

    assert outcome.failed is True
    assert outcome.skipped is False
    assert await _db_status(db_session, order.id) == OrderStatus.ANALYZED.value

    analysis = await db_session.get(AiAnalysis, outcome.analysis_id)
    assert analysis.status == AiAnalysisStatus.FAILED.value
    assert analysis.risk_level == "MEDIUM"          # 兜底值，不是 NULL、不是 LOW
    assert analysis.priority == "MEDIUM"
    assert analysis.action == "人工确认订单信息"
    assert "失败" in analysis.risk_reason
    assert analysis.error_message is not None
    assert analysis.model_name is None              # 没拿到模型名


# ============================================================
# 硬规则优先
# ============================================================


async def test_a_hard_rule_overrides_a_low_risk_ai_verdict(
    db_session, make_order, fake_analyzer
):
    """AI 判 LOW、金额 > 10000 → 落库的是 HIGH、理由是硬规则的。

    这一条把「合并」放到了真实链路里验：fake 永远返回 LOW，能变成 HIGH
    只可能是 `rule_engine.merge` 干的。
    """
    from decimal import Decimal

    order = await make_order(amount=Decimal("20000.00"))

    outcome = await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        order.id, blacklisted_phones=set()
    )

    analysis = await db_session.get(AiAnalysis, outcome.analysis_id)
    assert analysis.risk_level == "HIGH"
    assert "金额" in analysis.risk_reason


# ============================================================
# 缺 analyzer 的编程错误
# ============================================================


async def test_analyze_without_an_analyzer_is_a_programming_error(
    db_session, make_order
):
    """API 层（列表/修正）不传 analyzer，调用 `analyze_order` 就是 bug —— 明确炸掉。"""
    order = await make_order()

    with pytest.raises(RuntimeError):
        await AiAnalysisService(db_session).analyze_order(
            order.id, blacklisted_phones=set()
        )


async def test_analyzing_a_missing_order_is_a_no_op(db_session, fake_analyzer):
    """队列里残留了一个早已不存在的 order_id —— CAS 认领失败，直接 SKIPPED。

    队列是至少一次投递、又可能残留脏 member，所以「弹出来的是个死 id」
    必须是一条安静的分支，而不是异常。
    """
    outcome = await AiAnalysisService(db_session, fake_analyzer).analyze_order(
        999_999, blacklisted_phones=set()
    )

    assert outcome.skipped is True
    total = (
        await db_session.execute(select(func.count()).select_from(AiAnalysis))
    ).scalar_one()
    assert total == 0
