#!/usr/bin/env python3
"""模拟电商平台 —— 生成一批可导入的订单 CSV。

用法：
    python mock/platform/generate_orders.py
    python mock/platform/generate_orders.py --count 50
    python mock/platform/generate_orders.py --out /tmp/orders.csv --prefix JD
    python mock/platform/generate_orders.py --seed 42      # 同样的数据，便于复现

## 这个脚本只干一件事：造文件

它**不**导入、**不**建任务、**不**入队 —— 导入走 `POST /api/v1/orders/import`，
建任务走 `scripts/gen_tasks.py`。三件事的失败模式和触发时机都不一样，
混在一起以后想「只重跑一次导入」就得连数据生成一起重跑。

（这也是它和 `scripts/gen_tasks.py` 的分工：那个脚本吃**已有订单**、产出**任务**；
这个脚本从零产出**订单文件**。二者恰好接在导入接口的两头。）

## 表头必须与导入契约逐字一致

导入接口按**中文表头名**映射字段（`backend/app/services/import_service.py`
的 `HEADER_MAP`），且**任何一行不合法就让整批零插入**。所以这里写死 11 列，
顺序和用例完全照抄 `database/seed/04_orders_sample.csv`：

    订单号,下单时间,客户姓名,电话,收货地址,商品名称,SKU,数量,金额,买家留言,卖家备注

生成器要主动避开三类会让整批失败的错误：单号重复、必填为空、
电话/时间/数量/金额格式不对。这些规则都抄自 `import_service`，
改那边的话记得回来对一遍。

## 为什么单号带毫秒

`订单号` 有 UNIQUE 约束，重跑一次生成器**绝不能**造出和上一批撞号的订单 ——
否则第二次导入整批失败。用「前缀 + 到毫秒的时间戳 + 三位序号」保证全局唯一；
`--prefix` 只换前缀，时间戳仍然照走，所以换了前缀也不会撞。

`--seed` 只影响**数据内容**（姓名、地址、SKU、数量等），不影响单号里的时间戳 ——
单号必须随每次运行而变，那正是唯一性的来源。
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

#: 仓库根目录（本文件在 mock/platform/ 下）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = PROJECT_ROOT / "mock" / "platform" / "out"

#: 与 `import_service.HEADER_MAP` 一致的 11 列中文表头，顺序也照抄样例文件。
HEADERS = [
    "订单号",
    "下单时间",
    "客户姓名",
    "电话",
    "收货地址",
    "商品名称",
    "SKU",
    "数量",
    "金额",
    "买家留言",
    "卖家备注",
]

#: SKU → (商品名称, 单价)。与 `database/seed` 的商品目录对齐，
#: 这样库存页、以及「SKU-003 库存 0 → 校验拒绝」的演示分支都能被触达。
PRODUCTS: dict[str, tuple[str, str]] = {
    "SKU-001": ("儿童积木套装", "299.00"),
    "SKU-002": ("不锈钢保温杯", "129.00"),
    "SKU-003": ("蓝牙耳机", "199.00"),
    "SKU-004": ("机械键盘", "299.00"),
    "SKU-005": ("无线鼠标", "99.00"),
    "SKU-006": ("显示器支架", "89.00"),
    "SKU-007": ("移动电源", "159.00"),
}

#: 电话首位固定 1，第二位 3-9（手机号段），其余 9 位随机 —— 对齐
#: `import_service.PHONE_PATTERN = ^1[3-9]\d{9}$`。
_PHONE_SECOND = "3456789"

_SURNAMES = "张李王赵陈刘杨黄周吴徐孙马朱胡郭何高林罗"
_GIVEN_NAMES = ["伟", "娜", "强", "敏", "静", "洋", "磊", "芳", "凯", "婷", "杰", "丽", "军", "霞"]

_CITIES = [
    "北京市朝阳区建国路88号",
    "上海市浦东新区世纪大道200号",
    "浙江省杭州市西湖区文三路100号",
    "广东省广州市天河区体育西路50号",
    "江苏省南京市鼓楼区中山北路10号",
    "四川省成都市武侯区人民南路4段12号",
    "湖北省武汉市武昌区中南路99号",
    "陕西省西安市雁塔区科技路5号",
    "福建省厦门市思明区湖滨南路20号",
    "山东省青岛市市南区香港中路8号",
]

#: 买家留言空着居多 —— 真实数据就是这样，也让「无异常」这条主路径占多数。
_BUYER_MESSAGES = [
    "",
    "",
    "",
    "孩子明天生日，希望今天发货",
    "不要发顺丰，请发中通",
    "麻烦发到公司前台",
]

#: 卖家备注偶尔带上「大额/需确认」这类会触发人工复核的提示（演示用）。
_SELLER_NOTES = [
    "",
    "",
    "",
    "",
    "大额订单，需财务确认",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成一批可导入的模拟订单 CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=20,
        help="生成多少行订单（默认 20）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出路径（默认为 mock/platform/out/orders_<时间戳>.csv）",
    )
    parser.add_argument(
        "--prefix",
        default="MOCK",
        help="订单号前缀（默认 MOCK）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子；给定后用同样的种子能造出同样的数据内容（单号仍随时间变化）",
    )
    return parser.parse_args()


def _make_phone(rng: random.Random) -> str:
    second = rng.choice(_PHONE_SECOND)
    rest = "".join(rng.choice("0123456789") for _ in range(9))
    return f"1{second}{rest}"


def _make_name(rng: random.Random) -> str:
    return rng.choice(_SURNAMES) + rng.choice(_GIVEN_NAMES)


def _rows(count: int, prefix: str, rng: random.Random) -> list[list[str]]:
    """造出 count 行订单。单号在本批内天然唯一（序号递增）。"""
    # 到毫秒，重跑一次几乎不可能落在同一毫秒；外加序号，本批内也唯一。
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
    base_time = datetime.now() - timedelta(days=1)
    skus = list(PRODUCTS)

    rows: list[list[str]] = []
    for seq in range(1, count + 1):
        sku = skus[(seq - 1) % len(skus)]  # 轮流取，保证七个 SKU 都出现
        name, unit_price = PRODUCTS[sku]
        quantity = rng.randint(1, 3)
        amount = f"{float(unit_price) * quantity:.2f}"
        ordered_at = (base_time + timedelta(minutes=seq * 7)).strftime("%Y-%m-%d %H:%M:%S")

        rows.append(
            [
                f"{prefix}{stamp}{seq:03d}",
                ordered_at,
                _make_name(rng),
                _make_phone(rng),
                rng.choice(_CITIES),
                name,
                sku,
                str(quantity),
                amount,
                rng.choice(_BUYER_MESSAGES),
                rng.choice(_SELLER_NOTES),
            ]
        )
    return rows


def _default_out_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUT_DIR / f"orders_{stamp}.csv"


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("--count 至少要 1")
        return 1

    out_path = (args.out or _default_out_path()).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    rows = _rows(args.count, args.prefix, rng)

    # newline="" 是 csv 模块的官方要求（不这样的话 Windows 上会多空行）；
    # lineterminator="\n" 对齐仓库里那份样例 CSV（csv.writer 默认是 \r\n）。
    # UTF-8 无 BOM —— 导入接口按 utf-8-sig 解，BOM 可有可无，但样例没有，照抄。
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(rows)

    print(f"已生成 {len(rows)} 行订单 → {out_path}")
    print()
    print("导入（管理员 JWT）：")
    print("  TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \\")
    print('    -H "Content-Type: application/json" \\')
    print("""    -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}' | python -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')""")
    print("  curl -s -X POST http://127.0.0.1:8000/api/v1/orders/import \\")
    print(f'    -H "Authorization: Bearer $TOKEN" -F "file=@{out_path}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
