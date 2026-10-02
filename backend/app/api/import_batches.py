"""导入批次接口 —— 见《API接口设计》§7。

路径用连字符 `/import-batches`（与文档一致），模块名用下划线 ——
Python 模块名不能带连字符，而 URL 用连字符更符合习惯，两边各自保持自己的规范。
"""

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.core.enums import ImportBatchStatus
from app.core.exceptions import ParamError
from app.schemas.common import ApiResponse, Page, PageParams
from app.schemas.import_batch import ImportBatchDetail, ImportBatchListItem
from app.services.import_service import ImportService

router = APIRouter(prefix="/import-batches", tags=["导入批次"])


@router.get(
    "",
    response_model=ApiResponse[Page[ImportBatchListItem]],
    summary="批次列表",
    description="上传记录，按创建时间倒序。可按 status 筛选，便于只看失败的批次。",
)
async def list_batches(
    admin: AdminUser,
    session: SessionDep,
    params: PageParams = Depends(),  # noqa: B008
    status: str | None = None,
) -> ApiResponse[Page[ImportBatchListItem]]:
    if status and status not in {s.value for s in ImportBatchStatus}:
        raise ParamError(
            f"未知的批次状态：{status}。"
            f"可选值：{'、'.join(s.value for s in ImportBatchStatus)}"
        )
    page = await ImportService(session).list_batches(params, status=status)
    return ApiResponse.ok(page)


@router.get(
    "/{batch_id}",
    response_model=ApiResponse[ImportBatchDetail],
    summary="批次详情",
    description="在列表字段基础上追加失败明细与关联订单 id。",
)
async def get_batch(
    batch_id: int,
    admin: AdminUser,
    session: SessionDep,
) -> ApiResponse[ImportBatchDetail]:
    detail = await ImportService(session).get_batch(batch_id)
    return ApiResponse.ok(detail)
