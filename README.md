# 收盘之后 · A股盘后分析系统

本地优先的股票研究工具，面向持有几天至几周的 A 股观察。每天采集行情、检查数据、分析市场和行业、筛选可解释的观察信号，并记录后续表现。

## 本地启动

需要 Python 3.11+（推荐3.12，AKShare要求3.11以上）。Ubuntu 缺少 venv 时先安装对应版本的 `python3-venv`。只有旧版Python时可先执行 `uv python install 3.12` 和 `uv venv --seed --python 3.12 .venv`。

```bash
cd /home/vincent/code/ai_gaoqian
bash scripts/run_local.sh
```

打开 <http://127.0.0.1:8000>。脚本会启动网页和定时 worker；按 Ctrl+C 结束。首次创建的 `.env` 带有随机 `APP_API_KEY`，网页添加和移除自选股需要此密钥。首次真实数据初始化需在另一个终端执行下面的盘后采集命令。

服务已经运行时，直接打开网页查看即可。电脑重启或服务停止后，再执行上述启动命令；请保持启动终端开启，避免重复启动网页或 worker。启动脚本不会自动设置开机启动。

电脑需要开机、不休眠，并能联网。默认按北京时间17:30尝试盘后采集和分析、21:30补查公告；采集成功且 `EXPORT_HTML=true` 时，会自动更新 `data/html/baostock-latest.html`。保留 `.env` 和 `data/` 目录：已有行情和复权记录会复用，已经完成历史初始化的电脑无需从头采集。

如需收盘后手动更新一次，另开终端执行；默认分析北京时间当天，指定日期和故障处理见“本地真实运行一次”：

```bash
cd /home/vincent/code/ai_gaoqian
.venv/bin/python -m stock_daily run
```

**新配置默认使用免费 BaoStock 行情和 AKShare 公告，无需 Tushare Token。** 已有 `.env` 不会被启动脚本自动覆盖，需自行修改 `DATA_SOURCE`。免费模式覆盖 BaoStock 的沪深A股，不含北交所、B股、ETF和债券。页面明确显示数据来源和范围。

