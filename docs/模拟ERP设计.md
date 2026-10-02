# 模拟 ERP 设计 v1

> 配套文档：`docs/需求规格.md`、`docs/数据库设计.md`、`docs/API接口设计.md`
> 这是本项目**唯一被 RPA 用界面操作的系统**，它决定了 RPA 那一半代码长什么样。

---

## 1. 定位

模拟 ERP 是一个**独立应用、独立数据库**的 Web 系统，扮演现实中「老旧的、没有 API 的、只能靠人点界面操作的」企业 ERP。

它存在的唯一理由：**给 RPA 一个真实的操作对象**。

### 1.1 三条硬约束

| # | 约束 | 说明 |
| --- | --- | --- |
| 1 | 它是**独立应用**，独立库 `mock_erp` | 与主库 `ai_rpa` 不共享表、无外键 |
| 2 | RPA **禁止直接调用**它的 `/api/*` | 必须通过浏览器操作页面 |
| 3 | 它**不知道 RPA 的存在** | 它就是一个普通业务系统，没有为自动化做任何让步 |

第 3 条最容易做错。**不要**在模拟 ERP 里加「给 RPA 用的专用接口」——那等于把 RPA 的价值抽掉了。

### 1.2 为什么不直接让 RPA 调主库或调 API

如果 RPA 直接 `INSERT` ERP 的订单表，代码会更短，但：

- 现实中老 ERP 往往没有 API、数据库也不开放
- 整个项目的「RPA」部分就退化成一个 HTTP 客户端，**项目失去一半价值**
- 面试官第一个问题就会是「这为什么要用 RPA？」

所以：**让 RPA 真的去点按钮、填表单、读表格。**

---

## 2. 技术选型

| 项 | 选型 | 理由 |
| --- | --- | --- |
| 框架 | FastAPI | 与主服务同技术栈，只依赖 Python |
| 页面 | **Jinja2 服务端渲染** | 见下 |
| 数据库 | MySQL（独立库 `mock_erp`） | 与主服务一致，便于 Docker Compose 编排 |
| 样式 | 原生 CSS，不引入 UI 框架 | 老系统就该长这样；也避免复杂 DOM 干扰 RPA |
| 前端 JS | 极少量（仅库存实时校验） | 老系统基本没有前端逻辑 |

### 2.1 为什么用服务端渲染，而不是 Vue SPA

这是个**关键决策**，值得写进面试答案：

| | 服务端渲染（选它） | Vue SPA |
| --- | --- | --- |
| 与 RPA 的关系 | 每次操作都是真实页面跳转 + 表单提交，**正是 RPA 最擅长也最常遇到的场景** | 页面不刷新，DOM 由 JS 动态生成，RPA 需要额外处理异步加载 |
| 调试 | Playwright 里能看到清晰的导航事件 | 需要等元素出现，问题更隐蔽 |
| 真实性 | 老 ERP 就是这种 | 老 ERP 基本不长这样 |

**结论**：SPA 会让 RPA 代码更依赖「等待策略」，调试成本高，而且不真实。服务端渲染让 RPA 的每一步都是确定性的。

---

## 3. 页面结构与流程

```
/login              登录
   ↓
/dashboard          工作台（显示待办数量）
   ↓
/orders             订单列表（可搜索、可翻页）
   ├── /orders/new      新建订单（表单）
   └── /orders/{erp_order_no}   订单详情
                                    ↓
                              提交审核 → PENDING_REVIEW
                                    ↓
/review             审核列表 → 审核通过 → APPROVED

/inventory          库存查询（独立页面）
```

### 3.1 RPA 的完整操作路径（对应需求规格 §B3）

```
① 打开 /login          → 填账号密码 → 提交
② 到达 /dashboard      → 点击「订单管理」
③ 到达 /orders         → 点击「新建订单」
④ 到达 /orders/new     → 逐项填写表单 → 点保存
⑤ 弹出确认框           → 点「确定」
⑥ 跳转 /orders/{单号}  → 读取页面上的 erp_order_no
⑦ 点击「提交审核」      → 确认
⑧ 页面出现「已提交审核」→ 判定成功
⑨ 把 erp_order_no 回传主系统
```

