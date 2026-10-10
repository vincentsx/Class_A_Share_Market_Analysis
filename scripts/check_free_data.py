"""Read-only live probes. Run with PYTHONPATH=. .venv/bin/python scripts/check_free_data.py."""

import argparse
import json
import time

from stock_daily.config import Settings
from stock_daily.free_provider import BaoStockProvider, iso
from stock_daily.providers import DataError, parse_day
from stock_daily.sdk_client import SDKClient


def main():
    parser = argparse.ArgumentParser(description="免费接口连通性检查，不写入报告或行情")
    parser.add_argument("--date", required=True, help="已收盘的实际交易日 YYYYMMDD")
    parser.add_argument("--bulk", action="store_true", help="额外检查全市场接口（数据量大、可能较慢）")
    args = parser.parse_args()
    parse_day(args.date)
    settings = Settings.from_env()
    client = SDKClient(settings)
    probes = [
        ("calendar", "query_trade_dates", {"start_date": iso(args.date), "end_date": iso(args.date)}),
        ("stock_basic", "query_stock_basic", {"code": "sh.600000"}),
        ("daily_sample", "query_history_k_data_plus", {
            "code": "sh.600000", "fields": "date,code,close,volume,amount,adjustflag,isST",
            "start_date": iso(args.date), "end_date": iso(args.date), "frequency": "d", "adjustflag": "3",
        }),
        ("adjustment", "query_adjust_factor", {
            "code": "sh.600000", "start_date": "1999-11-10", "end_date": iso(args.date),
        }),
    ]
    if args.bulk:
        probes.extend([
            ("stock_basic_all", "query_stock_basic", {}),
            ("daily_all", "query_daily_history_k_AStock", {"date": iso(args.date)}),
        ])
    failed = False
    try:
        for label, method, params in probes:
            start = time.monotonic()
            try:
                rows = client.query(method, **params)
                output = {"check": label, "status": "ok" if rows else "empty", "rows": len(rows)}
                if label == "daily_sample" and rows:
                    output["sample"] = rows[0]
                failed |= not bool(rows)
            except DataError as error:
                output = {"check": label, "status": "failed", "message": str(error)}
                failed = True
            output["seconds"] = round(time.monotonic() - start, 2)
            print(json.dumps(output, ensure_ascii=False), flush=True)
        if settings.announcements:
            provider = BaoStockProvider(settings, client=client)
            events, status = provider.events(args.date)
            print(json.dumps({"check": "announcements", "status": status, "rows": len(events)}, ensure_ascii=False), flush=True)
            failed |= status != "available"
    finally:
        client.close()
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
