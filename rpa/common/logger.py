"""日志。

一个刻意的小设计：**启动成功时会打一条固定的 `STARTED_MARKER`**。
`scripts/run_rpa_worker.sh` 起来之后靠 grep 这一行来判断「真的启动了」，
而不是只判断「进程还活着」—— 进程活着但登录失败卡在重试里的情况，
只看 pid 是看不出来的。

心跳的日志会刷得很勤（每 30 秒一条）。默认 INFO 级别下心跳只打 DEBUG，
免得 8 小时的长跑把日志刷成几万行；要排查心跳问题时开 `--log-level DEBUG`。
"""

from __future__ import annotations

import logging
import sys

#: 启动探针。改这个字符串记得同步 scripts/run_rpa_worker.sh。
STARTED_MARKER = "RPA Worker 启动完成，开始领任务"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """配置根 logger。

    输出到 stdout —— 后台启动时由 shell 重定向进 `logs/rpa_worker.log`，
    前台跑时直接看得到。刻意不自己写文件：日志落哪由启动脚本决定，
    代码里再写一份就成了两个出处。
    """
    logging.basicConfig(
        level=level.upper(),
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    # Playwright 的驱动日志在 INFO 级别下会打一堆 node 侧的内部消息，
    # 对本项目没用，压到 WARNING。
    logging.getLogger("playwright").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
