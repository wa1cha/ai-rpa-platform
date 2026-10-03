"""通知接口 —— 见《API接口设计》§12。管理员权限，v1 **只读**。

「标记已读」「手动发送」这类写操作刻意不做：真实渠道（企业微信/钉钉/邮件）
还没接入，能写的操作都是空转。等渠道落地再按 `channel` 分派着加。
"""

from fastapi import APIRouter, Depends

from app.api.deps import AdminUser, SessionDep
from app.core.enums import NotificationStatus
from app.core.exceptions import ParamError
from app.schemas.common import ApiResponse, Page, PageParams
from app.schemas.notification import NotificationListItem
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get(
    "",
    response_model=ApiResponse[Page[NotificationListItem]],
    summary="通知列表",
    description="系统告警流水，按创建时间倒序。可按 status 筛选。",
)
async def list_notifications(
    admin: AdminUser,
    session: SessionDep,
    params: PageParams = Depends(),  # noqa: B008 —— 见 schemas/common.py 的说明
    status: str | None = None,
) -> ApiResponse[Page[NotificationListItem]]:
    if status and status not in {s.value for s in NotificationStatus}:
        raise ParamError(
            f"未知的通知状态：{status}。"
            f"可选值：{'、'.join(s.value for s in NotificationStatus)}"
        )
    page = await NotificationService(session).list_notifications(params, status=status)
    return ApiResponse.ok(page)
