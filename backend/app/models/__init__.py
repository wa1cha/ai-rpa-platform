"""ORM 模型集合。

统一在这里 import 一遍，作用是**让 `Base.metadata` 完整**。
少了任何一行，那个表在 SQLAlchemy 眼里就不存在 —— 而报错往往发生在
毫不相干的代码里（比如 relationship 解析、`Base.metadata.create_all`），
排查成本远高于维护这几十行 import。

模型层刻意**不定义 relationship**：异步 SQLAlchemy 里懒加载会抛
`MissingGreenlet`，是个只在运行时才暴露的坑。关联数据一律在
repository 层用显式 JOIN 或相关子查询取，写起来多几行，但行为可预测。
"""

from app.models.ai_analysis import AiAnalysis
from app.models.ai_review_log import AiReviewLog
from app.models.customer_blacklist import CustomerBlacklist
from app.models.import_batch import ImportBatch
from app.models.notification import Notification
from app.models.order import Order
from app.models.task import Task
from app.models.task_execution import TaskExecution
from app.models.user import User

__all__ = [
    "AiAnalysis",
    "AiReviewLog",
    "CustomerBlacklist",
    "ImportBatch",
    "Notification",
    "Order",
    "Task",
    "TaskExecution",
    "User",
]