**这条路径有 4 个页面跳转、1 个确认对话框、1 次数据读取**，足够体现 RPA 的能力，又不至于复杂到跑不通。

---

## 4. 页面详细设计

> 每张页面都要列出：路由、字段、以及**给 RPA 用的稳定选择器**。

### 4.1 选择器规范（重要）

所有 RPA 需要定位的元素，都必须带 `data-testid` 属性。

| 约定 | 示例 |
| --- | --- |
| 输入框 | `data-testid="input-<字段名>"` |
| 按钮 | `data-testid="btn-<动作>"` |
| 表格 | `data-testid="table-<实体>"` |
| 表格行 | `data-testid="row-<实体>-{主键}"` |
| 提示/错误 | `data-testid="msg-<类型>"` |

**为什么用 `data-testid` 而不是 CSS 类名或 XPath**：

- 类名/样式随时会改，改一次 RPA 就崩
- XPath 依赖 DOM 层级，加一层 `div` 就崩
- `data-testid` 是**契约**：只要它不变，页面怎么改样式 RPA 都不受影响

代价是要在模板里多写属性。**值得** —— 这是 RPA 项目里最省钱的一个习惯。

> 但注意：真实项目里你控制不了对方系统，只能用 XPath/文本定位。所以本项目**至少保留 2 处不使用 `data-testid` 的定位**，作为「真实场景」的练习和面试谈资（见 §9.3）。

---

### 4.2 `/login` 登录页

**路由**：`GET /login`（表单页）、`POST /login`（提交）

**字段**

| 字段 | 类型 | 校验 |
| --- | --- | --- |
| `username` | text | 必填 |
| `password` | password | 必填 |

**元素选择器**

| 元素 | 选择器 |
| --- | --- |
| 用户名输入框 | `[data-testid="input-username"]` |
| 密码输入框 | `[data-testid="input-password"]` |
| 登录按钮 | `[data-testid="btn-login"]` |
| 错误提示 | `[data-testid="msg-login-error"]` |
| 页面标题 | `h1`（文本「ERP 登录」） |

**行为**

- 校验失败：停留在 `/login`，显示 `msg-login-error`，文本「用户名或密码错误」
- 成功：写 session，**延时 1.5 秒**后跳转 `/dashboard`（模拟老系统慢，见 §9.1）

---

### 4.3 `/dashboard` 工作台

**路由**：`GET /dashboard`

**元素选择器**

| 元素 | 选择器 |
| --- | --- |
| 订单管理入口 | **按链接文本**定位（`a:has-text("订单管理")`），**无 testid** —— 见 §9.3 |
| 库存查询入口 | `[data-testid="nav-inventory"]` |
| 待审核数量 | `[data-testid="stat-pending-review"]` |

> RPA 走「② 点击订单管理」这一步，就是点这个链接。**刻意不用直接访问 URL，而是点导航** —— 更贴近真实 RPA 的行为。
> 而「订单管理」这个入口**刻意不给 testid**，逼 RPA 用文本定位（§9.3）。

---

### 4.4 `/orders` 订单列表

**路由**：`GET /orders?keyword=&status=&page=`

**Query 参数**

| 参数 | 说明 |
| --- | --- |
| `keyword` | 按 `erp_order_no` 或 `source_order_no` 模糊搜索 |
| `status` | `DRAFT` / `PENDING_REVIEW` / `APPROVED` |
| `page` | 页码，从 1 开始，每页 20 |

**元素选择器**

