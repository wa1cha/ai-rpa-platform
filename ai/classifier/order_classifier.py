"""把 LLM 抠出来的 dict 收敛成**受约束**的 `AnalysisResult`。

这是契约保证的最后一道、也是**唯一**一道：既然 DeepSeek 不支持 `json_schema`
（memory `project_deepseek_structured_output`），Pydantic 重校验 + 枚举白名单
就是「模型不按套路出牌时系统仍不崩」的全部依据。

降级原则（《LLM_Prompt设计》§6.1）：**输出异常时选让业务继续流转的保守值，
而不是让流程停摆。** 非法枚举 → `MEDIUM`（既不误放行，也不误判紧急）。
"""

import re
from enum import StrEnum

from pydantic import BaseModel

#: deadline 允许的第四种形态：具体日期。
DEADLINE_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_DEADLINE_DATE_RE = re.compile(DEADLINE_DATE_PATTERN)

#: risk_reason / action 的长度上限，与《LLM_Prompt设计》§3 的 schema 一致。
MAX_TEXT_LENGTH = 200

#: 兜底文案（§6.2）。
FALLBACK_ACTION = "人工确认订单信息"
_FALLBACK_REASON_PREFIX = "AI 分析失败，已转人工确认"


class Priority(StrEnum):
    """与 backend `core.enums.Priority` 取值一致，但本包不 import backend。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AnalysisResult(BaseModel):
    """`ai_analyses` 表要落地的 6 个业务字段，就是 LLM 必须输出的字段。"""

    priority: Priority
    deadline: str
    need_contact: bool
    risk_level: RiskLevel
    risk_reason: str
    action: str


def _truncate(value: object) -> str:
    text = "" if value is None else str(value)
    return text[:MAX_TEXT_LENGTH]


def _coerce_bool(value: object) -> bool:
    """模型偶尔把布尔输出成字符串（"true" / "false"）。别让 `bool("false")` 这个
    Python 陷阱把它变成 `True`。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _normalize_deadline(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text in {"TODAY", "TOMORROW", "NONE"}:
        return text
    if _DEADLINE_DATE_RE.match(text):
        return text
    return "NONE"


def classify(raw: dict) -> AnalysisResult:
    """按白名单收敛模型输出。任何越界值都被降级，**不抛异常**。"""
    priority = raw.get("priority")
    if priority not in Priority._value2member_map_:
        priority = Priority.MEDIUM

    risk_level = raw.get("risk_level")
    if risk_level not in RiskLevel._value2member_map_:
        risk_level = RiskLevel.MEDIUM

    return AnalysisResult(
        priority=Priority(priority),
        deadline=_normalize_deadline(raw.get("deadline")),
        need_contact=_coerce_bool(raw.get("need_contact")),
        risk_level=RiskLevel(risk_level),
        risk_reason=_truncate(raw.get("risk_reason")),
        action=_truncate(raw.get("action")),
    )


def conservative_fallback(detail: str = "") -> AnalysisResult:
    """AI 彻底失败时的降级结果（《LLM_Prompt设计》§6.2）。

    `risk_level=MEDIUM` 而不是 `LOW` 是**关键**：`TaskService._decide` 把
    `risk_level=NULL` 当作「无需审核」放行，所以失败时绝不能留空、更不能给 LOW。
    保守比激进正确。
    """
    reason = _FALLBACK_REASON_PREFIX
    if detail:
        reason = f"{reason}：{detail}"
    return AnalysisResult(
        priority=Priority.MEDIUM,
        deadline="NONE",
        need_contact=False,
        risk_level=RiskLevel.MEDIUM,
        risk_reason=reason[:MAX_TEXT_LENGTH],
        action=FALLBACK_ACTION,
    )
