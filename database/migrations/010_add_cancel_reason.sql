-- 010_add_cancel_reason  tasks 表补一列独立的「取消 / 人工处置说明」
--
-- 背景：Phase 3 收尾时决定，取消原因和重试说明**不再借用** review_reason。
-- review_reason 的语义是审核意见，复用会产生「status=CANCELLED 但
-- review_result 为 NULL、review_reason 有值」这种行 —— 读数据的人只能靠猜
-- 这行到底是审核结论还是取消原因。Phase 4 把它独立成列。见《数据库设计》§4.5。
--
-- 全新安装**不需要**跑这个文件：database/schema/005_tasks.sql 里已经有这一列了。
-- 这个文件只服务于「库早就建好了、表里还没有这一列」的环境。
--
-- 幂等：MySQL 8.0 / 9.x 没有 ADD COLUMN IF NOT EXISTS（那是 MariaDB 的扩展），
-- 所以先查 INFORMATION_SCHEMA 再决定执行哪条 —— 重复跑不会报
-- 1060 Duplicate column name。

USE `ai_rpa`;

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'tasks'
    AND COLUMN_NAME  = 'cancel_reason'
);

SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE tasks ADD COLUMN cancel_reason VARCHAR(500) NULL COMMENT ''取消 / 人工处置说明，与 review_reason 分开'' AFTER review_reason',
  'SELECT ''cancel_reason 已存在，跳过'' AS result'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'tasks'
  AND COLUMN_NAME  = 'cancel_reason';