| 元素 | 选择器 |
| --- | --- |
| 搜索输入框 | `[data-testid="input-search"]` |
| 搜索按钮 | `[data-testid="btn-search"]` |
| 新建订单按钮 | `[data-testid="btn-create-order"]` |
| 订单表格 | `[data-testid="table-orders"]` |
| 某一行 | `[data-testid="row-order-{erp_order_no}"]` |
| 行内状态列 | **按列序号**取单元格（第 7 列，下标 6），**无 testid** —— 见 §9.3 |
| 下一页 | `[data-testid="btn-page-next"]` |

**表格列**：`erp_order_no` / `source_order_no` / 客户 / 商品 / 数量 / 金额 / 状态 / 操作

> 状态列**刻意不给 testid**：RPA 只能靠列序号从行里取。列顺序一旦被改，这种定位就会
> 静默取到错的单元格 —— 这正是「按列序号定位很脆弱」的现场演示（§9.3）。

> **这一页是「读」的练习场**：RPA 执行前可以先搜一下 `source_order_no`，确认 ERP 里**还没有**这笔单 —— 这是**幂等性检查**（见 §7.2）。

---

### 4.5 `/orders/new` 新建订单

**路由**：`GET /orders/new`（表单页）、`POST /orders`（提交）

**字段**

| 字段 | 类型 | 必填 | 校验 | 选择器 |
| --- | --- | --- | --- | --- |
| `source_order_no` | text | 是 | ≤64 字符 | `[data-testid="input-source-order-no"]` |
| `customer_name` | text | 是 | ≤64 字符 | `[data-testid="input-customer-name"]` |
| `phone` | text | 是 | `^1[3-9]\d{9}$` | `[data-testid="input-phone"]` |
| `address` | textarea | 是 | ≤512 字符 | `[data-testid="input-address"]` |
| `product_name` | text | 是 | ≤255 字符 | `[data-testid="input-product-name"]` |
| `sku` | text | 是 | **必须存在于 `erp_inventory`** | `[data-testid="input-sku"]` |
| `quantity` | number | 是 | 整数 ≥ 1，且 **≤ 库存余量** | `[data-testid="input-quantity"]` |
| `amount` | number | 是 | ≥ 0，两位小数 | `[data-testid="input-amount"]` |

**按钮与提示**

| 元素 | 选择器 |
| --- | --- |
| 保存按钮 | `[data-testid="btn-save-order"]` |
| 表单错误 | `[data-testid="msg-form-error"]` |
| 确认对话框 | `[data-testid="dialog-confirm"]` |
| 确认按钮 | `[data-testid="btn-confirm-yes"]` |
| 取消按钮 | `[data-testid="btn-confirm-no"]` |

**`erp_order_no` 不在表单里** —— 由服务端自动生成，格式：

```
ERP + YYYYMMDD + 4 位当日序号
例：ERP202609300001
```

**为什么让 ERP 自己生成单号**：这样 RPA 必须**提交后回到详情页读取**这个值，再回传主系统。这正是真实 RPA 的常见动作 —— 它不是单纯的「填表机」，还要把外部系统产生的结果带回来。

**提交流程**

```
点击「保存」→ 前端弹确认框 → 点「确定」
        ↓
服务端校验
   ├─ 通过 → 生成 erp_order_no，status=DRAFT → 跳转 /orders/{erp_order_no}
   └─ 失败 → 停留本页，msg-form-error 显示原因
```

**服务端校验失败的情形**（这些是 RPA 必须能应对的真实分支）

| 情形 | 提示文本 |
| --- | --- |
| 手机号格式错误 | 手机号格式不正确 |
| SKU 不存在 | 商品编码不存在 |
| 数量 > 库存 | 库存不足，当前可用：{n} |
| 来源订单号已存在 | 该来源订单号已录入 |
| `source_order_no` 为空 | 来源订单号不能为空 |

---

### 4.6 `/orders/{erp_order_no}` 订单详情

**路由**：`GET /orders/{erp_order_no}`

**元素选择器**