手动安装和运行：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m stock_daily run --date 20261009
.venv/bin/python -m stock_daily serve
```

另开终端运行 `.venv/bin/python -m stock_daily worker` 才会执行定时任务。手动复制配置时可自行设置 `APP_API_KEY`；没有设置密钥，网页只允许读取，自选股仍可用 CLI 管理。

## 本地真实运行一次

以下步骤适用于已经安装依赖的当前项目。保留已有 `.env`，确认 `DATA_SOURCE=baostock`，即可使用免费真实行情，无需 Tushare 积分。

### 1. 运行盘后分析

如果网页已经显示采集任务正在运行，等待该任务完成，不要重复启动。否则在终端执行：

```bash
cd /home/vincent/code/ai_gaoqian
.venv/bin/python -m stock_daily run --date 20261009
```

`20261009` 是示例日期，请替换为已经收盘的实际交易日。首次运行会补齐最近100个交易日，并初始化全市场复权记录，耗时较长；后续运行会利用已保存的历史数据。命令执行期间会在终端输出进度，完成后查看任务结果是否成功。

### 2. 查看进度和结果

网页服务已启动时，打开 <http://127.0.0.1:8000> 查看任务进度和报告。尚未启动时，另开终端执行：

```bash
cd /home/vincent/code/ai_gaoqian
.venv/bin/python -m stock_daily serve
```

对于此前已在后台启动、日志写入 `data/initial_free_run.log` 的首次采集任务，可这样查看日志：

```bash
cd /home/vincent/code/ai_gaoqian
tail -f data/initial_free_run.log
```

在这个日志查看终端按 Ctrl+C 只会退出 `tail`，后台采集会继续运行。直接运行分析命令的终端会显示自身日志，并不会默认写入该文件。

### 3. 打开本地 HTML

配置 `EXPORT_HTML=true` 时，分析成功后会自动更新 `data/html/baostock-latest.html`，可直接双击打开。若报告尚未发布，该文件可能只显示采集状态；报告生成后重新打开文件查看结果。

也可以在分析完成后手动导出：

```bash
cd /home/vincent/code/ai_gaoqian
.venv/bin/python -m stock_daily export-html
```

### 4. 再次运行

以后按第1步的命令替换日期即可。当天已有报告时会跳过重复分析；需要重新采集当天数据并替换当天报告与信号时使用：

```bash
.venv/bin/python -m stock_daily run --date 20261009 --force
```

`--force` 用于修正数据或重算，不用于并行启动正在运行的任务。

### 5. 进程结束但 HTML 没有数据

进程结束不代表任务成功。先查看网页上的失败原因，或此前后台任务的 `data/initial_free_run.log`。首次历史补齐失败时不会发布不完整报告，HTML 会显示失败状态；修复后的版本会自动刷新该状态。网络接收错误或超时后，可重新执行同一个日期的 `run` 命令继续，已经保存的历史和复权记录会复用，无需删除数据库或加 `--force`。

全市场接口较慢时，可在 `.env` 将 `FREE_REQUEST_TIMEOUT` 调整为180，再重新运行；支持1～300秒，网络请求和SDK总超时均使用该限制。以命令最终输出的 `"status": "success"` 和 `"report_ready": true` 确认报告生成；只有采集状态的 HTML 不包含完整行情报告。成功后重新打开 `data/html/baostock-latest.html`，如需补导出，执行 `export-html`。

批量接口持续超时时，若此前已保存过真实交易日快照，还可以使用个股历史接口恢复：

```bash
.venv/bin/python -m stock_daily run --date 20261009 --stock-history
```

恢复模式要求缓存基础信息的采集日期不早于报告日期，避免遗漏新上市股票。它通过最多两个独立连接逐只获取剩余窗口的未复权行情，总请求间隔仍遵守 `FREE_REQUEST_INTERVAL`；原始断点保存在 `data/history_cache/`，复用复权记录和已保存交易日。仍按原覆盖率、价格、基准和历史要求校验后才发布报告。停牌依据个股日线的交易状态，恢复模式和基础信息日期会显示在报告中。`--force` 会重新采集本次窗口的个股断点数据。当前恢复任务的日志可用 `tail -f data/resume_free_run.log` 查看。

## 免费真实行情与公告

在 `.env` 中配置：

```dotenv
DATA_SOURCE=baostock
ENABLE_ANNOUNCEMENTS=true
FREE_REQUEST_INTERVAL=0.3
FREE_REQUEST_TIMEOUT=90
```

运行采集，并在修改配置后重启网页和 worker：

```bash
.venv/bin/python -m stock_daily run
# 非交易日指定已经收盘的实际交易日期
.venv/bin/python -m stock_daily run --date 20261009
```

首次补齐最近100个交易日，并逐只初始化从上市日至报告日的复权记录，可能需要较长时间；每100只输出复权进度。复权记录保存在数据库 `provider_cache`，中断后可继续；后续按日期批量更新除权记录、行情和停牌状态。不会将“当日没有除权记录”误当成所有股票的复权因子为1。后复权因子用于原价×因子的指标计算，量和额直接采用接口的股、元单位。

大接口比单只股票接口慢。每个SDK操作有总超时，失败重连最多3次；可将 `FREE_REQUEST_TIMEOUT` 调至300秒以内。超时、空行情、日期不符、分页失败或覆盖不足都不会发布报告，也不会自动改用示例数据。免费接口不承诺可用性或全市场完整性，具体运行结果以任务记录为准。

AKShare的 `stock_notice_report` 获取指定日期的公告标题、链接和日期；仅采集当前报告日公告，历史行情补齐不逐日抓取公告。SDK操作也受总超时限制。接口没有精确发布时间，因此本系统留空该字段。启用后21:30补查；接口不可用显示“公告未核验”，不会宣称没有风险。标题提示不读取正文，也不判断利好利空。

只读连通性检查不会写入行情或报告：

```bash
PYTHONPATH=. .venv/bin/python scripts/check_free_data.py --date 20261009
# 额外检查全市场基础信息和批量日线，可能明显较慢
PYTHONPATH=. .venv/bin/python scripts/check_free_data.py --date 20261009 --bulk
```

检查单只股票成功仅代表对应接口可用，不能证明全市场初始化成功。报告发布仍须满足下面的数据质量校验。参考：[BaoStock](https://www.baostock.com/)、[AKShare公告接口](https://akshare.akfamily.xyz/data/stock/stock.html#id203)。

## 可选 Tushare

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

## 合成示例

在 `.env` 设置 `DATA_SOURCE=demo`，执行 `.venv/bin/python -m stock_daily demo` 并重启服务，可生成近30日示范报告。全部行情、公司名称、公告和工作日历都是合成数据；统计不能证明策略有效。切回 `baostock` 会读取独立的真实数据区。

## 本地 HTML 文件

无需服务器即可双击打开导出的单个HTML。样式、报告数据、候选股近65日图表和观察统计均内嵌，不使用CDN，也不发起行情或后台请求。搜索、筛选、详情图表和导航可离线操作；自选股管理在本地服务或CLI中完成，HTML为只读。Token和管理密钥不写入文件。

```bash
# 当前来源的最新复盘，默认输出 data/html/数据源-latest.html
.venv/bin/python -m stock_daily export-html
# 导出特定日期到指定文件；只使用截至该日的价格和观察统计
.venv/bin/python -m stock_daily export-html --date 20261009 --output data/html/my-report.html
# 预览完整示例界面，明确标记为合成数据；不修改主数据源
.venv/bin/python -m stock_daily export-html --source demo --output data/html/demo-preview.html
```

真实源尚未发布报告时，最新导出会明确显示采集状态，不会代入示例行情。指定日期没有报告会报错，并保留原输出文件。文件标注导出时间和数据日期，已打开的文件不会自动获取行情；更新后重新打开，或点击“重载文件”读取当前文件内容。

`.env` 设置 `EXPORT_HTML=true`（新配置默认开启）和 `HTML_REPORT_DIR=./data/html`，盘后报告与晚间公告更新成功后会自动生成 `数据源-YYYYMMDD.html` 和 `数据源-latest.html`。HTML写入失败会保留已发布的数据库报告，在任务记录提示，可通过CLI重试。修改配置后重启worker。

若首次采集已经在旧进程中运行，可另外等待它完成后导出：`PYTHONPATH=. .venv/bin/python scripts/export_when_ready.py --date 20261009`。该脚本期间更新状态快照，等待最长6小时；采集失败会显示失败状态，不生成伪造行情报告。

Tushare官方权限表列出的基础非复权日线门槛为120积分，100积分不足；提高到120也不意味着拥有本系统所需的复权、每日指标、基准和公告权限。主数据源可继续使用 `baostock`，Token留在 `.env` 中备用。[权限说明](https://tushare.pro/document/2?doc_id=290)

## 每日报告

- 市场：沪深300日涨跌、上涨比例、成交额相对此前20日均值、站上20日均线的样本占比。
- 行业：连续65日行情样本的等权5/20日表现、相对沪深300强度、当日上涨比例。**这是样本统计，不是官方行业指数。**
- 候选：放量突破、趋势回调转强，每条包含指标、依据、风险和收盘失效条件。
- 自选：20日均线跌破、单日明显下跌、放量下跌、标题风险关键词提醒。
- 跟踪：已发布信号后5/10/20个交易日的复权收盘变化、期间相对信号收盘的最大不利变化、首个收盘失效日。

规则版本 `swing-v1.0`：股票须有连续65个交易日数据，默认当日成交额≥5000万元；排除名称含ST或退市标记、BaoStock当日ST标记、最近20日复权因子变化的股票；缺失行业分类的股票不参与行业筛选。行业位于相对强度前1/3且跑赢沪深300。个股收盘>MA20>MA60且20日跑赢沪深300。

放量突破：收盘突破此前20日最高价，成交量≥此前20日均量的1.5倍。回调转强：前一日收盘在当时MA20的±3%以内，此前3日均量小于其前20日均量，当日收盘转强并高于MA20，MA20较5日前上升。阈值为待验证假设，没有做参数优化或交易优势证明。

价格分析采用原始价格×复权因子，展示时换回当前价格尺度；原始价格、成交量（股）、成交额（元）与复权因子单独保存。财务PE/PB可存储展示，本版本不使用财报因子选股。

**跟踪是收盘到收盘的观察，不是策略回测。** 尚未实现可成交的次日开盘入场、佣金、滑点、涨跌停、交易制度约束、仓位、组合最大回撤或样本外策略检验。停牌或某日缺数据时该观察区间标记缺失，不按“下一条可用行情”凑足交易日。重叠信号不是独立样本；仅统计最近最多200个已保存信号。

股票基础信息包含当前可取得的上市、退市和暂停上市股票，但历史行业分类、名称和风险警示状态并非完整的时点历史。手工生成过去日期报告会使用采集时的基础信息，不能作为无前视偏差的历史策略回测。原始行情历史修订目前需人工复核和重算。

## 数据质量和定时任务

行情须满足日期一致、代码唯一、数值有限、价格/量/额/复权因子为正、OHLC一致、复权齐备、基准存在。BaoStock PE/PB等可为空；Tushare须有对应每日指标记录。扣除全天停牌后默认覆盖率至少98%，未达到则拒绝发布。BaoStock覆盖率的分母是其基础信息中的沪深A股，不是包含北交所的全部A股。达到阈值但仍有缺失时明确显示“部分行情缺失”。可将 `MIN_COVERAGE=1` 设为严格完整。

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
# 首次初始化（非交易日用 --date 指定已收盘的交易日）
docker compose run --rm app python -m stock_daily run --date 20261009
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

测试涵盖复权、指标窗口、停牌区间、两种真实源的单位转换、复权缓存和增量更新、分页截断、SDK超时重连、防止重复运行、失败原子性、断点续采、来源隔离、写入鉴权与调度。自动化测试使用合成数据、HTTP mock和SDK替身，不等同于真实接口全量验证。实时连通性检查使用上述脚本。

目录：`providers.py` Tushare/示例源；`free_provider.py` 免费数据源；`sdk_client.py` SDK进程与超时；`pipeline.py` 采集校验发布；`analysis.py` 指标与信号；`service.py` 报告和观察视图；`web.py` API；`scheduler.py` 定时；`db.py` 存储；`static/` 和 `templates/` 页面。
