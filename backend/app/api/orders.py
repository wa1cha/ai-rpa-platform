"""订单接口 —— 见《API接口设计》§6。"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import AdminUser, SessionDep
from app.core.enums import OrderStatus
from app.core.exceptions import ParamError
from app.repositories.order_repository import OrderFilters
from app.schemas.common import ApiResponse, Page, PageParams
from app.schemas.order import (
    OrderDetail,
    OrderImportResult,
    OrderListItem,
    OrderReanalyzeRequest,
    OrderReanalyzeResult,
)
from app.services.import_service import MAX_FILE_BYTES, ImportService
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["订单"])

_VALID_STATUSES = {s.value for s in OrderStatus}


def _parse_statuses(status: str | None) -> list[str] | None:
    """把 `?status=A,B` 拆成列表。

    未知状态直接报 400 而不是「返回空列表」：拼错一个字母却得到一页空白，
    排查成本远高于一句明确的报错。
    """
    if not status:
        return None
    values = [s.strip() for s in status.split(",") if s.strip()]
    unknown = [s for s in values if s not in _VALID_STATUSES]
    if unknown:
        raise ParamError(
            f"未知的订单状态：{'、'.join(unknown)}。"
            f"可选值：{'、'.join(sorted(_VALID_STATUSES))}"
        )
    return values or None


@router.get(
    "",
    response_model=ApiResponse[Page[OrderListItem]],
    summary="订单列表",
    description="支持状态/平台/订单号/客户名/风险/时间区间筛选，手机号脱敏返回。",
)
async def list_orders(
    admin: AdminUser,
    session: SessionDep,
    params: PageParams = Depends(),  # noqa: B008 —— 见 schemas/common.py 的说明
    status: str | None = None,
    platform: str | None = None,
    order_no: str | None = None,
    customer_name: str | None = None,
    has_risk: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
) -> ApiResponse[Page[OrderListItem]]:
    filters = OrderFilters(
        statuses=_parse_statuses(status),
        platform=platform,
        order_no=order_no,
        customer_name=customer_name,
        has_risk=has_risk,
        start_date=start_date,
        end_date=end_date,
    )
    page = await OrderService(session).list_orders(filters, params)
    return ApiResponse.ok(page)


@router.post(
    "/import",
    response_model=ApiResponse[OrderImportResult],
    summary="导入订单",
    description="上传 .xlsx/.csv，整批校验、全成功或全失败。dry_run=true 时只校验不入库。",
)
async def import_orders(
    admin: AdminUser,
    session: SessionDep,
    file: Annotated[UploadFile, File(description=".xlsx / .xlsm / .csv，最大 10 MB")],
    dry_run: Annotated[bool, Form(description="true 时只校验不入库")] = False,
) -> ApiResponse[OrderImportResult]:
    # `read(n)` 最多读 n 字节，所以超大文件不会被整个读进内存再被拒绝。
    # 多读 1 字节是为了让 service 能区分「刚好 10 MB」和「超过 10 MB」。
    content = await file.read(MAX_FILE_BYTES + 1)

    result = await ImportService(session).import_file(
        filename=file.filename or "未命名文件",
        content=content,
        uploaded_by=admin.id,
        dry_run=dry_run,
    )
    return ApiResponse.ok(result)


@router.get(
    "/{order_id}",
    response_model=ApiResponse[OrderDetail],
    summary="订单详情",
    description="一次返回订单 + 最新 AI 分析 + 任务摘要 + 人工修正记录，手机号不脱敏。",
)
async def get_order(
    order_id: int,
    admin: AdminUser,
    session: SessionDep,
) -> ApiResponse[OrderDetail]:
    detail = await OrderService(session).get_detail(order_id)
    return ApiResponse.ok(detail)


@router.post(
    "/{order_id}/reanalyze",
    response_model=ApiResponse[OrderReanalyzeResult],
    summary="重新触发 AI 分析",
    description=(
        "把已分析过的订单退回待分析，清掉尚未执行的旧任务并重新入队。"
        "任务已被 RPA 执行（RUNNING 或终态）时返回 4009。"
    ),
)
async def reanalyze_order(
    order_id: int,
    admin: AdminUser,
    session: SessionDep,
    payload: OrderReanalyzeRequest | None = None,
) -> ApiResponse[OrderReanalyzeResult]:
    # body 可整个省略 —— 重分析本身不需要参数，`reason` 只是留给日志的一句人话。
    result = await OrderService(session).reanalyze(
        order_id, payload.reason if payload else None
    )
    return ApiResponse.ok(result)