| 元素 | 选择器 |
| --- | --- |
| ERP 单号 | `[data-testid="text-erp-order-no"]` |
| 状态 | `[data-testid="text-order-status"]` |
| 提交审核按钮 | `[data-testid="btn-submit-review"]` |
| 成功提示 | `[data-testid="msg-success"]` |

**行为**

- 状态为 `DRAFT` 时才显示「提交审核」按钮
- 点击 → 确认框 → `status` 变 `PENDING_REVIEW`
- 成功后显示 `msg-success`，文本「已提交审核」
- 若状态不是 `DRAFT`，按钮不渲染（RPA 找不到按钮 → 说明已完成，应视为幂等成功）

**RPA 的判定逻辑**：提交后检查 `msg-success` 是否出现，**并**检查 `text-erp-order-no` 有值 → 两者都满足才算成功。

---

### 4.7 `/review` 审核列表

**路由**：`GET /review`

**元素**：`[data-testid="table-review"]`、`[data-testid="btn-approve-{erp_order_no}"]`

> 这一步**不由 RPA 完成**。RPA 的职责到「提交审核」为止，审核是 ERP 侧人工的事。保留它是为了**让状态机能走完**（`PENDING_REVIEW → APPROVED`），后台看板才有完整数据。
> 也可以由你手动点几下，作为「人工介入 ERP」的演示。

---

### 4.8 `/inventory` 库存查询

**路由**：`GET /inventory`

**元素**：`[data-testid="table-inventory"]`、`[data-testid="cell-qty-{sku}"]`

**作用**
1. 让「库存不足」这个异常可查、可解释
2. 给 RPA 一个只读页面的练习（读表格 → 判断 → 决定是否继续）

---

## 5. 内部 API（RPA 禁止调用）

页面上的少量 AJAX（目前只有库存实时校验）走内部接口。

```
POST /api/inventory/check
Body: { "sku": "SKU-001", "quantity": 2 }
Resp: { "available": true, "stock": 50 }
```

### 5.1 如何**真正**禁止 RPA 调用

「约定不许调」是君子协定，没有约束力。这里用**技术手段**：

```
页面的所有 /api/* 请求，都必须带请求头：
    X-ERP-UI: <token>

token 由页面模板注入到 <meta name="erp-ui-token" content="...">，
由页面 JS 读取后附加到请求头。

/api/* 路由校验该头，缺失或不匹配 → 403
```

**为什么这能挡住 RPA**：

- RPA 操作**真实页面**时，页面的 JS 会自然带上这个头 → 正常放行
- RPA 若绕过页面、直接用 `requests` 调 `/api/*`，构造不出这个头（每次会话随机）→ 403

> 这个设计能让面试官眼前一亮：**「你怎么保证 RPA 不走捷径？」——「我把捷径堵了，而且不是靠约定。」**
> 但也要诚实地说：模拟 ERP 是我们自己写的，想加后门随时能加。这条约束的真正价值是**在数据层复现真实的权限边界**，而不是防住一个假想的攻击者。

---

## 6. 数据模型

建表语句见《数据库设计》§7，三张表：`erp_users`、`erp_orders`、`erp_inventory`。

> **`erp_orders.source_order_no` 是与主系统的唯一关联点，但它没有外键。**
> 主库也**不做**这个外键。两个系统在数据层完全隔离，只能通过 RPA 传递业务标识 —— 这跟现实中的系统集成完全一致。

---

## 7. 业务规则

### 7.1 订单状态

```
DRAFT ──提交审核──→ PENDING_REVIEW ──审核通过──→ APPROVED
```

只有三个状态，不做「出库」「完成」—— 那是需求规格里明确排除的 v1 范围。

### 7.2 幂等性（重要）

模拟 ERP 必须拒绝重复录入：

> 若 `source_order_no` 已存在 → 提交时返回「该来源订单号已录入」

**为什么这很关键**：RPA 的回传可能失败（网络抖动），主系统会判定任务失败并重试。重试时 RPA 就会**第二次录入同一笔订单**。

正确的 RPA 流程应该是：

