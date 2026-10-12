import argparse
import json
import logging
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from .config import Settings, now
from .db import Database, watchlist
from .pipeline import refresh_events, run_daily
from .providers import DemoProvider, parse_day
from .scheduler import worker
from .service import tracking
from .web import validate_code


def main():
    parser = argparse.ArgumentParser(description="A股每日复盘与信号观察")
    commands = parser.add_subparsers(dest="command", required=True)
    daily = commands.add_parser("run", help="采集历史和当天行情，生成盘后报告")
    daily.add_argument("--date", help="YYYYMMDD，默认北京时间当天")
    daily.add_argument("--force", action="store_true", help="重新采集当天数据并替换该日报告及信号")
    daily.add_argument("--stock-history", action="store_true", help="BaoStock 批量接口异常时，用缓存基础信息和个股历史断点恢复")
    demo = commands.add_parser("demo", help="生成醒目标记的合成数据和近期报告")
    demo.add_argument("--date", help="示例结束日期，默认最近的工作日")
    demo.add_argument("--days", type=int, default=30)
    events = commands.add_parser("events", help="补查当天公告，不修改原始信号依据")
    events.add_argument("--date")
    serve = commands.add_parser("serve", help="启动本地报告页面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("worker", help="运行盘后及晚间定时任务")
    snapshot = commands.add_parser("snapshot", help="采集当天实时快照并导出HTML/PNG")
    snapshot.add_argument("--slot", choices=("midday", "close"), required=True)
    commands.add_parser("evening", help="执行完整日线分析、补查公告并导出HTML/PNG")
    commands.add_parser("track", help="输出已保存信号的观察统计")
    html = commands.add_parser("export-html", help="导出可双击打开的离线HTML报告")
    html.add_argument("--date", help="指定已有报告日期，默认最新报告或采集状态")
    html.add_argument("--output", help="输出HTML路径，默认 data/html/数据源-latest.html")
    html.add_argument("--source", choices=("baostock", "tushare", "demo"), help="只选择导出来源，不修改配置")
    png = commands.add_parser("export-png", help="将已有本地HTML导出为PNG概览长图")
    png.add_argument("--html", help="输入HTML，默认当前数据源的latest.html")
    png.add_argument("--output", help="输出PNG，默认与输入HTML同名")
    saved_snapshot = commands.add_parser("export-snapshot", help="重新导出已保存实时快照的HTML/PNG，不重新请求行情")
    saved_snapshot.add_argument("--date", help="YYYYMMDD，默认最新快照日期")
    saved_snapshot.add_argument("--slot", choices=("midday", "close"), help="指定快照时段")
    commands.add_parser("news", help="采集并留存免费滚动财经快讯，不发送邮件")
    news_refresh = commands.add_parser("news-refresh", help="为当天已保存快照补充新闻，明确标记补采时间，不重新请求行情或发信")
    news_refresh.add_argument("--date", help="YYYYMMDD，默认北京时间当天")
    news_refresh.add_argument("--slot", choices=("midday", "close"), default="close")
    mail_preview = commands.add_parser("mail-preview", help="生成带HTML和PNG附件的本地.eml邮件预览，不发信")
    mail_preview.add_argument("--date", help="YYYYMMDD，默认所选时段最新已发布日期")
    mail_preview.add_argument("--slot", choices=("midday", "close", "evening"), default="midday")
    mail_preview.add_argument("--output", default="data/mail-preview.eml")
    commands.add_parser("mail-setup", help="在本地交互填写Gmail配置，密码输入不回显，不发送邮件")
    mail_send = commands.add_parser("mail-send", help="发送已有HTML/PNG报告，不重新采集行情")
    mail_send.add_argument("--date")
    mail_send.add_argument("--slot", choices=("midday", "close", "evening"), default="midday")
    commands.add_parser("mail-status", help="查看脱敏后的邮箱配置检查和发信队列状态")
    mail_retry = commands.add_parser("mail-retry", help="重试指定失败邮件，不重新运行分析")
    mail_retry.add_argument("--id", required=True, help="mail-status显示的邮件ID")
    watch = commands.add_parser("watch", help="管理自选股")
    watch.add_argument("action", choices=("add", "remove", "list"))
    watch.add_argument("code", nargs="?")
    watch.add_argument("--note", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        if args.command == "demo":
            if settings.source != "demo":
                raise ValueError("demo 命令要求 DATA_SOURCE=demo，防止误切换真实数据")
            if not 1 <= args.days <= 90:
                raise ValueError("--days 应为1～90")
        db = Database(settings.database_url)
        if args.command == "run":
            if args.stock_history:
                from .history_recovery import make_recovery_provider

                provider = make_recovery_provider(db, settings, args.date, args.force)
                try:
                    result = run_daily(db, settings, args.date, args.force, provider=provider)
                finally:
                    provider.close()
            else:
                result = run_daily(db, settings, args.date, args.force)
        elif args.command == "demo":
            end = parse_day(args.date) if args.date else now().date()
            provider = DemoProvider(settings)
            start = (end - timedelta(days=args.days * 2 + 10)).strftime("%Y%m%d")
            dates = provider.calendar(start, end.strftime("%Y%m%d"))[-args.days :]
            for day in dates:
                run_daily(db, settings, day, provider=provider)
            for code in ("600000.SH", "600010.SH", "600031.SH"):
                if not db.rows(select(watchlist).where(watchlist.c.code == code)):
                    db.save_watch(code, "示例自选股")
            result = {
                "status": "success",
                "source": "demo",
                "last_day": dates[-1],
                "message": "合成示例报告已就绪；运行 stock-daily serve 查看",
            }
        elif args.command == "events":
            result = refresh_events(db, settings, args.date)
        elif args.command == "snapshot":
            from .intraday import run_snapshot

            result = run_snapshot(db, settings, slot=args.slot)
        elif args.command == "evening":
            from .scheduler import run_evening

            result = run_evening(db, settings, now().strftime("%Y%m%d"))
        elif args.command == "export-png":
            from pathlib import Path

            from .png_export import export_png

            result = export_png(args.html or Path(settings.html_report_dir) / f"{settings.source}-latest.html", args.output)
        elif args.command == "export-snapshot":
            from .intraday import export_saved_snapshot

            result = export_saved_snapshot(db, settings, args.date, args.slot)
        elif args.command == "news":
            from .news import collect_news

            result = collect_news(db, settings, force=True)
        elif args.command == "news-refresh":
            from .news import refresh_snapshot_news

            result = refresh_snapshot_news(db, settings, args.date, args.slot)
        elif args.command == "mail-setup":
            from .mailer import configure_gmail

            result = configure_gmail(settings)
        elif args.command == "mail-preview":
            from .mailer import preview_report

            result = preview_report(db, settings, args.date, args.slot, args.output)
        elif args.command == "mail-status":
            from .mailer import outbox_status

            result = outbox_status(db, settings)
        elif args.command == "mail-retry":
            from .mailer import retry_message

            result = retry_message(db, settings, args.id)
        elif args.command == "mail-send":
            from .db import mail_outbox
            from .mailer import enqueue_report, flush_outbox, require_configuration, saved_artifacts

            require_configuration(settings)
            day, html_path, png_path, details = saved_artifacts(db, settings, args.date, args.slot)
            queued = enqueue_report(db, settings, day, args.slot, html_path, png_path, details)
            delivery = flush_outbox(db, settings, identifier=queued['id'])
            record = db.rows(select(mail_outbox.c.status, mail_outbox.c.last_error)
                             .where(mail_outbox.c.id == queued['id']))[0]
            result = {"id": queued['id'], **record, "delivery": delivery}
            if record['status'] == 'failed':
                raise RuntimeError(record['last_error'])
        elif args.command == "worker":
            worker(db, settings)
            return
        elif args.command == "serve":
            import uvicorn

            from .web import create_app

            uvicorn.run(create_app(settings, db), host=args.host, port=args.port)
            return
        elif args.command == "track":
            result = tracking(db, settings)
        elif args.command == "export-html":
            from dataclasses import replace

            from .html_export import export_html

            if args.source:
                settings = replace(settings, source=args.source)
            result = export_html(db, settings, args.date, args.output)
        else:
            if args.action == "list":
                result = db.rows(select(watchlist))
            else:
                if not args.code:
                    raise ValueError("请提供股票代码")
                validate_code(args.code)
                if args.action == "add":
                    db.save_watch(args.code, args.note)
                else:
                    with db.engine.begin() as connection:
                        connection.execute(delete(watchlist).where(watchlist.c.code == args.code))
                result = {"status": "success", "code": args.code}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        logging.warning("操作已中断；已保存的数据保留")
        sys.exit(130)
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "程序异常，请查看任务记录"
        logging.error("%s", message)
        sys.exit(1)


if __name__ == "__main__":
    main()
