"""AI 分析业务逻辑 —— 单笔分析、结果列表、人工修正（《API接口设计》§8）。

单笔分析的完整链路（AI Worker 出队后调它）：

    CAS IMPORTED→ANALYZING  →  立即 commit（释放行锁）
      →  调 LLM（慢，不能在事务里攥着）
      →  硬规则合并（硬规则优先）
      →  写 ai_analyses  →  CAS ANALYZING→ANALYZED  →  commit

两个**必须**守住的点：

1. **CAS 消费**。AI 队列是至少一次投递，同一单可能被两个 worker 拿到；条件
   UPDATE 让后到的那个 `rowcount==0`，直接 SKIPPED，不重复分析。
2. **失败也要闭环**。LLM 彻底失败时写一条 `status=FAILED` 的行，但业务字段用
   保守兜底（`risk_level=MEDIUM`），订单照样推进到 `ANALYZED`。绝不能让
   `risk_level` 留 NULL —— `TaskService._decide` 把 NULL 当作「无需审核」放行，
   那等于 AI 一挂就把风险单自动放出去了。
"""

import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ai.client import OrderAnalyzer
from ai.classifier.order_classifier import conservative_fallback
from ai.extractor.order_extractor import OrderInput
from ai.extractor.response_parser import ResponseParseError
from ai.llm.base import LLMError
from ai.prompts.order_analysis_v2 import PROMPT_VERSION

from app.core.enums import (
    DEADLINE_DATE_PATTERN,
    AiAnalysisStatus,
    Deadline,
    OrderStatus,
    Priority,
    RiskLevel,
    TaskStatus,
)
from app.core.exceptions import NotFoundError, ParamError
from app.models.ai_analysis import AiAnalysis
from app.models.ai_review_log import AiReviewLog
from app.models.order import Order
from app.repositories.ai_analysis_repository import (
    AiAnalysisFilters,
    AiAnalysisRepository,
)
from app.repositories.order_repository import OrderRepository
from app.repositories.task_repository import TaskRepository
from app.schemas.ai_analysis import (
    AiAnalysisListItem,
    AiAnalysisReviewApplied,
    AiAnalysisReviewChange,
    AiAnalysisReviewResult,
)
from app.schemas.common import Page, PageParams
from app.services import rule_engine

logger = logging.getLogger(__name__)

#: 人工可修正的字段白名单（《API接口设计》§8.2 的约束表）。
_ALLOWED_REVIEW_FIELDS = frozenset(
    {"priority", "deadline", "need_contact", "risk_level", "risk_reason", "action"}
)

#: 自由文本字段的长度上限，与 `ai_analyses` 的列宽一致。
_MAX_TEXT_LENGTH = 500

_DEADLINE_DATE_RE = re.compile(DEADLINE_DATE_PATTERN)
_PRIORITY_VALUES = {p.value for p in Priority}
_RISK_VALUES = {r.value for r in RiskLevel}
_DEADLINE_VALUES = {d.value for d in Deadline}

_TRUE_WORDS = {"true", "1", "yes", "y"}
_FALSE_WORDS = {"false", "0", "no", "n"}


@dataclass(slots=True)
class AnalyzeOutcome:
    """单笔分析的结果。`skipped=True` 表示 CAS 没抢到（别人分析过了/已完成），
    调用方**不要**据此再去建任务。"""

    order_id: int
    skipped: bool = False
    analysis_id: int | None = None
    #: LLM 调用失败（但已写 FAILED 行 + 保守兜底，订单仍推进到 ANALYZED）
    failed: bool = False


