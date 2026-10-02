-- 003_orders  订单
-- 见《数据库设计》§4.3。
-- 注意：下单时间字段叫 ordered_at 而不是 created_at ——
-- 「业务发生时间」和「这行记录写进库的时间」是两件事，混用迟早出错。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS orders (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_no        VARCHAR(64)     NOT NULL               COMMENT '订单号，业务唯一',
  platform        VARCHAR(32)     NOT NULL DEFAULT 'mock' COMMENT '来源平台，v1 固定 mock',
  ordered_at      DATETIME        NOT NULL               COMMENT '下单时间（业务时间）',
  customer_name   VARCHAR(64)     NOT NULL,
  phone           VARCHAR(32)     NOT NULL,
  address         VARCHAR(512)    NOT NULL               COMMENT '收货地址',
  product_name    VARCHAR(255)    NOT NULL,
  sku             VARCHAR(64)     NOT NULL,
  quantity        INT             NOT NULL DEFAULT 1,
  amount          DECIMAL(12,2)   NOT NULL               COMMENT '订单金额，绝不用 FLOAT/DOUBLE',
  buyer_message   VARCHAR(1000)   NULL                   COMMENT '买家留言（AI 主要输入）',
  seller_note     VARCHAR(1000)   NULL                   COMMENT '卖家备注',
  status          VARCHAR(20)     NOT NULL DEFAULT 'IMPORTED'
                                  COMMENT 'IMPORTED/ANALYZING/ANALYZED/TASK_CREATED/COMPLETED/FAILED',
  import_batch_id BIGINT UNSIGNED NULL                   COMMENT '来源导入批次',
  imported_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '导入时间',
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_orders_order_no (order_no),
  KEY idx_orders_status (status),
  KEY idx_orders_imported_at (imported_at),
  KEY idx_orders_ordered_at (ordered_at),
  KEY idx_orders_batch (import_batch_id),
  CONSTRAINT fk_orders_batch FOREIGN KEY (import_batch_id)
    REFERENCES import_batches (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单';
