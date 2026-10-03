"""AI 分析接口 —— 见《API接口设计》§8。两个端点都是管理员权限。

分析**本身**由作业进程里的 `ai_worker` 做（不是 HTTP 接口），这里只暴露
「看结果」和「人工修正」两个运营动作。
"""

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.core.enums import AiAnalysisStatus, Priority, RiskLevel
from app.core.exceptions import ParamError
from app.repositories.ai_analysis_repository import AiAnalysisFilters
from app.schemas.ai_analysis import (
    AiAnalysisListItem,
    AiAnalysisReviewRequest,
    AiAnalysisReviewResult,
)
from app.schemas.common import ApiResponse, Page, PageParams
from app.services.ai_service import AiAnalysisService

router = APIRouter(prefix="/ai-analyses", tags=["AI 分析"])

_VALID_RISK = {r.value for r in RiskLevel}
_VALID_PRIORITY = {p.value for p in Priority}
_VALID_STATUS = {s.value for s in AiAnalysisStatus}


def _reject(name: str, value: str, allowed: set[str]) -> None:
    raise ParamError(
        f"未知的 {name}：{value}。可选值：{'、'.join(sorted(allowed))}"
    )


@router.get(
    "",
    response_model=ApiResponse[Page[AiAnalysisListItem]],
    summary="分析结果列表",
    description="支持风险等级/优先级/是否需联系/状态/订单号筛选，按分析时间倒序。",
)
async def list_analyses(
    admin: AdminUser,
    session: SessionDep,
    params: PageParams = Depends(),  # noqa: B008 —— 见 schemas/common.py 的说明
    risk_level: str | None = None,
    priority: str | None = None,
    need_contact: bool | None = None,
    status: str | None = None,
    order_no: str | None = None,
) -> ApiResponse[Page[AiAnalysisListItem]]:
    if risk_level and risk_level not in _VALID_RISK:
        _reject("risk_level", risk_level, _VALID_RISK)
    if priority and priority not in _VALID_PRIORITY:
        _reject("priority", priority, _VALID_PRIORITY)
    if status and status not in _VALID_STATUS:
        _reject("status", status, _VALID_STATUS)

    filters = AiAnalysisFilters(
        risk_level=risk_level,
        priority=priority,
        need_contact=need_contact,
        status=status,
        order_no=order_no,
    )
    page = await AiAnalysisService(session).list_analyses(filters, params)
    return ApiResponse.ok(page)


@router.post(
    "/{analysis_id}/review",
    response_model=ApiResponse[AiAnalysisReviewResult],
    summary="人工修正 AI 结果",
    description=(
        "按字段白名单修正分析结果，逐字段写 ai_review_logs；"
        "若该订单任务仍在 WAITING_REVIEW 则同步其 priority，否则只记录。"
    ),
)
async def review_analysis(
    analysis_id: int,
    body: AiAnalysisReviewRequest,
    admin: AdminUser,
    session: SessionDep,
) -> ApiResponse[AiAnalysisReviewResult]:
    result = await AiAnalysisService(session).review_analysis(
        analysis_id, body.changes, body.reason, admin.id
    )
    return ApiResponse.ok(result)