def _log_str(value: object) -> str | None:
    """把字段值转成写进 `ai_review_logs` 的字符串。布尔用 "true"/"false"，
    与《API接口设计》§8.2 的示例一致。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_review_value(field_name: str, raw: str) -> object:
    """按字段类型解析人工修正值，非法值抛 `ParamError`（对应 4000）。"""
    text = raw.strip()

    if field_name == "priority":
        if text not in _PRIORITY_VALUES:
            raise ParamError(
                f"priority 只能是 {'/'.join(sorted(_PRIORITY_VALUES))}，收到 {raw!r}"
            )
        return text

    if field_name == "risk_level":
        if text not in _RISK_VALUES:
            raise ParamError(
                f"risk_level 只能是 {'/'.join(sorted(_RISK_VALUES))}，收到 {raw!r}"
            )
        return text

    if field_name == "deadline":
        if text in _DEADLINE_VALUES or _DEADLINE_DATE_RE.match(text):
            return text
        raise ParamError(
            f"deadline 只能是 {'/'.join(sorted(_DEADLINE_VALUES))} 或 YYYY-MM-DD，"
            f"收到 {raw!r}"
        )

    if field_name == "need_contact":
        lowered = text.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ParamError(f"need_contact 只能是 true/false，收到 {raw!r}")

    # risk_reason / action：自由文本
    if len(text) > _MAX_TEXT_LENGTH:
        raise ParamError(f"{field_name} 超过 {_MAX_TEXT_LENGTH} 字上限")
    return text


class AiAnalysisService:
    def __init__(
        self, session: AsyncSession, analyzer: OrderAnalyzer | None = None
    ) -> None:
        self.session = session
        #: 只有 `analyze_order` 需要它；列表/人工修正这两条读改写路径由 API 层
        #: 调用，不传 analyzer。
        self.analyzer = analyzer
        self.orders = OrderRepository(session)
        self.analyses = AiAnalysisRepository(session)
        self.tasks = TaskRepository(session)

    # ==========================================================
    # 单笔分析
    # ==========================================================

    async def analyze_order(
        self, order_id: int, *, blacklisted_phones: set[str]
    ) -> AnalyzeOutcome:
        analyzer = self.analyzer
        if analyzer is None:
            # 编程错误：构造时没给 analyzer 却要分析。API 层不该走到这里。
            raise RuntimeError("AiAnalysisService 未配置 analyzer，无法执行分析")

        # ① CAS 认领：只有此刻仍是 IMPORTED 才轮到我们分析。
        claimed = await self.orders.cas_transition_status(
            order_id, OrderStatus.IMPORTED, OrderStatus.ANALYZING
        )
        if not claimed:
            logger.info("订单 %d 已不在 IMPORTED，跳过（可能已被分析或已推进）", order_id)
            return AnalyzeOutcome(order_id=order_id, skipped=True)

        # ② 立即提交：把 ANALYZING 落库并释放行锁。后面是慢的 LLM 调用，
        #    不能一直攥着这个事务（否则这一单的行锁会卡住整轮补偿扫描）。
        await self.session.commit()

        order = await self.orders.get(order_id)
        if order is None:
            logger.warning("订单 %d 认领后已不存在，放弃分析", order_id)
            return AnalyzeOutcome(order_id=order_id, skipped=True)

        # ③ 调 LLM，并记录耗时。
        order_input = self._to_input(order)
        started = time.perf_counter()
        failed = False
        error_message: str | None = None
        raw: dict | None = None
        model_name: str | None = None
        prompt_version = PROMPT_VERSION

        try:
            outcome = await analyzer.analyze(order_input)
            ai_result = outcome.result
            raw = outcome.raw
            model_name = outcome.model
            prompt_version = outcome.prompt_version
        except (LLMError, ResponseParseError) as exc:
            # LLM 彻底失败：不抛出去，走 §6.2 的保守兜底，让闭环不断。
            failed = True
            error_message = str(exc)[:_MAX_TEXT_LENGTH]
            ai_result = conservative_fallback(error_message)
            logger.warning("订单 %s AI 分析失败，转人工兜底：%s", order.order_no, exc)

        duration_ms = int((time.perf_counter() - started) * 1000)

        # ④ 硬规则合并（硬规则优先）。
        hard = rule_engine.evaluate(order, blacklisted_phones=blacklisted_phones)
        merged = rule_engine.merge(ai_result, hard)
        for conflict in merged.conflicts:
            logger.warning("订单 %s 硬规则与 AI 冲突：%s", order.order_no, conflict)

        # ⑤ 写分析行。业务字段一律是合并后的最终值 —— 绝不写 None。
        analysis = AiAnalysis(
            order_id=order_id,
            priority=merged.priority,
            deadline=merged.deadline,
            need_contact=merged.need_contact,
            risk_level=merged.risk_level,
            risk_reason=merged.risk_reason,
            action=merged.action,
            model_name=model_name,
            prompt_version=prompt_version,
            raw_response=raw,
            status=(
                AiAnalysisStatus.FAILED if failed else AiAnalysisStatus.SUCCESS
            ).value,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self.session.add(analysis)
        await self.session.flush()

        # ⑥ 推进 ANALYZING→ANALYZED。若此刻已不是 ANALYZING（极少），分析行照样
        #    保留 —— 它是事实，不该因为状态被别人改了就丢。
        await self.orders.cas_transition_status(
            order_id, OrderStatus.ANALYZING, OrderStatus.ANALYZED
        )
        await self.session.commit()

        logger.info(
            "订单 %s 分析完成：risk=%s priority=%s%s（%d ms）",
            order.order_no,
            merged.risk_level,
            merged.priority,
            "（AI 失败，兜底）" if failed else "",
            duration_ms,
        )
        return AnalyzeOutcome(
            order_id=order_id,
            skipped=False,
            analysis_id=analysis.id,
            failed=failed,
        )

    # ==========================================================
    # 列表
    # ==========================================================

    async def list_analyses(
        self, filters: AiAnalysisFilters, params: PageParams
    ) -> Page[AiAnalysisListItem]:
        rows, total = await self.analyses.list_analyses(filters, params)
        items = [
            AiAnalysisListItem(
                id=row.analysis.id,
                order_id=row.analysis.order_id,
                order_no=row.order_no,
                buyer_message=row.buyer_message,
                priority=row.analysis.priority,
                deadline=row.analysis.deadline,
                need_contact=row.analysis.need_contact,
                risk_level=row.analysis.risk_level,
                risk_reason=row.analysis.risk_reason,
                action=row.analysis.action,
                model_name=row.analysis.model_name,
                status=row.analysis.status,
                created_at=row.analysis.created_at,
            )
            for row in rows
        ]
        return self.analyses.build_page(rows, total, params, items)

    # ==========================================================
    # 人工修正（§8.2）
    # ==========================================================

    async def review_analysis(
        self,
        analysis_id: int,
        changes: list[AiAnalysisReviewChange],
        reason: str | None,
        reviewer_id: int,
    ) -> AiAnalysisReviewResult:
        analysis = await self.analyses.get(analysis_id)
        if analysis is None:
            raise NotFoundError("分析记录不存在")

        applied: list[AiAnalysisReviewApplied] = []
        for change in changes:
            if change.field_name not in _ALLOWED_REVIEW_FIELDS:
                raise ParamError(
                    f"不允许修正字段 {change.field_name}。"
                    f"可选：{'、'.join(sorted(_ALLOWED_REVIEW_FIELDS))}"
                )
            original = getattr(analysis, change.field_name)
            new_value = _coerce_review_value(change.field_name, change.new_value)
            setattr(analysis, change.field_name, new_value)

            # 每个字段一条日志 —— 「哪个字段最常被 AI 判错」可以 GROUP BY 统计。
            self.session.add(
                AiReviewLog(
                    order_id=analysis.order_id,
                    ai_analysis_id=analysis.id,
                    field_name=change.field_name,
                    original_value=_log_str(original),
                    new_value=_log_str(new_value),
                    reviewer=reviewer_id,
                )
            )
            applied.append(
                AiAnalysisReviewApplied(
                    field_name=change.field_name,
                    original_value=_log_str(original),
                    new_value=_log_str(new_value),
                )
            )

        # 只有任务仍停在 WAITING_REVIEW 才同步优先级 —— 不让后台改数据去影响
        # 正在执行（QUEUED/RUNNING）的任务，否则 RPA 拿着旧数据执行、后端已改，
        # 两边不一致（《API接口设计》§8.2 第 4 点）。
        task_updated = False
        task = await self.tasks.get_by_order_id(analysis.order_id)
        if task is not None and task.status == TaskStatus.WAITING_REVIEW:
            task.priority = analysis.priority
            task_updated = True

        await self.session.commit()

        logger.info(
            "分析 %d 人工修正 %d 个字段（task_updated=%s）%s",
            analysis_id,
            len(applied),
            task_updated,
            f"原因：{reason}" if reason else "",
        )
        return AiAnalysisReviewResult(
            analysis_id=analysis_id, applied=applied, task_updated=task_updated
        )

    # ==========================================================
    # 内部工具
    # ==========================================================

    @staticmethod
    def _to_input(order: Order) -> OrderInput:
        """`Order` → `OrderInput`：backend 与 AI 之间的字段映射只此一处。"""
        return OrderInput(
            order_no=order.order_no,
            product_name=order.product_name,
            quantity=order.quantity,
            amount=order.amount,
            address=order.address,
            buyer_message=order.buyer_message,
            seller_note=order.seller_note,
        )
