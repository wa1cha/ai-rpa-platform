"""notifications 表的数据访问 —— 见《数据库设计》§4.8。

v1 **只写不读**：通知由业务侧产生（任务最终失败、需人工审核），落库 + 打日志。
后台的「通知列表」接口是 Phase 8 的事，那时再往这里加查询方法 ——
现在先放一个没人调用的 `list_notifications` 只会先烂在那儿。
"""

from app.models.notification import Notification
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification
