"""硬规则引擎 —— 《需求规格》§8.1 的确定性判定。

分工（《LLM_Prompt设计》§1）：**AI 处理语义，硬规则处理事实。** 金额 > 10000
这种判断 AI 有概率判错，硬规则是 0%；确定性的事情用确定性手段做，既省钱又让
系统行为可预测。

**纯逻辑，不碰数据库、不 import `ai`**（`AnalysisResult` 只在类型注解里用
`TYPE_CHECKING` 引入）。黑名单集合由调用方查好传进来，所以这个模块可以只用
一个 `Order` 对象 + 一个 `set` 就完整单测。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.order import Order

if TYPE_CHECKING:  # 只为类型注解，运行时不 import ai（保持本模块可独立测）
    from ai.classifier.order_classifier import AnalysisResult

#: 超过这个金额必须人工审核（《需求规格》§8.1）。用 Decimal 比较，
#: 不引入 float —— 金额上任何精度误差都是账目问题。
AMOUNT_REVIEW_THRESHOLD = Decimal("10000")

#: 「地址过短」的阈值。中文地址写到能配送的程度至少十几个字，
#: 只填「北京市」这种显然是残缺的（§8.1 的地址过短规则）。
MIN_ADDRESS_LENGTH = 10

#: 风险等级的可比序。硬规则命中一律是 HIGH，合并时取二者较高。
_RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


@dataclass(slots=True)
class HardRuleResult:
    """一组硬规则的判定结果。任一命中即 `risk_level=HIGH`。"""

    risk_level: str = "LOW"
    risk_reason: str = ""
    triggered: list[str] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.triggered)


@dataclass(slots=True)
class MergedJudgment:
    """硬规则与 AI 合并后的**最终判定** —— 字段直接映射 `ai_analyses` 的业务列。"""

    priority: str
    deadline: str
    need_contact: bool
    risk_level: str
    risk_reason: str
    action: str
    #: 冲突明细：AI 认定的风险等级低于硬规则时逐条记录，由调用方写日志。
    conflicts: list[str] = field(default_factory=list)


def evaluate(order: Order, *, blacklisted_phones: set[str]) -> HardRuleResult:
    """跑四条硬规则，任一命中即 `HIGH` 并把所有命中的原因汇总。

    四条（《需求规格》§8.1 / 《LLM_Prompt设计》§1）：
      1. 金额 > 10000
      2. 地址缺失或过短
      3. 客户手机号在（生效的）黑名单里
      4. 数量 ≤ 0 或金额异常（≤ 0）

    `blacklisted_phones` 由调用方一次查全量后传入 —— 一轮分析几十上百单只查一次库。
    """
    reasons: list[str] = []

    if order.amount is not None and order.amount > AMOUNT_REVIEW_THRESHOLD:
        reasons.append(
            f"订单金额 {order.amount} 元超过 {AMOUNT_REVIEW_THRESHOLD} 元阈值，需人工审核"
        )

    address = (order.address or "").strip()
    if len(address) < MIN_ADDRESS_LENGTH:
        reasons.append(f"收货地址缺失或过短（{address or '空'}），无法确认配送")

    if order.phone in blacklisted_phones:
        reasons.append("客户手机号命中黑名单")

    if order.quantity is None or order.quantity <= 0:
        reasons.append(f"商品数量异常（{order.quantity}）")

    if order.amount is None or order.amount <= 0:
        reasons.append(f"订单金额异常（{order.amount}）")

    if not reasons:
        return HardRuleResult()

    return HardRuleResult(
        risk_level="HIGH",
        risk_reason="；".join(reasons),
        triggered=reasons,
    )


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def merge(ai: "AnalysisResult", hard: HardRuleResult) -> MergedJudgment:
    """合并 AI 结果与硬规则 —— **硬规则优先**（《需求规格》§8.1）。

    为什么是「优先」而不是「投票」：硬规则是业务方的明确意志（「超过 1 万必须
    人工看」），不是统计推断。如果 AI 能推翻它，规则就形同虚设。

    具体规则：
      · `risk_level` 取二者较高（序 LOW < MEDIUM < HIGH）；
      · `risk_reason` 命中硬规则时用硬规则的串，否则用 AI 的；
      · `priority` / `deadline` / `need_contact` / `action` 用 AI 的（硬规则不管这些）；
      · AI 低于硬规则时记一条 `conflicts`，交给调用方写日志 —— 否则永远不知道
        AI 和规则在哪儿打架。
    """
    ai_risk = _enum_value(ai.risk_level)
    hard_risk = hard.risk_level
    risk_level = max(ai_risk, hard_risk, key=lambda v: _RISK_ORDER.get(v, 1))

    conflicts: list[str] = []
    if hard.hit and _RISK_ORDER.get(ai_risk, 1) < _RISK_ORDER.get(hard_risk, 1):
        conflicts.append(
            f"AI 判 risk_level={ai_risk}，硬规则要求 {hard_risk}（{hard.risk_reason}）"
        )

    return MergedJudgment(
        priority=_enum_value(ai.priority),
        deadline=ai.deadline,
        need_contact=ai.need_contact,
        risk_level=risk_level,
        risk_reason=hard.risk_reason if hard.hit else ai.risk_reason,
        action=ai.action,
        conflicts=conflicts,
    )
