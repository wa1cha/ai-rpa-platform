-- 009_mock_erp  模拟 ERP 的建表
-- 见《数据库设计》§7。独立库 mock_erp，与主库 ai_rpa 不共享任何表、不做外键关联。
-- 强制分离的理由：现实中 ERP 是外部系统。主库看不到 ERP 的表，
-- 「RPA 只能通过界面操作 ERP」这条约束在数据层也是真的。
USE `mock_erp`;

CREATE TABLE IF NOT EXISTS erp_users (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username      VARCHAR(50)     NOT NULL,
  password_hash VARCHAR(255)    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_erp_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ERP操作员';

CREATE TABLE IF NOT EXISTS erp_orders (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  erp_order_no    VARCHAR(64)     NOT NULL,
  source_order_no VARCHAR(64)     NOT NULL COMMENT '对应主库 orders.order_no，用于对账',
  customer_name   VARCHAR(64)     NOT NULL,
  phone           VARCHAR(32)     NOT NULL,
  address         VARCHAR(512)    NOT NULL,
  product_name    VARCHAR(255)    NOT NULL,
  sku             VARCHAR(64)     NOT NULL,
  quantity        INT             NOT NULL DEFAULT 1,
  amount          DECIMAL(12,2)   NOT NULL,
  status          VARCHAR(20)     NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT/PENDING_REVIEW/APPROVED',
  created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_erp_orders_no (erp_order_no),
  -- 同一来源订单号只能录入一次（《模拟ERP设计》§7.2）。查重是应用层的事，
  -- 这条索引是防并发插入穿透的那道底线，见 011 迁移。
  UNIQUE KEY uk_erp_orders_source_no (source_order_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ERP订单';

CREATE TABLE IF NOT EXISTS erp_inventory (
  id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku      VARCHAR(64)     NOT NULL,
  quantity INT             NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_erp_inventory_sku (sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='ERP库存';