```
① 先在 /orders 搜索 source_order_no
② 已存在 → 不新建，直接读取 erp_order_no，判定成功
③ 不存在 → 走新建流程
```

**这一段是面试的高价值素材**：讲清楚「RPA 重试如何保证不重复录入」，比讲「我会用 Playwright 填表单」有分量得多。

---

## 8. 配置

模拟 ERP 需要一些开关来支撑演示和测试。全部与 `mock/erp/app/config.py`
的 `ErpSettings` 字段一一对应，读同一份根目录 `.env`。

**进程自身**（模拟 ERP 启动时读）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_ERP_HOST` | `127.0.0.1` | 监听地址 |
| `MOCK_ERP_PORT` | `8001` | 监听端口。与主平台 `APP_PORT=8000` 岔开，别撞 |
| `MOCK_ERP_DB` | `mock_erp` | 独立库名 |
| `MOCK_ERP_DB_URL` | 空 | 连接串**覆盖项**。留空即按 `MYSQL_*` + `MOCK_ERP_DB` 推导；只有想连一个完全独立的库时才填 |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` | — | 复用主平台那套账号，本地不必多维护一份 |
| `MOCK_ERP_SESSION_SECRET` | 开发用固定串 | 会话签名密钥。**上线前必须换**，否则 session cookie 可被伪造 |
| `MOCK_ERP_SESSION_MINUTES` | `30` | 会话过期时间（分钟） |
| `MOCK_ERP_PAGE_DELAY_MS` | `1500` | 页面跳转延时，模拟老系统慢 |
| `MOCK_ERP_FAIL_RATE` | `0` | **故障注入**：0.2 表示 20% 概率返回「系统繁忙」 |
| `MOCK_ERP_ENABLE_REVIEW_PAGE` | `true` | 是否启用审核页 |

**操作员账号**（启动时自建，见 `app/seed.py`）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_ERP_USERNAME` | `erp_operator` | 登录模拟 ERP 的用户名 |
| `MOCK_ERP_PASSWORD` | 空 | 口令。留空则**不建账号**（只打一条 error 日志），因为 bcrypt 哈希不能写进 SQL 种子 |

> `MOCK_ERP_PASSWORD` 留空时不建账号是有意的：模拟 ERP 的口令哈希必须是
> 运行时算出来的（bcrypt 自带盐），写不进 `.sql` 种子文件。
> 详见 `database/seed/02_erp_users.sql` 的说明。

**给 RPA Worker 用的**（主平台侧读，模拟 ERP 自己不看）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `MOCK_ERP_BASE_URL` | `http://127.0.0.1:8001` | RPA 开浏览器访问的地址 |
| `MOCK_ERP_USERNAME` / `MOCK_ERP_PASSWORD` | — | 与上表同两个键，RPA 用它们登录 |

### 8.1 关于 `MOCK_ERP_FAIL_RATE`（故障注入）

这是**本项目最好用的一个测试开关**。设为 `0.2` 后，RPA 执行会随机失败，于是：

- 重试逻辑**真的能被验证**（否则你永远不知道重试写没写对）
- 僵尸任务回收也能被触发
- 演示时可以现场展示「失败 → 自动重试 → 成功」的完整过程

**默认必须为 `0`**，只在测试和演示时打开。否则开发阶段会被随机失败折磨。

**只作用在写操作上**（保存订单 / 提交审核 / 审核通过），`GET` 一律不受影响。
理由：故障注入要练的是「提交动作失败后 RPA 能不能重试」，而不是「页面打不开」——
把读也弄失败，会让 RPA 的「读回结果」步骤随机崩，反而盖住了要测的那条路径。

---

## 9. 刻意保留的「真实感」

模拟 ERP 如果做得太顺滑，RPA 代码会过于简单，演示也没有说服力。以下摩擦点是**故意**的：

### 9.1 页面慢

每次跳转延时 `MOCK_ERP_PAGE_DELAY_MS`（默认 1.5 秒）。

