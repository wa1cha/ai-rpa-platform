"""任务接口 —— 见《API接口设计》§9。

端点全部是管理员权限：任务队列是运营动作，Worker 走的是一套完全不同的
`/rpa/*` 接口（Phase 4）。唯一的例外是读失败截图，它虽是 Worker 上传的，
但取图的是后台管理员，所以留在这里、走管理员鉴权。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.api.deps import AdminUser, QueueDep, SessionDep
from app.core.enums import Priority, TaskStatus
from app.core.exceptions import ParamError
from app.repositories.task_repository import TaskFilters
from app.schemas.common import ApiResponse, Page, PageParams
from app.schemas.task import (
    TaskCancelRequest,
    TaskDetail,
    TaskExecutionItem,
    TaskListItem,
    TaskRetryRequest,
    TaskRetryResult,
    TaskReviewRequest,
    TaskStatusChanged,
)
from app.services.rpa_service import RpaService
from app.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["任务"])

_VALID_STATUSES = {s.value for s in TaskStatus}
_VALID_PRIORITIES = {p.value for p in Priority}


def _parse_statuses(status: str | None) -> list[str] | None:
    """`?status=QUEUED,FAILED` —— 与订单列表同一套写法，理由见 api/orders.py。"""
    if not status:
        return None
    values = [s.strip() for s in status.split(",") if s.strip()]
    unknown = [s for s in values if s not in _VALID_STATUSES]
    if unknown:
        raise ParamError(
            f"未知的任务状态：{'、'.join(unknown)}。"
            f"可选值：{'、'.join(sorted(_VALID_STATUSES))}"
        )
    return values or None


@router.get(
    "",
    response_model=ApiResponse[Page[TaskListItem]],
    summary="任务列表",
    description="支持状态/优先级筛选，按创建时间倒序。",
)
async def list_tasks(
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
    params: PageParams = Depends(),  # noqa: B008 —— 见 schemas/common.py 的说明
    status: str | None = None,
    priority: str | None = None,
) -> ApiResponse[Page[TaskListItem]]:
    if priority and priority not in _VALID_PRIORITIES:
        raise ParamError(
            f"未知的优先级：{priority}。"
            f"可选值：{'、'.join(sorted(_VALID_PRIORITIES))}"
        )

    filters = TaskFilters(statuses=_parse_statuses(status), priority=priority)
    page = await TaskService(session, queue).list_tasks(filters, params)
    return ApiResponse.ok(page)


@router.get(
    "/{task_id}",
    response_model=ApiResponse[TaskDetail],
    summary="任务详情",
    description="返回订单快照 + 生成该任务所依据的 AI 分析 + 全部执行记录。",
)
async def get_task(
    task_id: int,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[TaskDetail]:
    detail = await TaskService(session, queue).get_detail(task_id)
    return ApiResponse.ok(detail)


@router.get(
    "/{task_id}/executions",
    response_model=ApiResponse[list[TaskExecutionItem]],
    summary="任务执行记录",
    description="含每次重试，按尝试次数升序。",
)
async def list_executions(
    task_id: int,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[list[TaskExecutionItem]]:
    items = await TaskService(session, queue).list_executions(task_id)
    return ApiResponse.ok(items)


@router.post(
    "/{task_id}/review",
    response_model=ApiResponse[TaskStatusChanged],
    summary="人工审核任务",
    description="仅 WAITING_REVIEW 可审核：APPROVED → QUEUED 入队，REJECTED → CANCELLED。",
)
async def review_task(
    task_id: int,
    body: TaskReviewRequest,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[TaskStatusChanged]:
    result = await TaskService(session, queue).review(
        task_id, body.result, body.reason, admin.id
    )
    return ApiResponse.ok(result)


@router.post(
    "/{task_id}/retry",
    response_model=ApiResponse[TaskRetryResult],
    summary="手动重试任务",
    description="仅 FAILED 可重试，重新入队。",
)
async def retry_task(
    task_id: int,
    body: TaskRetryRequest,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[TaskRetryResult]:
    result = await TaskService(session, queue).retry(
        task_id, reset_retry_count=body.reset_retry_count, reason=body.reason
    )
    return ApiResponse.ok(result)


@router.post(
    "/{task_id}/cancel",
    response_model=ApiResponse[TaskStatusChanged],
    summary="取消任务",
    description="PENDING / WAITING_REVIEW / QUEUED 可取消；RUNNING 不可取消。",
)
async def cancel_task(
    task_id: int,
    body: TaskCancelRequest,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[TaskStatusChanged]:
    result = await TaskService(session, queue).cancel(task_id, body.reason)
    return ApiResponse.ok(result)


@router.get(
    "/{task_id}/executions/{execution_id}/screenshot",
    response_class=FileResponse,
    summary="下载失败截图",
    description=(
        "返回图片二进制。**不套统一外壳**（`{code,message,data}` 装不下二进制），"
        "失败仍走统一错误响应。需管理员 JWT —— 截图含客户明文信息。"
    ),
)
async def download_screenshot(
    task_id: int,
    execution_id: int,
    admin: AdminUser,
    session: SessionDep,
    queue: QueueDep,
) -> FileResponse:
    # 返回 FileResponse 而不是把字节读进内存再包一层：截图最大 5MB，
    # FileResponse 走流式发送，也不用在这里重复一遍 Content-Type 推断。
    path, media_type = await RpaService(session, queue).read_screenshot(
        task_id, execution_id
    )
    return FileResponse(path, media_type=media_type)
