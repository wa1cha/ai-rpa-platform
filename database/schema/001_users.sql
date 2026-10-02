-- 001_users  用户
-- 见《数据库设计》§4.1。v1 只有管理员一个角色，WORKER 角色给 RPA 机器用。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS users (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username       VARCHAR(50)     NOT NULL                COMMENT '登录名',
  password_hash  VARCHAR(255)    NOT NULL                COMMENT 'bcrypt 哈希，绝不存明文',
  role           VARCHAR(20)     NOT NULL DEFAULT 'ADMIN' COMMENT 'ADMIN/WORKER，取值见 core/enums.py',
  is_active      TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '1启用 0禁用',
  last_login_at  DATETIME        NULL                    COMMENT '最后登录时间',
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户';
