-- 011_add_erp_orders_source_unique  erp_orders.source_order_no 加唯一索引
--
-- 背景：《模拟ERP设计》§7.2 要求同一来源订单号（= 主库 orders.order_no）
-- 在 ERP 里只能录入一次，重复录入必须报「该来源订单号已录入」。
-- 应用层查重有个绕不过的竞态：两个请求同时查到「不存在」，然后双双插入。
-- 唯一索引把这条规则落到数据层 —— 撞键时数据库直接拒绝，
-- 应用层捕获 IntegrityError 再翻译成那句提示。查重是体验，唯一索引才是保证。
--
-- 全新安装**不需要**跑这个文件：database/schema/009_mock_erp.sql 里已经有这个索引了。
-- 这个文件只服务于「库早就建好了、表上还没有这个索引」的环境。
--
-- 幂等：MySQL 没有 ADD INDEX IF NOT EXISTS，所以先查 information_schema.STATISTICS
-- 再决定执行哪条 —— 重复跑不会报 1061 Duplicate key name。

USE `mock_erp`;

SET @idx_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'erp_orders'
    AND INDEX_NAME   = 'uk_erp_orders_source_no'
);

-- 建索引前先自证建得起来：存量重复会让 ALTER 直接报 1062 而中断，
-- 与其让运维去翻错误码，不如先把冲突的行摊出来。
SELECT source_order_no, COUNT(*) AS dup_rows
FROM erp_orders
GROUP BY source_order_no
HAVING dup_rows > 1;

SET @ddl := IF(
  @idx_exists = 0,
  'ALTER TABLE erp_orders ADD UNIQUE KEY uk_erp_orders_source_no (source_order_no)',
  'SELECT ''uk_erp_orders_source_no 已存在，跳过'' AS result'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT INDEX_NAME, NON_UNIQUE, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'erp_orders'
GROUP BY INDEX_NAME, NON_UNIQUE
ORDER BY INDEX_NAME;
