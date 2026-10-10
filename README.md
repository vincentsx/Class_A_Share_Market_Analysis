# 收盘之后 · A股盘后分析系统

本地优先的股票研究工具，面向持有几天至几周的 A 股观察。每天采集行情、检查数据、分析市场和行业、筛选可解释的观察信号，并记录后续表现。

## 本地启动

需要 Python 3.10+。Ubuntu 缺少 venv 时先安装 `python3-venv`。

```bash
bash scripts/run_local.sh
```

打开 <http://127.0.0.1:8000>。脚本会初始化合成示例、启动网页和定时 worker；按 Ctrl+C 结束。首次创建的 `.env` 带有随机 `APP_API_KEY`，网页添加和移除自选股需要此密钥。

**默认是合成示例模式。示例行情、公司名称、公告和工作日历都不是实际市场数据。** 示范近30日复盘，展示完整流程；示例信号的统计不能证明策略有效。

手动安装和运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m stock_daily demo
.venv/bin/python -m stock_daily serve
```

另开终端运行 `.venv/bin/python -m stock_daily worker` 才会执行定时任务。手动复制配置时可自行设置 `APP_API_KEY`；没有设置密钥，网页只允许读取，自选股仍可用 CLI 管理。

## 接入真实 A 股行情

在 `.env` 中修改：

```dotenv
DATA_SOURCE=tushare
TUSHARE_TOKEN=你的Token
ENABLE_ANNOUNCEMENTS=false
```

然后执行并重启网页、worker：

```bash
.venv/bin/python -m stock_daily run
# 非交易日或手工补采时指定实际交易日期
.venv/bin/python -m stock_daily run --date 20261009
```

首次运行自动初始化最近100个交易日；后续只补采缺失历史和当天数据。按日期批量拉取全市场、分页、限制频率，网络错误最多重试3次；首次全市场初始化可能需要数分钟到几十分钟，任务进度保存在数据库。重复执行不会重复写入；`--force` 重新采集当天并替换当天报告与信号，应仅用于修正数据或重算。历史快照不会在普通增量任务中自动覆盖。

真实数据模式需要 `stock_basic`、`trade_cal`、`daily`、`adj_factor`、`daily_basic`、`suspend_d` 和沪深300的 `index_daily` 权限。按账号确认实际权限和频率；接口异常不自动切换示例数据。默认 HTTPS，不自动降级为 HTTP。

公告使用 `anns_d`，需要单独权限。启用 `ENABLE_ANNOUNCEMENTS=true` 后会采集公告标题、日期、链接和可用的发布时间；未启用或接口失败时显示“公告未核验”。21:30补查公告只更新公告视图，保留原信号发布时的依据。此版本只按标题关键词提示，没有读取 PDF 正文，不判断事件的利好或利空方向。

接口参考：[日线](https://tushare.pro/document/2?doc_id=27)、[每日指标](https://tushare.pro/document/2?doc_id=32)、[股票列表](https://tushare.pro/document/2?doc_id=25)、[交易日历](https://tushare.pro/document/2?doc_id=26)、[停复牌](https://tushare.pro/document/2?doc_id=214)、[公告](https://tushare.pro/document/2?doc_id=176)、[HTTP API](https://tushare.pro/document/1?doc_id=40)。使用和展示数据时遵循数据提供方的授权范围。

## 每日报告

- 市场：沪深300日涨跌、上涨比例、成交额相对此前20日均值、站上20日均线的样本占比。
- 行业：连续65日行情样本的等权5/20日表现、相对沪深300强度、当日上涨比例。**这是样本统计，不是官方行业指数。**
- 候选：放量突破、趋势回调转强，每条包含指标、依据、风险和收盘失效条件。
- 自选：20日均线跌破、单日明显下跌、放量下跌、标题风险关键词提醒。
- 跟踪：已发布信号后5/10/20个交易日的复权收盘变化、期间相对信号收盘的最大不利变化、首个收盘失效日。

规则版本 `swing-v1.0`：股票须有连续65个交易日数据，默认当日成交额≥5000万元；排除名称含ST或退市标记、最近20日复权因子变化的股票；行业位于相对强度前1/3且跑赢沪深300。个股收盘>MA20>MA60且20日跑赢沪深300。

放量突破：收盘突破此前20日最高价，成交量≥此前20日均量的1.5倍。回调转强：前一日收盘在当时MA20的±3%以内，此前3日均量小于其前20日均量，当日收盘转强并高于MA20，MA20较5日前上升。阈值为待验证假设，没有做参数优化或交易优势证明。

价格分析采用原始价格×复权因子，展示时换回当前价格尺度；原始价格、成交量（股）、成交额（元）与复权因子单独保存。财务PE/PB可存储展示，本版本不使用财报因子选股。

**跟踪是收盘到收盘的观察，不是策略回测。** 尚未实现可成交的次日开盘入场、佣金、滑点、涨跌停、交易制度约束、仓位、组合最大回撤或样本外策略检验。停牌或某日缺数据时该观察区间标记缺失，不按“下一条可用行情”凑足交易日。重叠信号不是独立样本；仅统计最近最多200个已保存信号。

股票基础信息包含当前可取得的上市、退市和暂停上市股票，但历史行业分类、名称和风险警示状态并非完整的时点历史。手工生成过去日期报告会使用采集时的基础信息，不能作为无前视偏差的历史策略回测。原始行情历史修订目前需人工复核和重算。

## 数据质量和定时任务

行情须满足日期一致、代码唯一、数值有限、价格/量/额/复权因子为正、OHLC一致、复权和每日指标齐备、基准存在。扣除全天停牌后默认覆盖率至少98%，未达到则拒绝发布。达到阈值但仍有缺失时明确显示“部分行情缺失”。可将 `MIN_COVERAGE=1` 设为严格完整。

当天行情、元信息、报告和信号在同一数据库事务中发布；失败保留之前的已发布结果，成功采集的历史可用于断点续采。示例和真实数据按 `source` 完全隔离。自选股列表共用，切换数据源后会重新分析，示例添加的代码可自行移除。

worker 使用 `Asia/Shanghai`，默认17:30盘后任务、21:30公告补查（`SCHEDULE_TIME` / `ANNOUNCEMENT_TIME` 可改）。启动时补执行当天已到时间且未成功的任务，失败每15分钟重试，当天最多4次。非交易日记录跳过。**worker 停止时没有调度；电脑需开机且进程运行。** 停机数日后的下一次运行会补齐计算窗口内的行情，但不会自动生成停机期间的每一天报告。

数据库任务锁避免重复写入；进程异常退出后锁最长6小时过期。可配置 `ALERT_WEBHOOK`，任务失败后向该地址 POST 通用 JSON；默认关闭，失败也会记录在页面中。

## 自选股与 API

```bash
.venv/bin/python -m stock_daily watch add 600000.SH --note '关注回调后量价变化'
.venv/bin/python -m stock_daily watch list
.venv/bin/python -m stock_daily watch remove 600000.SH
.venv/bin/python -m stock_daily track
```

只读 API：`/health`、`/api/dashboard?day=YYYYMMDD`、`/api/report/{day}`、`/api/report/{day}/download`、`/api/stocks/{code}/history?day=YYYYMMDD`、`/api/tracking`、`/api/watchlist`。自选股 POST/DELETE 要求 `Authorization: Bearer APP_API_KEY`。网页不存储管理密钥。

## Docker 部署

先创建 `.env`。SQLite版无需单独数据库，数据保存在持久卷，默认仅绑定服务器回环地址：

```bash
docker compose build
# 示例模式首次初始化；真实模式换成 python -m stock_daily run
docker compose run --rm app python -m stock_daily demo
docker compose up -d
docker compose logs -f worker
```

可通过 SSH 转发查看：`ssh -L 8000:127.0.0.1:8000 用户@服务器`。需要对外访问时在反向代理增加 HTTPS 和访问认证；本版本没有用户账户和只读登录。自选股备注属于个人数据，因此部署配置没有默认公开端口。

全市场长期使用可换 PostgreSQL。在 `.env` 增加长的字母数字 `POSTGRES_PASSWORD`（如果包含URL保留字符，需正确编码数据库连接字符串）。叠加配置：

```bash
docker compose -f compose.yaml -f deploy/compose.postgres.yaml up -d --build
docker compose -f compose.yaml -f deploy/compose.postgres.yaml exec app python -m stock_daily run
```

SQLite和PostgreSQL数据库不会自动迁移数据。建议对持久数据库定期备份。SQLite备份用 Python `sqlite3.Connection.backup()` 或 SQLite `.backup`，不要在任务运行中只复制主DB文件而忽略WAL。PostgreSQL使用 `pg_dump`。

## 开发和验证

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check stock_daily tests
node --check stock_daily/static/app.js
```

测试涵盖复权、指标窗口、停牌区间、单位转换、分页、防止重复运行、失败原子性、断点续采、来源隔离、写入鉴权与调度。真实Tushare接口需要用户Token和权限，测试使用合成数据与HTTP mock。

目录：`providers.py` 数据源；`pipeline.py` 采集校验发布；`analysis.py` 指标与信号；`service.py` 报告和观察视图；`web.py` API；`scheduler.py` 定时；`db.py` 存储；`static/` 和 `templates/` 页面。
