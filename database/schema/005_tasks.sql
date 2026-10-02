-- 005_tasks  RPA 任务
-- 见《数据库设计》§4.5 与《需求规格》§9 状态机。
-- 两条关键设计：
--   uk_tasks_order         把「一订单=一任务」写进数据库，不靠代码自觉
--   idx_tasks_status_priority  claim 出队的主力索引
-- claimed_by / heartbeat_at 用于回收「Worker 领走后崩了」的僵尸任务（§6.1）
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS tasks (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NOT NULL,
  ai_analysis_id BIGINT UNSIGNED NULL               COMMENT '生成时的依据，便于追溯「为什么是HIGH」',
  priority       VARCHAR(16)     NOT NULL DEFAULT 'MEDIUM'
                                 COMMENT 'LOW/MEDIUM/HIGH，决定出队顺序',
  status         VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
                                 COMMENT 'PENDING/WAITING_REVIEW/QUEUED/RUNNING/SUCCESS/FAILED/CANCELLED',
  need_review    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否需人工审核',
  review_result  VARCHAR(16)     NULL               COMMENT 'APPROVED/REJECTED',
  review_reason  VARCHAR(500)    NULL               COMMENT '审核意见',
  cancel_reason  VARCHAR(500)    NULL               COMMENT '取消 / 人工处置说明，与 review_reason 分开',
  reviewed_by    BIGINT UNSIGNED NULL,
  reviewed_at    DATETIME        NULL,
  retry_count    INT             NOT NULL DEFAULT 0 COMMENT '已重试次数',
  max_retry      INT             NOT NULL DEFAULT 3 COMMENT '最大重试次数',
  last_error     VARCHAR(1000)   NULL               COMMENT '最近一次失败原因（冗余，列表页直接用）',
  queued_at      DATETIME        NULL               COMMENT '进入队列时间',
  claimed_by     VARCHAR(64)     NULL               COMMENT '领取任务的 Worker 标识',
  heartbeat_at   DATETIME        NULL               COMMENT '最近心跳，用于回收僵尸任务',
  finished_at    DATETIME        NULL               COMMENT '终态时间',
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_tasks_order (order_id)                COMMENT '强制 1订单=1任务',
  KEY idx_tasks_status_priority (status, priority, created_at)
                                 COMMENT 'claim 出队与列表筛选的主力索引',
  KEY idx_tasks_status (status),
  KEY idx_tasks_created_at (created_at),
  CONSTRAINT fk_tasks_order FOREIGN KEY (order_id)
    REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_tasks_ai_analysis FOREIGN KEY (ai_analysis_id)
    REFERENCES ai_analyses (id) ON DELETE SET NULL,
  CONSTRAINT fk_tasks_reviewer FOREIGN KEY (reviewed_by)
    REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='RPA任务';
