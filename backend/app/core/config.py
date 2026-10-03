"""全局配置。

所有配置项都从项目根目录的 .env 读取，代码里不允许出现硬编码的连接串或密钥。
"""

from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "AI-RPA Platform"
    app_version: str = "0.1.0"
    app_env: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    # 监听地址。放在配置里而不是写死在 scripts/start.sh，是为了让
    # 「服务监听在哪」和「健康检查打哪」只有一个出处，改一处两边都变。
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # ---------- MySQL ----------
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "ai_rpa"
    mysql_password: str = ""
    mysql_db: str = "ai_rpa"

    # ---------- Redis ----------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # ---------- JWT ----------
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    # ---------- 初始管理员（只被 scripts/seed_admin.py 读取）----------
    # 放在配置里而不是脚本参数，是为了让「用户名/口令」和数据库连接串一样，
    # 只有 .env 一个出处；脚本和 CI 都不会把明文写进命令行历史。
    admin_username: str = "admin"
    admin_password: str = ""

    # ---------- 初始 RPA Worker（只被 scripts/seed_worker.py 读取）----------
    # 和 ADMIN_* 同样的理由：口令只有 .env 一个出处，不写进命令行历史。
    # Worker 是个**独立账号**而不是管理员的一个别名 —— 它只该能调 /rpa/*，
    # 不该能看订单列表、改用户（见《API接口设计》§10 开头）。
    worker_username: str = "rpa-worker"
    worker_password: str = ""

    # ---------- AI（OpenAI 兼容接口）----------
    ai_base_url: str = "https://api.deepseek.com/v1"
    ai_api_key: str = ""
    ai_model: str = "deepseek-chat"
    ai_timeout_seconds: int = 30

    # ---------- 队列与 Worker ----------
    #: Redis 里任务队列的键名。放配置里是为了让 scripts/ 下的排查命令
    #: （`redis-cli zrange <key> 0 -1`）和应用用的是同一个名字，不用去翻代码。
    task_queue_key: str = "ai_rpa:task_queue"

    #: Redis 里 AI 分析队列的键名。**刻意和任务队列分开**：两者语义不同 ——
    #: 任务队列按优先级出队（HIGH 能插队），分析队列只能 FIFO，因为分析前
    #: 根本不知道紧急度，那正是分析的产物。member 是 order_id。
    ai_queue_key: str = "ai_rpa:ai_queue"

    #: 「导入已提交、但还没进分析队列」的容许时长。超过这么久仍是 IMPORTED，
    #: 就判定为「入队那一步失败了」，由补偿任务把它重新推回队列
    #: （见 services/ai_reconciler.py）。给 60 秒是为了不误伤正常路径：
    #: 提交和入队是同一次请求里的前后两步。
    ai_enqueue_grace_seconds: int = 60

    #: 订单停在 ANALYZING 超过这个时长，判定为「分析进程中途死了」，置回
    #: IMPORTED 重排队。与 zombie_timeout_seconds 同一套思路（那边管 RUNNING 的任务）。
    ai_analyzing_timeout_seconds: int = 300

    zombie_timeout_seconds: int = 300
    zombie_scan_interval_seconds: int = 60
    ai_worker_concurrency: int = 5

    #: AI Worker 的轮询间隔。只在「队列空、没事干」时起作用 —— 有活时
    #: `analyze_once` 一轮就把队列排空，不会傻等一个间隔才做下一批。
    ai_worker_poll_interval_seconds: int = 5

    # ---------- 模拟 ERP ----------
    mock_erp_base_url: str = "http://127.0.0.1:8001"
    mock_erp_username: str = "erp_operator"
    mock_erp_password: str = ""

    # ---------- RPA 失败截图 ----------
    #: 截图落盘目录。**刻意放在 webroot 之外**：截图拍的是 ERP 页面，里面有
    #: 客户姓名 / 电话 / 地址明文，而本项目的订单列表是特意脱敏手机号的 ——
    #: 让这些图能被无鉴权地 GET 到，等于一边遮一边漏。
    #: 取图只能走 `GET /tasks/{id}/executions/{eid}/screenshot`（需管理员 JWT）。
    screenshot_dir: Path = PROJECT_ROOT / "backend" / "var" / "screenshots"

    #: 单张截图上限。超过 5MB 多半是截了整页长图或传错了文件，
    #: 与其让它悄悄写满磁盘，不如直接拒掉。
    screenshot_max_bytes: int = 5 * 1024 * 1024

    @property
    def mysql_url(self) -> str:
        """异步 SQLAlchemy 连接串。

        密码必须做 URL 编码 —— 否则密码里出现 @ : / 等字符时连接串会被解析错，
        而这类 bug 只在特定密码下才复现，很难查。
        """
        return (
            f"mysql+asyncmy://{self.mysql_user}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"


settings = Settings()
