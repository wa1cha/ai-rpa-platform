USE `ai_rpa`;
-- 客户黑名单。硬规则「客户在黑名单 → 直接标记异常」的数据来源
-- （见《需求规格》§8.1、《数据库设计》）。
--
-- 为什么不建 customer 主表再加外键：v1 的订单上只有 customer_name / phone
-- 两个客户标识，整个系统里没有「客户」这个实体 —— 为一条硬规则凭空引入
-- 实体表 + 订单外键，等于把一个集合查询膨胀成关系建模。
-- 黑名单本身就是「一份手机号集合」，手机号唯一键正是它天然的主键语义。
-- 匹配按手机号（不是姓名）：同名的人太多，改名的客户也不少。
CREATE TABLE IF NOT EXISTS customer_blacklist (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  phone         VARCHAR(32)  NOT NULL                    COMMENT '客户手机号；v1 的匹配键，与 orders.phone 同一格式',
  customer_name VARCHAR(64)  NULL                        COMMENT '姓名，仅作备注（同一手机号可能换过名字）',
  reason        VARCHAR(255) NULL                        COMMENT '列入黑名单的原因',
  is_active     TINYINT(1)   NOT NULL DEFAULT 1          COMMENT '是否生效；停用比删除好回溯',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_customer_blacklist_phone (phone),
  KEY idx_customer_blacklist_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='客户黑名单';
