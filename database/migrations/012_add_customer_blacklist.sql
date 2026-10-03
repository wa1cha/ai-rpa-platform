-- 012_add_customer_blacklist  新增 customer_blacklist 表
--
-- 背景：Phase 5 把《需求规格》§8.1 的硬规则「客户在黑名单 → 直接标记异常」
-- 正式落地。原先只在规则引擎里有一个写死的手机号，无法维护、无法停用、
-- 也说不清「谁在名单里、为什么」。此迁移把它落成一张真表。
--
-- 全新安装**不需要**跑这个文件：database/schema/010_customer_blacklist.sql
-- 里已经建了这张表（init_db.sh / init_test_db.sh 会按 0*.sql 自动载入）。
-- 这个文件只服务于「库早就建好了、还没有这张表」的环境。
--
-- 幂等：先查 INFORMATION_SCHEMA 再决定执行哪条 —— 重复跑不会报
-- 1050 Table already exists。
--
-- 种子数据不在这里：黑名单是运营数据不是表结构，种一条示例由
-- scripts/seed_blacklist.py 负责（与 seed_admin.py 同一口径：表里一条都没有才种）。

USE `ai_rpa`;

SET @tbl_exists := (
  SELECT COUNT(*) FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME   = 'customer_blacklist'
);

SET @ddl := IF(
  @tbl_exists = 0,
  'CREATE TABLE customer_blacklist (
     id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
     phone         VARCHAR(32)  NOT NULL                    COMMENT ''客户手机号；v1 的匹配键，与 orders.phone 同一格式'',
     customer_name VARCHAR(64)  NULL                        COMMENT ''姓名，仅作备注（同一手机号可能换过名字）'',
     reason        VARCHAR(255) NULL                        COMMENT ''列入黑名单的原因'',
     is_active     TINYINT(1)   NOT NULL DEFAULT 1          COMMENT ''是否生效；停用比删除好回溯'',
     created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
     updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
     PRIMARY KEY (id),
     UNIQUE KEY uk_customer_blacklist_phone (phone),
     KEY idx_customer_blacklist_active (is_active)
   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT=''客户黑名单''',
  'SELECT ''customer_blacklist 已存在，跳过'' AS result'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT TABLE_NAME, TABLE_COMMENT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'customer_blacklist';
