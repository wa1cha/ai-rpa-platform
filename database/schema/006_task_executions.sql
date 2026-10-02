-- 006_task_executions  RPA 执行记录
-- 见《数据库设计》§4.6。每次执行（含每次重试）一条，是排查问题的唯一现场。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS task_executions (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id         BIGINT UNSIGNED NOT NULL,
  attempt         INT             NOT NULL DEFAULT 1  COMMENT '第几次尝试，从1开始',
  status          VARCHAR(16)     NOT NULL DEFAULT 'RUNNING'
                                  COMMENT 'RUNNING/SUCCESS/FAILED',
  worker_name     VARCHAR(64)     NULL                COMMENT '执行该任务的 Worker 标识',
  erp_order_no    VARCHAR(64)     NULL                COMMENT '在模拟ERP中生成的单号',
  started_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at     DATETIME        NULL,
  duration_ms     INT             NULL,
  error_code      VARCHAR(64)     NULL                COMMENT '如 ERP_LOGIN_FAILED / TIMEOUT / WORKER_LOST',
  error_message   VARCHAR(1000)   NULL,
  screenshot_path VARCHAR(512)    NULL                COMMENT '失败截图相对路径',
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_executions_attempt (task_id, attempt)
                                  COMMENT '同一任务同一次尝试不可重复',
  KEY idx_task_executions_task (task_id, started_at),
  KEY idx_task_executions_status (status),
  CONSTRAINT fk_task_executions_task FOREIGN KEY (task_id)
    REFERENCES tasks (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='RPA执行记录';
