-- 004_ai_analyses  AI 分析结果
-- 见《数据库设计》§4.4。一个订单可以有多条（支持重新分析），
-- 取 created_at 最新的那条为有效结果。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS ai_analyses (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NOT NULL,
  priority       VARCHAR(16)     NULL              COMMENT 'LOW/MEDIUM/HIGH',
  deadline       VARCHAR(32)     NULL              COMMENT 'TODAY/TOMORROW/NONE/具体日期',
  need_contact   TINYINT(1)      NULL              COMMENT '是否需要联系客户',
  risk_level     VARCHAR(16)     NULL              COMMENT 'LOW/MEDIUM/HIGH',
  risk_reason    VARCHAR(500)    NULL              COMMENT '风险原因（自然语言）',
  action         VARCHAR(500)    NULL              COMMENT '建议动作（自然语言）',
  model_name     VARCHAR(64)     NULL              COMMENT '如 deepseek-chat',
  prompt_version VARCHAR(32)     NULL              COMMENT 'Prompt 版本，便于回溯效果',
  raw_response   JSON            NULL              COMMENT 'LLM 原始返回，排查用',
  status         VARCHAR(16)     NOT NULL DEFAULT 'SUCCESS' COMMENT 'SUCCESS/FAILED',
  error_message  VARCHAR(500)    NULL              COMMENT 'AI 调用失败原因',
  duration_ms    INT             NULL              COMMENT 'AI 调用耗时',
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_ai_analyses_order (order_id, created_at),
  KEY idx_ai_analyses_risk (risk_level),
  CONSTRAINT fk_ai_analyses_order FOREIGN KEY (order_id)
    REFERENCES orders (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI分析结果';
