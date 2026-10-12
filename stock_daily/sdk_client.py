"""Bound uninterruptible third-party SDK calls with a disposable worker process."""

import contextlib
import logging
import multiprocessing
import os
import socket
import time

from .providers import DataError

logger = logging.getLogger(__name__)


def read_result(result, bulk=False):
    if result.error_code != "0":
        if result.error_code == "10002007":
            raise DataError("BaoStock 网络接收错误（错误码 10002007）")
        raise DataError(f"BaoStock 返回错误码 {result.error_code}")
    fields = result.fields
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields):
        raise DataError("BaoStock 返回字段无效")
    rows = []
    seen_pages = set()
    while True:
        page = result.data
        fingerprint = tuple(tuple(row) for row in page)
        if page and fingerprint in seen_pages:
            raise DataError("BaoStock 重复分页，拒绝截断数据")
        seen_pages.add(fingerprint)
        for row in page:
            if len(row) != len(fields):
                raise DataError("BaoStock 字段数量不一致")
            rows.append(dict(zip(fields, row)))
        # New bulk endpoints explicitly return the entire day in one page.
        if bulk:
            if len(page) >= 20000:
                raise DataError("BaoStock 批量接口达到保护上限，可能截断")
            break
        if len(page) < 2000:
            break
        previous = result.cur_page_num
        result.cur_row_num = len(page)
        more = result.next()
        if result.error_code != "0" or result.cur_page_num == previous:
            raise DataError("BaoStock 下一页获取失败，拒绝截断数据")
        if not more:
            break
        if len(rows) > 100000:
            raise DataError("BaoStock 超过分页保护上限")
    return rows


def sdk_worker(pipe, timeout):
    # Bulk daily responses can take over 30 seconds. Honor the configured limit;
    # the parent process also enforces it and disposes of a stalled worker.
    socket.setdefaulttimeout(timeout)
    logged_in = False
    # SDK progress bars and login messages must not contaminate CLI JSON output.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            while True:
                request = pipe.recv()
                if request is None:
                    break
                method, params = request
                try:
                    if method == "stock_notice_report":
                        import akshare

                        frame = akshare.stock_notice_report(**params)
                        expected = {"代码", "公告标题", "公告日期", "网址"}
                        if not expected.issubset(frame.columns):
                            raise DataError("AKShare 公告字段缺失，不能认定无公告")
                        rows = frame.to_dict("records")
                    else:
                        import baostock

                        if not logged_in:
                            login = baostock.login()
                            if login.error_code != "0":
                                raise DataError(f"BaoStock 登录失败，错误码 {login.error_code}")
                            logged_in = True
                        rows = read_result(
                            getattr(baostock, method)(**params),
                            bulk=method in {"query_daily_history_k_AStock", "query_daily_adjust_factor"},
                        )
                    pipe.send({"rows": rows})
                except Exception as error:
                    message = str(error) if isinstance(error, DataError) else f"SDK 异常：{type(error).__name__}"
                    pipe.send({"error": message})
        except (EOFError, BrokenPipeError):
            pass
        finally:
            if logged_in:
                baostock.logout()
            pipe.close()


class SDKClient:
    def __init__(self, settings, worker_target=sdk_worker):
        self.timeout = settings.free_timeout
        self.interval = settings.free_request_interval
        self.worker_target = worker_target
        self.process = None
        self.pipe = None
        self.last_request = 0

    def close(self, graceful=True):
        if graceful and self.pipe and self.process and self.process.is_alive():
            try:
                self.pipe.send(None)
                self.process.join(timeout=2)
            except (EOFError, BrokenPipeError, OSError):
                pass
        if self.pipe:
            self.pipe.close()
            self.pipe = None
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.process = None

    def query(self, method, **params):
        detail = "接口不可用"
        for attempt in range(3):
            try:
                if self.process is None:
                    context = multiprocessing.get_context("spawn")
                    self.pipe, child = context.Pipe()
                    self.process = context.Process(target=self.worker_target, args=(child, self.timeout), daemon=True)
                    self.process.start()
                    child.close()
                delay = self.interval - (time.monotonic() - self.last_request)
                if delay > 0:
                    time.sleep(delay)
                self.last_request = time.monotonic()
                self.pipe.send((method, params))
                if not self.pipe.poll(self.timeout):
                    raise DataError(f"超过 {self.timeout:g} 秒超时限制")
                response = self.pipe.recv()
                if "error" in response:
                    raise DataError(response["error"])
                return response["rows"]
            except (DataError, EOFError, BrokenPipeError, OSError) as error:
                detail = str(error) if isinstance(error, DataError) else "连接已中断"
                logger.warning("%s 第%s/3次请求失败：%s", method, attempt + 1, detail)
                self.close(graceful=False)  # A timed-out socket cannot be reused for the next request.
                if attempt < 2:
                    time.sleep(2**attempt)
        raise DataError(f"{method} 请求失败，已重连3次：{detail}；请稍后重试或检查网络")