→ 迫使 RPA 代码使用**显式等待**（`wait_for_selector`）而不是 `sleep(1)` 硬等。
→ 面试可讲：「为什么不用固定 sleep？因为页面快慢会变，硬等要么慢要么崩。」

### 9.2 确认对话框

保存和提交审核都会弹确认框。

→ 迫使 RPA 处理**非页面导航的异步 UI**，这是真实 RPA 最常见的坑。

### 9.3 两处不使用 `data-testid` 的定位（刻意）

为了让 RPA 代码里**同时存在两种定位方式**，以下两处故意只靠文本/DOM 定位：

| 位置 | 定位方式 | 练习点 |
| --- | --- | --- |
| `/dashboard` 的「订单管理」入口 | **按链接文本**定位（无 testid） | 文本定位 |
| `/orders` 表格的状态列 | **按列序号**取单元格（无 testid） | 表格结构定位 |

理由：真实项目里你控制不了对方系统的 DOM，**必须会写脆弱的定位**。同时保留两种方式，代码里才有对比，面试时才有话说（「为什么这两处不用 testid？因为真实系统不会给你 testid」）。

### 9.4 会话过期

session `MOCK_ERP_SESSION_MINUTES` 分钟过期，过期后任意页面跳回 `/login`。

→ RPA 需要能**识别「被踢回登录页」并重新登录**，否则长跑任务会莫名失败。
→ v1 可以做最简处理：检测 URL 是否为 `/login`，是则重登一次。

---

## 10. 目录结构

```
mock/
├── erp/                            # 模拟 ERP（独立 FastAPI 应用）
│   ├── app/
│   │   ├── main.py                 # 应用入口：Session、页面延时、异常处理、静态挂载
│   │   ├── config.py               # 读取 MOCK_ERP_* 环境变量（ErpSettings）
│   │   ├── database.py             # 独立库连接（与主服务不共用 engine）
│   │   ├── models.py               # erp_users / erp_orders / erp_inventory
│   │   ├── security.py             # bcrypt 封装，**刻意不复用主服务那份**
│   │   ├── seed.py                 # 启动时自建操作员 + 库存（仅当表为空）
│   │   ├── deps.py                 # 模板环境、登录依赖、X-ERP-UI 校验、故障注入
│   │   ├── routes/
│   │   │   ├── auth.py             # /login /logout
│   │   │   ├── dashboard.py        # /dashboard
│   │   │   ├── orders.py           # /orders*（列表/新建/详情/提交审核）
│   │   │   ├── review.py           # /review（审核，可用开关摘掉）
│   │   │   ├── inventory.py        # /inventory
│   │   │   └── api.py              # /api/*（带 X-ERP-UI 校验）
│   │   └── templates/
│   │       ├── base.html
│   │       ├── login.html
│   │       ├── dashboard.html
│   │       ├── orders_list.html
│   │       ├── order_new.html
│   │       ├── order_detail.html
│   │       ├── review.html
│   │       └── inventory.html
│   ├── static/
│   │   ├── app.js                  # 注入 X-ERP-UI 头、确认框、库存实时校验
│   │   └── style.css               # 刻意朴素，像个老系统
│   ├── tests/                      # 独立测试套件（见 pytest.ini 的说明）
│   │   ├── conftest.py             # 测试库保护两层 + 夹具
│   │   ├── test_login.py
│   │   ├── test_order_flow.py
│   │   ├── test_validation.py
│   │   ├── test_api_guard.py
│   │   ├── test_data_guarantees.py # 两个唯一索引 + 撞键归因
│   │   ├── test_faults.py          # 故障注入 + 页面延时（spy，不等真时间）
│   │   └── test_units.py           # 纯逻辑，不连库
│   ├── pytest.ini                  # pythonpath=. —— 与主平台那次运行必须分开
│   └── requirements.txt
└── platform/                       # 模拟电商平台（极简）
    ├── generate_orders.py          # 生成模拟订单 CSV/Excel
    └── templates/
        └── orders_template.csv     # 列定义模板
```

