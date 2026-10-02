-- 002_import_batches  导入批次
-- 见《数据库设计》§4.2。回答「部分行导入失败怎么办」：
-- 整批要么全进，要么全退，失败原因逐行记在 error_detail。
USE `ai_rpa`;

CREATE TABLE IF NOT EXISTS import_batches (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  filename      VARCHAR(255)    NOT NULL              COMMENT '原始文件名',
  uploaded_by   BIGINT UNSIGNED NULL                  COMMENT '上传人',
  total_rows    INT             NOT NULL DEFAULT 0    COMMENT 'Excel 总行数',
  success_rows  INT             NOT NULL DEFAULT 0    COMMENT '成功导入行数',
  failed_rows   INT             NOT NULL DEFAULT 0    COMMENT '失败行数',
  status        VARCHAR(20)     NOT NULL DEFAULT 'PROCESSING'
                                COMMENT 'PROCESSING/COMPLETED/FAILED',
  error_detail  JSON            NULL                  COMMENT '[{"row":3,"order_no":"..","reason":"手机号格式错误"}]',
  created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at   DATETIME        NULL,
  PRIMARY KEY (id),
  KEY idx_import_batches_status (status),
  KEY idx_import_batches_created_at (created_at),
  CONSTRAINT fk_import_batches_user FOREIGN KEY (uploaded_by)
    REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='导入批次';
