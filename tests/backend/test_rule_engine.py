"""硬规则引擎单测 —— `services/rule_engine.py`。

**纯 unit，不连库**：`evaluate` 只读一个 `Order` 对象的几个属性，
`merge` 只读一个 `AnalysisResult`。黑名单是调用方传进来的一个 `set`，
所以这里连「查黑名单」这一步都不需要真库。

分成三组：`evaluate` 四条规则各验一次 + 汇总；`merge` 的「硬规则优先」；
以及两者的边界（无命中、AI 已经不低于硬规则）。把规则和合并分开测，
是因为它们各自能独立出错：规则漏判是漏放风险单，合并判错是让 AI 推翻规则。
"""

from decimal import Decimal

import pytest

from ai.classifier.order_classifier import AnalysisResult
from app.models.order import Order
from app.services import rule_engine

pytestmark = pytest.mark.unit

#: 一条「什么都不触发」的订单。各用例只覆盖自己关心的那一个字段。
_BASE = {
    "order_no": "T-1",
    "product_name": "测试商品",
    "quantity": 1,
    "amount": Decimal("99.00"),
    # 中文地址必须 ≥ MIN_ADDRESS_LENGTH(10) 个字，否则会误触发「地址过短」。
    "address": "北京市朝阳区建国路 88 号 3 单元",
    "phone": "13800000000",
    "buyer_message": None,
    "seller_note": None,
}


def _order(**overrides) -> Order:
    return Order(**{**_BASE, **overrides})


def _ai(risk="LOW", priority="MEDIUM", **overrides) -> AnalysisResult:
    data = {
        "priority": priority,
        "deadline": "TOMORROW",
        "need_contact": False,
        "risk_level": risk,
        "risk_reason": "AI 的判定理由",
        "action": "AI 建议的动作",
    }
    data.update(overrides)
    return AnalysisResult(**data)


# ============================================================
# evaluate —— 四条硬规则
# ============================================================


def test_no_rule_hits_is_low_risk():
    result = rule_engine.evaluate(_order(), blacklisted_phones=set())

    assert result.risk_level == "LOW"
    assert result.hit is False
    assert result.triggered == []
    assert result.risk_reason == ""


def test_amount_over_threshold_hits():
    """金额 > 10000 必须人工审核（§8.1）。用 Decimal 比较，不引入 float。"""
    result = rule_engine.evaluate(
        _order(amount=Decimal("10000.01")), blacklisted_phones=set()
    )

    assert result.risk_level == "HIGH"
    assert result.hit is True
    assert "金额" in result.risk_reason


def test_amount_exactly_at_threshold_does_not_hit():
    """边界：**严格大于**才命中。正好 10000 是允许自动处理的。"""
    result = rule_engine.evaluate(
        _order(amount=Decimal("10000")), blacklisted_phones=set()
    )

    assert result.hit is False


def test_missing_or_short_address_hits():
    result = rule_engine.evaluate(_order(address="北京市"), blacklisted_phones=set())

    assert result.risk_level == "HIGH"
    assert "地址" in result.risk_reason


def test_blank_address_hits():
    """地址为空白（None / 全空格）同样算缺失，不能被 `len("")` 之外的形态绕过。"""
    result = rule_engine.evaluate(_order(address="   "), blacklisted_phones=set())

    assert result.hit is True
    assert "地址" in result.risk_reason


def test_blacklisted_phone_hits():
    """黑名单是「一份手机号集合」，按 `phone` 精确匹配。"""
    order = _order(phone="13800007777")

    result = rule_engine.evaluate(order, blacklisted_phones={"13800007777"})

    assert result.risk_level == "HIGH"
    assert "黑名单" in result.risk_reason


def test_non_positive_quantity_hits():
    result = rule_engine.evaluate(_order(quantity=0), blacklisted_phones=set())

    assert result.hit is True
    assert "数量" in result.risk_reason


def test_non_positive_amount_hits():
    """金额 ≤ 0 是数据异常，不能当「便宜」放过去。"""
    result = rule_engine.evaluate(
        _order(amount=Decimal("0")), blacklisted_phones=set()
    )

    assert result.hit is True
    assert "金额" in result.risk_reason


def test_several_hits_are_aggregated_into_one_reason():
    """多条命中汇总成一条 `risk_reason` —— 让人一眼看全，而不是只看第一条。"""
    order = _order(amount=Decimal("20000"), address="北京", phone="13800007777")

    result = rule_engine.evaluate(order, blacklisted_phones={"13800007777"})

    assert result.risk_level == "HIGH"
    assert len(result.triggered) == 3
    assert result.risk_reason == "；".join(result.triggered)


# ============================================================
# merge —— 硬规则优先
# ============================================================


def test_merge_without_hard_hit_keeps_the_ai_judgment():
    """硬规则没命中时，AI 的每一项都原样保留（硬规则不越权改它管不到的东西）。"""
    hard = rule_engine.evaluate(_order(), blacklisted_phones=set())
    merged = rule_engine.merge(_ai(risk="MEDIUM", priority="HIGH"), hard)

    assert merged.risk_level == "MEDIUM"
    assert merged.priority == "HIGH"
    assert merged.deadline == "TOMORROW"
    assert merged.need_contact is False
    assert merged.action == "AI 建议的动作"
    assert merged.risk_reason == "AI 的判定理由"
    assert merged.conflicts == []


def test_merge_lets_the_hard_rule_win_and_records_the_conflict():
    """AI 判 LOW、硬规则判 HIGH —— 取 HIGH，理由用硬规则的，并记一条冲突。

    这条是「硬规则优先」的核心断言：业务方的明确意志（超 1 万必须人看）
    不能被模型的统计推断推翻。
    """
    hard = rule_engine.evaluate(
        _order(amount=Decimal("20000")), blacklisted_phones=set()
    )
    merged = rule_engine.merge(_ai(risk="LOW"), hard)

    assert merged.risk_level == "HIGH"
    assert merged.risk_reason == hard.risk_reason
    assert len(merged.conflicts) == 1
    assert "LOW" in merged.conflicts[0] and "HIGH" in merged.conflicts[0]


def test_merge_does_not_flag_a_conflict_when_ai_is_already_high():
    """AI 自己就报了 HIGH：结果一致，不算冲突（否则日志里全是假冲突）。"""
    hard = rule_engine.evaluate(
        _order(amount=Decimal("20000")), blacklisted_phones=set()
    )
    merged = rule_engine.merge(_ai(risk="HIGH"), hard)

    assert merged.risk_level == "HIGH"
    assert merged.conflicts == []
    # 没冲突时理由用硬规则的（它更具体：能说清为什么）
    assert merged.risk_reason == hard.risk_reason
