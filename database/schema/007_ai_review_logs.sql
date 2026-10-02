-- 007_ai_review_logs  AI 结果人工修正记录
-- 见《数据库设计》§4.7。一次修正产生 N 行（改了几个字段就几行），
-- 于是「哪个字段最常被 AI 判错」可以直接 GROUP BY field_name 统计。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS ai_review_logs (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id        BIGINT UNSIGNED NOT NULL,
  ai_analysis_id  BIGINT UNSIGNED NOT NULL,
  field_name      VARCHAR(32)     NOT NULL COMMENT 'priority/deadline/need_contact/risk_level/risk_reason/action',
  original_value  VARCHAR(500)    NULL     COMMENT 'AI 原值',
  new_value       VARCHAR(500)    NULL     COMMENT '人工修正值',
  reviewer        BIGINT UNSIGNED NOT NULL COMMENT '修正人',
  reviewed_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_ai_review_logs_order (order_id, reviewed_at),
  KEY idx_ai_review_logs_analysis (ai_analysis_id),
  CONSTRAINT fk_ai_review_logs_order FOREIGN KEY (order_id)
    REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_ai_review_logs_analysis FOREIGN KEY (ai_analysis_id)
    REFERENCES ai_analyses (id) ON DELETE CASCADE,
  CONSTRAINT fk_ai_review_logs_reviewer FOREIGN KEY (reviewer)
    REFERENCES users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI结果人工修正记录';
