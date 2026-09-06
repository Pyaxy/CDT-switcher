"""One-shot, read-only Alibaba Cloud international account bill query.

Does not import the controller, poll Telegram, or persist runtime state.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys

import yaml
from alibabacloud_tea_openapi.client import Client
from alibabacloud_tea_openapi import models
from alibabacloud_tea_util.models import RuntimeOptions


# International billing is account-level; do not derive this from the ECS region.
ENDPOINT = "business.ap-southeast-1.aliyuncs.com"
BILL_TZ = timezone(timedelta(hours=8))
BILL_TYPES = {
    "SubscriptionOrder": "预付费",
    "PayAsYouGoBill": "按量付费",
    "Refund": "退款",
    "Adjustment": "调账",
}


class BillingError(Exception):
    """Safe user-facing error, without raw SDK requests or credentials."""


@dataclass(frozen=True)
class BillingAccount:
    key: str
    label: str
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)


@dataclass(frozen=True)
class BillRow:
    product: str
    kind: str
    currency: str
    pretax: Decimal


def display(value: object) -> str:
    """Keep API/config labels on one terminal line, without control characters."""
    return "".join(c if c.isprintable() else " " for c in str(value))[:160]


def month_arg(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError("月份必须为 YYYY-MM，例如 2026-09")
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("月份无效") from exc
    return value


def load_accounts(path: str, selected: str | None) -> list[BillingAccount]:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        # YAML parser exceptions can include secret-bearing source lines.
        raise BillingError("无法读取配置，请检查路径、文件权限和 YAML 格式") from exc
    raw = config.get("accounts") if isinstance(config, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise BillingError("配置必须包含非空 accounts 映射")
    if selected is not None:
        if selected not in raw:
            raise BillingError("指定账号键不存在，请核对 accounts 配置")
        raw = {selected: raw[selected]}
    accounts = []
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
            raise BillingError("accounts 中的账号键或配置格式无效")
        credentials = [entry.get("access_key_id"), entry.get("access_key_secret")]
        if any(not isinstance(v, str) or not v.strip() for v in credentials):
            raise BillingError(f"账号 {display(key)} 缺少有效的 AccessKey 配置")
        if any(v.startswith("YOUR_") for v in credentials):
            raise BillingError(f"账号 {display(key)} 仍使用示例凭据，请先填写真实配置")
        accounts.append(BillingAccount(
            key, str(entry.get("instance_name") or key), *credentials,
        ))
    return accounts


def safe_code(value: object) -> str:
    code = str(value or "")
    return code if re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", code) else "Unknown"


def query_overview(account: BillingAccount, month: str) -> list[BillRow]:
    client = Client(models.Config(
        access_key_id=account.access_key_id,
        access_key_secret=account.access_key_secret,
        endpoint=ENDPOINT,
    ))
    # The action is deliberately fixed: this module exposes no write API.
    params = models.Params(
        action="QueryBillOverview", version="2017-12-14", protocol="HTTPS",
        pathname="/", method="POST", auth_type="AK", style="RPC",
        req_body_type="formData", body_type="json",
    )
    try:
        response = client.call_api(
            params, models.OpenApiRequest(query={"BillingCycle": month}),
            RuntimeOptions(connect_timeout=5_000, read_timeout=10_000, autoretry=False),
        )
    except Exception as exc:
        code = safe_code(getattr(exc, "code", None))
        raise BillingError(
            f"账单请求失败（{code}）；请检查网络、国际站账号凭据及 "
            "bss:DescribeBillList 权限。未将失败计为 0 元。"
        ) from exc
    body = response.get("body") if isinstance(response, dict) else None
    if not isinstance(body, dict):
        raise BillingError("账单响应格式异常，无法确认金额")
    if body.get("Success") is not True or body.get("Code") != "Success":
        raise BillingError(f"账单查询未成功（{safe_code(body.get('Code'))}），无法确认金额")
    data = body.get("Data")
    if not isinstance(data, dict) or data.get("BillingCycle") != month:
        raise BillingError("账单数据缺失或返回月份不一致，无法确认金额")
    items = data.get("Items")
    rows = items.get("Item") if isinstance(items, dict) else None
    if not isinstance(rows, list):
        raise BillingError("账单明细结构异常，不能当作空账单处理")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise BillingError("账单包含无效明细，未输出不完整总额")
        currency = row.get("Currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise BillingError("账单币种缺失或无效，无法安全汇总")
        try:
            amount = Decimal(str(row["PretaxAmount"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise BillingError("税前金额缺失或无效，未将其计为 0") from exc
        if not amount.is_finite():
            raise BillingError("税前金额不是有限数值，无法安全汇总")
        result.append(BillRow(
            str(row.get("ProductName") or row.get("ProductCode") or "未命名产品"),
            str(row.get("Item") or "未知类型"), currency, amount,
        ))
    return result


def format_overview(rows: list[BillRow]) -> str:
    if not rows:
        return "  本月接口暂无账单条目（不代表未产生费用）。"
    grouped: dict[tuple[str, str, str], Decimal] = {}
    totals: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (row.currency, row.kind, row.product)
        grouped[key] = grouped.get(key, Decimal(0)) + row.pretax
        total_key = (row.currency, row.kind)
        totals[total_key] = totals.get(total_key, Decimal(0)) + row.pretax
    lines = []
    # Keep refunds/adjustments separate until their signed accounting semantics
    # have been reconciled with the user's console. Never sum across accounts.
    for (currency, kind), amount in sorted(totals.items()):
        title = BILL_TYPES.get(kind, display(kind))
        lines.append(f"  {title} · 税前金额小计：{currency} {amount:f}")
        for (ccy, typ, product), value in sorted(grouped.items()):
            if (ccy, typ) == (currency, kind):
                lines.append(f"    {display(product)}：{currency} {value:f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立只读查询阿里云国际站账户月账单")
    parser.add_argument("--config", default="config.yaml", help="配置路径，默认 config.yaml")
    parser.add_argument("--account", help="只查询指定 accounts 账号键；默认全部")
    parser.add_argument("--month", type=month_arg,
                        default=datetime.now(BILL_TZ).strftime("%Y-%m"),
                        help="账期 YYYY-MM；默认 UTC+8 当前月份")
    args = parser.parse_args(argv)
    try:
        accounts = load_accounts(args.config, args.account)
    except BillingError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    print(f"账单月份：{args.month} · 账户级查询（非单台 ECS 成本）", flush=True)
    print("账单存在延迟；当月金额尚未最终确认。税前金额不等于现金支付。", flush=True)
    failed = False
    for account in accounts:
        print(f"\n{display(account.label)} [{display(account.key)}] · 正在查询…", flush=True)
        try:
            print(format_overview(query_overview(account, args.month)), flush=True)
            stamp = datetime.now(BILL_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")
            print(f"  查询完成：{stamp}（不是计费数据截止时间）", flush=True)
        except BillingError as exc:
            failed = True
            print(f"  查询失败：{exc}", file=sys.stderr, flush=True)
        except Exception:
            failed = True
            print("  查询发生未预期错误，未输出原始异常以避免泄露凭据；金额未知。",
                  file=sys.stderr, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消账单查询。", file=sys.stderr)
        raise SystemExit(130)