> `mock/erp/tests/pytest.ini` 是刻意独立的：模拟 ERP 的包也叫 `app`，
> 与主服务 `backend/app` 同名。两套测试**绝不能塞进同一次 pytest 运行**
> （`pytest tests mock/erp` 会 `ModuleNotFoundError`），必须分开跑：
> `python -m pytest` 与 `python -m pytest mock/erp`。

---

## 11. 与主系统的对接点

| 环节 | 数据流 |
| --- | --- |
| RPA 领任务 | 主系统 → RPA：订单全字段（含 `order_no`） |
| RPA 录入 ERP | `order_no` → `erp_orders.source_order_no` |
| RPA 读取结果 | ERP 生成 `erp_order_no` → RPA 读取 |
| RPA 回传 | RPA → 主系统：`erp_order_no` 写入 `task_executions.erp_order_no` |
| 对账 | 主库 `orders.order_no` ←→ ERP `erp_orders.source_order_no` |

**对账是面试的好素材**：「你怎么确认 RPA 真的把订单录进 ERP 了？」→ 「`task_executions.erp_order_no` 有值，且能在 ERP 里按 `source_order_no` 查到。」

---

## 12. 已确认事项

实现时逐条定下来了，**留档备查**（「当时为什么这么选」比「选了哪个」更值得记）：

- [x] **页面延时加在所有 HTML 跳转上**，不只是登录后。
      实现：`main.py` 的 `slow_pages` 中间件，只对 `GET` + `Content-Type: text/html`
      生效（静态资源、`/health`、`/api/*` 都不延时）。
      测试里由 `conftest.py` 把 `MOCK_ERP_PAGE_DELAY_MS` 钉成 `0` —— 否则每个用例
      白白多花几秒。延时的**正确性**另有一条专门用例验证（用 spy 记录 `asyncio.sleep`
      的入参，而不是真的等 1.5 秒）。

- [x] **库存只校验、不扣减。** 下单时查 `erp_inventory.quantity` 够不够，
      不够就拒绝（`库存不足，当前可用：{n}`）；扣减留给「人工出库」这个 v1 不做
      的环节。理由：扣减会让并发的多个任务抢同一批库存，把「重试」这件事从
      幂等问题变成库存竞争问题，超出 v1 范围。

- [x] **`erp_order_no` 用 `MAX+1` + 唯一索引 + 重试。**
      格式 `ERP{YYYYMMDD}{序号:04d}`，序号取当天最大值 +1。数据层有
      `uk_erp_orders_no` 兜底，撞了就重试（`_ORDER_NO_RETRIES = 5`）。
      **单 Worker 下 `MAX+1` 够用，但唯一索引不能省** —— 它是并发插入穿透的底线。
      同时给 `source_order_no` 也加了唯一索引（`uk_erp_orders_source_no`，
      见迁移 `011_add_erp_orders_source_unique.sql`）：应用层查重是为了给出
      「该来源订单号已录入」这句人话，索引才是防并发的底线，两者都要。

- [x] **`X-ERP-UI` token 每次会话随机，存 session。**
      实现：登录成功时 `secrets.token_hex(16)` 写入 `session["ui_token"]`，
      模板渲染进 `base.html` 的 `<meta name="erp-ui-token">`，页面里的 `app.js`
      在 `fetch` 时把它塞进 `X-ERP-UI` 头。`/api/*` 缺这个头或对不上就 403。
      它挡的是「RPA 用 requests 直接打内部 API」这条捷径 —— 因为拿 token 必须
      先真的把页面渲染出来。

- [x] **不做「出库」「完成」状态页。** 状态只有三个：
      `DRAFT`（草稿）→ `PENDING_REVIEW`（待审核）→ `APPROVED`（已通过）。
      RPA 的终点是「审核通过」，再往后是仓储/物流，与订单录入无关。
