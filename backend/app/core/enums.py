"""全部业务枚举 —— 唯一出处。

数据库里这些字段都是 VARCHAR（见《数据库设计》§1.1），不靠数据库约束。
因此**所有取值只能在这里定义**，任何业务代码里都不允许再出现裸字符串
"QUEUED" / "RUNNING" 之类 —— 否则同一个状态迟早会写成两种拼法，
而这种错误只会在跑了几百单之后才暴露。
"""

from enum import StrEnum

# ============================================================
# 订单
# ============================================================


class OrderStatus(StrEnum):
    """orders.status —— 见《数据库设计》§5.1"""

    IMPORTED = "IMPORTED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    TASK_CREATED = "TASK_CREATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ============================================================
# 任务
# ============================================================


class TaskStatus(StrEnum):
    """tasks.status —— 状态机见《需求规格》§9"""

    PENDING = "PENDING"
    WAITING_REVIEW = "WAITING_REVIEW"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: 终态：进入这些状态后不再流转。用于「不允许再操作」的判断。
TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


# ============================================================
# 优先级与风险
# ============================================================

#: 数字越小越先出队。作为 Redis ZSET 的 score 分段依据。
_PRIORITY_WEIGHT = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


class Priority(StrEnum):
    """tasks.priority 与 ai_analyses.priority 共用"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def weight(self) -> int:
        return _PRIORITY_WEIGHT[self.value]


class RiskLevel(StrEnum):
    """ai_analyses.risk_level"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Deadline(StrEnum):
    """ai_analyses.deadline 的三个语义取值。

    该字段还允许一个具体的 YYYY-MM-DD 日期，因此不是封闭枚举 ——
    具体日期的格式校验用 DEADLINE_DATE_PATTERN。

    用 VARCHAR 而非 DATE 存：前三个是语义值不是日期，
    混在 DATE 列里无法表达「客户说今天发」和「客户说 2026-10-05 发」的区别。
    """

    TODAY = "TODAY"
    TOMORROW = "TOMORROW"
    NONE = "NONE"


#: deadline 允许的第四种形态：具体日期
DEADLINE_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


# ============================================================
# 执行与审核
# ============================================================


class ExecutionStatus(StrEnum):
    """task_executions.status —— 单次执行的成败"""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AiAnalysisStatus(StrEnum):
    """ai_analyses.status —— AI 调用本身的成败。

    注意与 ExecutionStatus 区分：这是「AI 分析」的结果，
    不是「RPA 执行」的结果。
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ReviewResult(StrEnum):
    """tasks.review_result —— 人工审核结论"""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ============================================================
# 导入批次
# ============================================================


class ImportBatchStatus(StrEnum):
    """import_batches.status"""

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ============================================================
# 通知
# ============================================================


class NotificationType(StrEnum):
    """notifications.type"""

    TASK_FAILED = "TASK_FAILED"
    TASK_NEED_REVIEW = "TASK_NEED_REVIEW"
    IMPORT_FAILED = "IMPORT_FAILED"


class NotificationChannel(StrEnum):
    """notifications.channel —— v1 只有 LOG，其余是预留"""

    LOG = "LOG"
    WECOM = "WECOM"
    DINGTALK = "DINGTALK"
    EMAIL = "EMAIL"


class NotificationStatus(StrEnum):
    """notifications.status"""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


# ============================================================
# 用户
# ============================================================


class UserRole(StrEnum):
    """users.role

    RPA Worker 使用独立的 WORKER 角色：它跑在另一台机器上，
    权限必须最小化 —— 只能访问 /rpa/* 接口。
    """

    ADMIN = "ADMIN"
    WORKER = "WORKER"
