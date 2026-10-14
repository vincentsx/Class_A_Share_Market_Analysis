"""Build a separate public website, then publish only its allowlisted files."""

import copy
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from .config import now
from .db import reports, snapshots
from .html_export import ASSETS, atomic_write, daily_payload, render_daily
from .intraday import render_snapshot
from .providers import DataError

logger = logging.getLogger(__name__)
VERSION = "public-pages-v1"
RELEASE = "pages-report-data"
SLOTS = {"portfolio": "模拟持仓 · 盘中估值", "midday": "午间观察", "close": "收盘观察", "evening": "日线分析"}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def public_snapshot(payload):
    result = copy.deepcopy(payload)
    result["focus"] = []
    result["public"] = True

    def clean(value):
        if isinstance(value, dict):
            for key in ("watched", "reason", "note"):
                value.pop(key, None)
            for item in value.values():
                clean(item)
        elif isinstance(value, list):
            for item in value:
                clean(item)

    clean(result)
    return result


def export_pages(db, settings, output="data/pages", render_image=None):
    """Use saved data only; a failed build leaves the previous site intact."""
    from .png_export import export_png

    renderer = render_image or export_png
    destination = Path(output).absolute()
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise DataError("公开网站输出必须是独立目录，不能是文件或符号链接")
    old = {}
    if destination.exists():
        try:
            old = json.loads((destination / "manifest.json").read_text())
        except (OSError, ValueError):
            if any(destination.iterdir()):
                raise DataError("输出目录已有其他文件，请为公开网站指定独立目录") from None
        if old and (old.get("version") != VERSION or old.get("source") != settings.source):
            raise DataError("该网站目录的来源或版本不匹配，请指定新输出目录")
    saved = db.rows(select(snapshots.c.payload).where(snapshots.c.source == settings.source)
                    .order_by(snapshots.c.day.desc()).limit(settings.pages_retain_days * 2))
    days = db.rows(select(reports.c.day).where(reports.c.source == settings.source)
                   .order_by(reports.c.day.desc()).limit(settings.pages_retain_days))
    dates = sorted({r["payload"]["day"] for r in saved} | {r["day"] for r in days}, reverse=True)[
        :settings.pages_retain_days]
    entries = []
    for record in saved:
        payload = record["payload"]
        if payload["day"] not in dates:
            continue
        payload = public_snapshot(payload)
        entries.append({"day": payload["day"], "slot": payload["slot"], "payload": payload,
                        "data_at": payload["captured_at"],
                        "news_at": payload.get("news", {}).get("as_of", "")})
    for record in days:
        if record["day"] not in dates:
            continue
        payload = daily_payload(db, settings, record["day"], public=True)
        report = payload["dashboard"]["report"]
        entries.append({"day": record["day"], "slot": "evening", "payload": payload,
                        "data_at": report.get("generated_at", ""), "news_at": ""})
    if days:
        payload = daily_payload(db, settings, days[0]["day"], public=True, portfolio=True)
        valuation = payload["backtest"].get("valuation", {})
        report = payload["dashboard"]["report"]
        portfolio_day = valuation.get("day", report["day"])
        if portfolio_day not in dates:
            dates = sorted({portfolio_day, *dates}, reverse=True)
        entries.append({"day": portfolio_day, "slot": "portfolio", "payload": payload,
                        "data_at": valuation.get("captured_at", report.get("generated_at", "")), "news_at": ""})
    entries.sort(key=lambda r: (r["day"], r["data_at"], {"midday": 0, "close": 1, "evening": 2,
                                                       "portfolio": 3}[r["slot"]]), reverse=True)
    if not entries:
        raise DataError("没有已发布报告；公开网站不会使用空页面或合成行情代替真实数据")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cached = {r["html"]: r for r in old.get("reports", [])}
    manifest = {"version": VERSION, "source": settings.source, "reports": [], "files": {}}
    asset_paths = sorted([*ASSETS.glob("templates/*.html"), *ASSETS.glob("static/*.css"),
                          *ASSETS.glob("static/*.js"), ASSETS / "static_report.py",
                          ASSETS / "html_export.py", ASSETS / "intraday.py", Path(__file__)])
    asset_signature = digest(b"".join(file.read_bytes() for file in asset_paths))
    with tempfile.TemporaryDirectory(prefix=".pages-build-", dir=destination.parent) as folder:
        stage = Path(folder) / "site"
        (stage / "reports").mkdir(parents=True)
        for entry in entries:
            payload = entry["payload"]
            fingerprint_payload = copy.deepcopy(payload)
            fingerprint_payload.pop("exported_at", None)
            fingerprint = digest(json.dumps([VERSION, asset_signature, fingerprint_payload], sort_keys=True,
                                            ensure_ascii=False, allow_nan=False).encode())
            name = f"reports/{settings.source}-{entry['day']}-{entry['slot']}.html"
            png_name = name.removesuffix(".html") + ".png"
            previous = cached.get(name, {})
            valid_cache = previous.get("fingerprint") == fingerprint and all(
                (destination / file).is_file() and not (destination / file).is_symlink()
                and digest((destination / file).read_bytes()) == old.get("files", {}).get(file)
                for file in (name, png_name))
            if valid_cache:
                for file in (name, png_name):
                    shutil.copyfile(destination / file, stage / file)
            else:
                logger.info("生成公开报告：%s %s", entry["day"], SLOTS[entry["slot"]])
                html = (render_daily(payload, settings) if entry["slot"] in {"evening", "portfolio"}
                        else render_snapshot(payload, settings))
                atomic_write(stage / name, html)
                renderer(stage / name)
            manifest["reports"].append({k: entry[k] for k in ("day", "slot", "data_at", "news_at")} | {
                "label": SLOTS[entry["slot"]], "html": name, "png": png_name, "fingerprint": fingerprint})
        signature = digest(json.dumps(manifest["reports"], sort_keys=True).encode())
        unchanged = signature == old.get("signature")
        manifest.update(signature=signature, built_at=old["built_at"] if unchanged else now().isoformat())
        env = Environment(loader=FileSystemLoader(ASSETS / "templates"), autoescape=select_autoescape(["html"]))
        latest = manifest["reports"][0]
        slot_reports = {slot: next((r for r in manifest["reports"] if r["slot"] == slot), None) for slot in SLOTS}
        atomic_write(stage / "index.html", env.get_template("pages.html").render(
            latest=latest, slots=slot_reports, dates=dates, reports=manifest["reports"], built_at=manifest["built_at"]))
        for alias, report in {"latest": latest, **slot_reports}.items():
            if report:
                # Controlled relative filenames work under a GitHub project subpath.
                atomic_write(stage / f"{alias}.html", '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                             f'<meta http-equiv="refresh" content="0;url={report["html"]}">'
                             f'<title>{report["label"]}</title><a href="{report["html"]}">打开报告</a></html>')
        atomic_write(stage / ".nojekyll", "")
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                manifest["files"][file.relative_to(stage).as_posix()] = digest(file.read_bytes())
        atomic_write(stage / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if sum(file.stat().st_size for file in stage.rglob("*") if file.is_file()) > 900 * 1024 * 1024:
            raise DataError("公开网站过大，请减少PAGES_RETAIN_DAYS")
        # Keep both trees on the same filesystem, with rollback if replacing fails.
        backup = Path(folder) / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    return {"status": "success", "path": str(destination), "reports": len(entries),
            "latest_day": latest["day"], "latest_slot": latest["slot"], "signature": signature}


def gh(*arguments, check=True):
    try:
        timeout = 420 if arguments[:2] == ("release", "upload") else 180
        result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise DataError("GitHub命令不可用或超时；请检查gh安装、登录及网络") from None
    if check and result.returncode:
        # Do not print authentication, account metadata, or raw upstream errors.
        raise DataError("GitHub操作未完成；请检查gh登录、仓库权限、Pages设置和pages.yml工作流")
    return result


def publish_pages(db, settings, repository, force=False):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise DataError("请用--repo或PAGES_REPOSITORY指定GitHub owner/repository")
    output = Path(settings.html_report_dir).absolute().parent / "pages"
    output.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with (output.parent / ".pages-publish.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        # Validate remote setup before spending time rendering/uploading reports.
        remote_page = json.loads(gh("api", f"repos/{repository}/pages").stdout)
        workflow = json.loads(gh("api", f"repos/{repository}/actions/workflows/pages.yml").stdout)
        if remote_page.get("build_type") != "workflow" or workflow.get("state") != "active":
            raise DataError("请在仓库Settings → Pages选择GitHub Actions，并启用pages.yml工作流")
        site = export_pages(db, settings, output)
        state_file = output.parent / ".pages-published.json"
        try:
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            state = {}
        if not force and state.get("repository") == repository and state.get("signature") == site["signature"]:
            # A dispatch is not a completed deployment. Check it before skipping retries.
            runs = json.loads(gh("run", "list", "--repo", repository, "--workflow", "pages.yml", "--limit", "1",
                                 "--json", "status,conclusion,createdAt").stdout)
            if runs and datetime.fromisoformat(runs[0]["createdAt"].replace("Z", "+00:00")) >= datetime.fromisoformat(
                    state["dispatched_at"]) - timedelta(seconds=5):
                run = runs[0]
                if run["status"] != "completed" or run["conclusion"] == "success":
                    return {**site, "status": "unchanged" if run["status"] == "completed" else "deploying",
                            "repository": repository}
        manifest = json.loads((output / "manifest.json").read_text())
        with tempfile.TemporaryDirectory(prefix="pages-upload-") as temporary:
            bundle = Path(temporary) / "site.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                for name in [*manifest["files"], "manifest.json"]:
                    path = output / name
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output.resolve()):
                        raise DataError("公开网站文件检查失败，未上传")
                    if name != "manifest.json" and digest(path.read_bytes()) != manifest["files"][name]:
                        raise DataError("公开网站文件在构建后变化，未上传")
                    archive.add(path, arcname=name, recursive=False)
            if gh("release", "view", RELEASE, "--repo", repository, check=False).returncode:
                gh("release", "create", RELEASE, "--repo", repository, "--target", "main", "--prerelease",
                   "--latest=false", "--title", "GitHub Pages 公开报告数据",
                   "--notes", "自动生成的公开报告网站数据，不含个人自选、备注、邮件记录或数据库。")
            gh("release", "upload", RELEASE, str(bundle), "--repo", repository, "--clobber")
        dispatched = now().isoformat()
        gh("workflow", "run", "pages.yml", "--repo", repository, "--ref", "main")
        atomic_write(state_file, json.dumps({"repository": repository, "signature": site["signature"],
                                            "dispatched_at": dispatched}))
    return {**site, "status": "dispatched", "repository": repository,
            "url": remote_page.get("html_url") or f"https://{repository.split('/')[0]}.github.io/{repository.split('/')[1]}/",
            "message": "已触发部署；是否上线请以GitHub Actions成功结果为准"}
