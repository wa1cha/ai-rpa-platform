-- 03_erp_inventory  模拟 ERP 的库存种子数据
-- 见《数据库设计》§8、《需求规格》§8.2。
--
-- 这些 SKU 与 04_orders_sample.csv 里的订单是**成对设计**的：
-- 样例订单引用哪个 SKU，这里就必须有对应的库存行，否则跑不出「库存不足」那条分支。
--
-- 关键设计：SKU-003 的库存**故意为 0**。
-- 库存充足时 RPA 能一路走通，只有 0 库存才能把任务推进到 WAITING_REVIEW ——
-- 「一切正常」的样例数据是测不出异常分支的。
USE `mock_erp`;

INSERT INTO erp_inventory (sku, quantity) VALUES
  ('SKU-001', 50),   -- 儿童积木套装
  ('SKU-002', 30),   -- 不锈钢保温杯
  ('SKU-003', 0),    -- 蓝牙耳机  ← 故意为 0，触发「库存不足」
  ('SKU-004', 15),   -- 机械键盘
  ('SKU-005', 100),  -- 无线鼠标
  ('SKU-006', 8),    -- 显示器支架
  ('SKU-007', 25)    -- 移动电源
ON DUPLICATE KEY UPDATE quantity = VALUES(quantity);
