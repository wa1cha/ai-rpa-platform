-- 008_notifications  通知
-- 见《数据库设计》§4.8。v1 只写记录 + 打日志，channel 固定 LOG。
-- 表结构先按多渠道的样子建好，以后加渠道只需加实现类，不动表。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS notifications (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  type             VARCHAR(32)     NOT NULL COMMENT 'TASK_FAILED/TASK_NEED_REVIEW/IMPORT_FAILED',
  channel          VARCHAR(16)     NOT NULL DEFAULT 'LOG' COMMENT 'v1 只有 LOG，预留 WECOM/DINGTALK/EMAIL',
  target           VARCHAR(255)    NULL     COMMENT '接收方（v1 为空）',
  title            VARCHAR(255)    NULL,
  content          TEXT            NULL,
  status           VARCHAR(16)     NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/SENT/FAILED',
  related_order_id BIGINT UNSIGNED NULL,
  related_task_id  BIGINT UNSIGNED NULL,
  sent_at          DATETIME        NULL,
  error_message    VARCHAR(500)    NULL,
  created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_notifications_status (status, created_at),
  KEY idx_notifications_task (related_task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='通知';
