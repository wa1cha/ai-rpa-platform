"""RPA Worker 专用接口 —— 见《API接口设计》§10。

四个端点，全部 `require_worker`：这组接口的语义是「以某个 Worker 的身份领活、
回传」，`task_executions.worker_name` 记的就是这个身份。管理员账号来调，
返回 403 而不是放行 —— 让非 Worker 从这里走，等于允许伪造执行者身份。

业务逻辑全在 `RpaService`，这里只做三件事：校验入参、取依赖、包统一外壳。
"""

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import QueueDep, SessionDep, WorkerUser
from app.core.config import settings
from app.core.exceptions import ParamError
from app.schemas.common import ApiResponse
from app.schemas.rpa import (
    ClaimData,
    ClaimRequest,
    HeartbeatData,
    HeartbeatRequest,
    ResultData,
    ResultRequest,
    ScreenshotData,
)
from app.services.rpa_service import RpaService

router = APIRouter(prefix="/rpa", tags=["RPA Worker"])


def _service(session: SessionDep, queue: QueueDep) -> RpaService:
    return RpaService(session, queue)


@router.post(
    "/tasks/claim",
    response_model=ApiResponse[ClaimData | None],
    summary="领取任务",
    description="从队列取一个任务并置为执行中。无任务时返回 data: null（不是 404）。",
)
async def claim(
    body: ClaimRequest,
    worker: WorkerUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[ClaimData | None]:
    # `worker` 只用于鉴权，任务归属写的是 body.worker_name ——
    # 两者都由 Worker 自报，但要区分：token 证明「它有权领活」，
    # worker_name 是它给这次执行起的名字（同一台机器可能跑多个实例）。
    data = await _service(session, queue).claim(body.worker_name, body.wait_seconds)
    return ApiResponse.ok(data)


@router.post(
    "/tasks/{task_id}/heartbeat",
    response_model=ApiResponse[HeartbeatData],
    summary="心跳",
    description="刷新心跳；任务已不归本 Worker 时返回 cancel=true 令其停手。",
)
async def heartbeat(
    task_id: int,
    body: HeartbeatRequest,
    worker: WorkerUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[HeartbeatData]:
    data = await _service(session, queue).heartbeat(task_id, body.worker_name)
    return ApiResponse.ok(data)


@router.post(
    "/tasks/{task_id}/result",
    response_model=ApiResponse[ResultData],
    summary="回传执行结果",
    description="成功 → SUCCESS；失败 → 有重试额度则回队列，否则 FAILED 并转人工。",
)
async def submit_result(
    task_id: int,
    body: ResultRequest,
    worker: WorkerUser,
    session: SessionDep,
    queue: QueueDep,
) -> ApiResponse[ResultData]:
    data = await _service(session, queue).submit_result(task_id, body)
    return ApiResponse.ok(data)


@router.post(
    "/tasks/{task_id}/screenshot",
    response_model=ApiResponse[ScreenshotData],
    summary="上传失败截图",
    description=(
        "multipart/form-data：file（PNG/JPG，≤5MB）+ attempt（第几次尝试）。"
        "调用顺序是 screenshot → result，所以此刻执行记录一定还在。"
    ),
)
async def upload_screenshot(
    task_id: int,
    worker: WorkerUser,
    session: SessionDep,
    queue: QueueDep,
    file: UploadFile = File(..., description="截图文件"),
    attempt: int = Form(..., ge=1, description="对应第几次尝试"),
) -> ApiResponse[ScreenshotData]:
    # 先按 Content-Length 挡一道再读：`await file.read()` 会把整个文件读进内存，
    # 不挡的话一个超大文件就能把进程撑爆。这里只是省内存，**真正的上限判定
    # 在 service 里**（它按实际字节数判，也负责拒绝空文件）。
    if file.size is not None and file.size > settings.screenshot_max_bytes:
        raise ParamError(
            f"截图过大：{file.size} 字节，上限 {settings.screenshot_max_bytes}"
        )

    content = await file.read()
    path = await _service(session, queue).save_screenshot(
        task_id, attempt, file.filename or "", content
    )
    return ApiResponse.ok(ScreenshotData(path=path))
