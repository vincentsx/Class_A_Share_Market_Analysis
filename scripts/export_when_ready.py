"""Export the in-progress first acquisition when it publishes its requested day."""

import argparse
import time

from sqlalchemy import select

from stock_daily.config import Settings
from stock_daily.db import Database, runs
from stock_daily.html_export import export_html, export_published
from stock_daily.providers import parse_day


def main():
    parser = argparse.ArgumentParser(description="等待正在运行的首次采集，完成后导出HTML")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    parse_day(args.date)
    settings = Settings.from_env()
    db = Database(settings.database_url)
    deadline = time.monotonic() + 6 * 3600
    while time.monotonic() < deadline:
        if db.latest_report(settings.source, args.date):
            print(export_published(db, settings, args.date), flush=True)
            return
        export_html(db, settings)  # Show an honest acquisition-status snapshot meanwhile.
        tasks = db.rows(select(runs).where(runs.c.source == settings.source, runs.c.day == args.date,
                                         runs.c.kind == "daily").order_by(runs.c.started_at.desc()).limit(1))
        if tasks and tasks[0]["status"] == "failed":
            print("采集失败，已导出失败状态；未生成行情报告。", flush=True)
            raise SystemExit(1)
        time.sleep(30)
    print("等待超过6小时，未生成行情报告。", flush=True)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
