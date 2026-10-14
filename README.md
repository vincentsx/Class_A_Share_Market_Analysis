# 收盘之后 · A股研究与模拟交易系统

本地优先的 A 股研究与模拟交易工具，面向持有几天至几周的交易研究。支持午间与收盘观察、日线分析、信号跟踪、模拟买卖明细、盘中持仓估值和下一交易日模拟交易计划，帮助核对决策依据与后续结果。

## 在线展示

**[打开展示网页 →](https://vincentsx.github.io/Class_A_Share_Market_Analysis/)**

[模拟持仓 · 盘中估值](https://vincentsx.github.io/Class_A_Share_Market_Analysis/portfolio.html) · [最新报告](https://vincentsx.github.io/Class_A_Share_Market_Analysis/latest.html) · [午间快照](https://vincentsx.github.io/Class_A_Share_Market_Analysis/midday.html) · [收盘快照](https://vincentsx.github.io/Class_A_Share_Market_Analysis/close.html) · [日线研究与模拟持仓](https://vincentsx.github.io/Class_A_Share_Market_Analysis/evening.html)

无需本地安装，手机和电脑均可查看已发布报告、历史归档，以及下载 HTML / PNG。日线报告包含下一交易日模拟交易计划、信号跟踪、模拟买卖明细和持仓资金概览；数据日期与估值截止时间以页面标注为准。

## 研究工作台的新功能

- **完整信号存档**：`MAX_CANDIDATES`只控制优先展示数量；所有规则命中都写入报告与信号表。页面可切换完整名单，搜索会检索全部已存候选。旧版遗漏可按冻结指标严格核对后重建；重建候选单独标记、单独统计，并从模拟成交中排除，不冒充当时已经发布的信号。
- **每日变化清单**：只对比上一交易日的已发布报告，区分新增、持续满足、不再入选、触发原失效条件与无法判定。缺少前日报告不推断变化；旧版截断名单的比较标为“较已存名单新增”。晚间汇总当天真实保存的午间与收盘观察。
- **自选观察计划**：记录关注理由、待验证事项、观察价和失效价；可选择均线、明显下跌、放量下跌和价格条件。计划生效后的日线建立基线，之后只突出新触发或解除的条件；重复状态不生成新的变化提醒。修改晚于历史报告的计划不回填该报告。状态提醒随报告阅读，不另行发送消息。
- **规则效果看板**：按策略、版本分别展示5/10/20交易日均值、中位数、25%/75%分位、分布、失效占比和同期沪深300超额变化。支持全部已存命中、优先展示与同股票/策略/版本20交易日冷却样本；冷却样本仍可能相关。少于30条完整观察标记样本不足。统计最近最多10000条，明细展示200条，展示统计截止日期与缺失数。
- **运行健康**：worker独立心跳在长任务期间持续更新，90秒未更新标记过期；展示采集、导出、邮件、公开网站阶段与备份状态。保存日历不足时，下一时段显示为“检查时间，交易日历待核验”。新版worker使用进程锁，避免同数据库重复启动。
- **事件证据**：展示原始公告或留存快讯、发布时间、首次收录、出处和待核实事项。价格观察从首次发现之后的收盘建立基线，再比较后续1/5交易日；缺数据或观察期不足留空，不推断新闻导致涨跌。报告固定证据截止时点，普通重导出不添加后来才发现的消息。
- **模拟成交研究**：与收盘观察统计分开。展示初始资金、当前持仓市值、可用资金和盈亏金额；总资产=持仓市值+可用资金，盈亏金额=总资产−初始资金，已计入发生的费用。金额按实际计算截止日估值，不使用收益率显示值倒算。下一交易日开盘入场，失效或持有期届满后的下一开盘退出，买入后最早下一交易日卖出。支持资金、权重、持仓上限、持有期、费用、滑点和以前一日成交量为基准的参与上限；同股票持仓期间合并重叠信号。事后生成或缺少发布时间的信号排除，开盘接近涨跌停保守不成交，退出受限时延期。持仓无行情且未确认停牌，或发生缺少分红送转细节的复权变化时停止净值计算，明确显示实际计算截止日。

本地页面默认每30秒刷新并跟随最新报告；选择某个日期后固定在该日，信号跟踪与模拟均截止该日。导出的HTML内嵌默认参数模拟结果，离线文件不能重新模拟或调用模型；邮件附件禁用脚本时也能阅读变化清单、策略统计、计划、证据和模拟概览。

### 模拟交易决策明细

日线分析增加“下一交易日模拟交易计划”，使用默认模拟参数列出待卖出、待买入、继续持有及仅观察的完整名单。参考买卖价按本日收盘价加减滑点测算，参考股数、支出和回款不代表委托或成交；买入优先级、仓位、费用与前一日量能限制沿用模拟规则。依赖卖出成功腾出资金或仓位的买入单独标注；次日开盘跳空、停牌、涨跌停、复权或流动性限制可能导致数量变化或不成交。

新日线报告在行情、信号与报告发布的同一事务中冻结计划，盘中估值和重导出不修改它。下一交易日以供应商核验并保存的完整交易日历确定，日历不足时不猜测日期。旧报告缺少计划时只按报告截止日的数据补算，明确标为“非当时预先发布的计划”；晚于目标开盘生成的报告计划仅供回放核对。计划指向报告日的下一交易日，不自动将旧报告计划解释为当前日期的明天。

“模拟持仓”页面按笔展示买入时间、买入价格、卖出时间、卖出价格、股数、已发生费用与扣费盈亏，当前持仓优先、已卖出交易按最近卖出日期排序。持仓另列最近估值价及其行情时间；未卖出不填卖出价。可按名称、代码或策略搜索、筛选当前持仓或已卖出，并将当前筛选明细导出为 CSV；离线报告也可查看和导出。

展开每笔的依据，可核对原信号日期、发布时间、规则版本、原始买入理由、开盘原价与滑点模拟价、失效参考价、退出触发日和逐日延期原因。买卖时间是北京时间的交易日期与“开盘模拟”时段，没有实际成交分钟记录；模拟价格不代表真实成交价。失效按收盘判断，之后下一可模拟开盘退出，不是盘中触价立即止损；持有交易日数按已核验日线统计，盘中估值不会提前确认新买卖或增加持有天数。

已卖出盈亏扣除双边费用，未卖出浮盈亏仅扣买入费用；已实现盈亏加持仓浮盈亏等于账户总盈亏。费用、滑点和失效位置仍是模型假设。邮件及禁用脚本的 HTML 阅读也保留买卖明细；旧导出文件需重新导出才能看到新版展示。

### 自动备份与健康检查

```dotenv
BACKUP_ENABLED=true
BACKUP_DIR=./data/backups
BACKUP_RETAIN_DAYS=7
```

worker每天首次检查成功生成一份备份，每15分钟检查失败重试，默认保留7天。SQLite使用一致性备份API并校验完整性；PostgreSQL使用`pg_dump --format=custom`，运行环境需安装与服务端兼容的客户端。备份文件仅当前系统账号可读，包含个人自选与邮件记录，不进入公开网站。备份仍在本机，异机备份需另行配置。新增数据表在启动时创建，现有行情、报告与信号保留。

```bash
.venv/bin/python -m stock_daily health
.venv/bin/python -m stock_daily backup
# 先自动备份，再核对旧报告冻结指标、命中数、排序和原始依据；不联网、不发信
.venv/bin/python -m stock_daily repair-archive
.venv/bin/python -m stock_daily backtest --strategy breakout --holding-days 10 --date 20261012
# 可选前后段对比；分界日期必须在模拟日期范围内
.venv/bin/python -m stock_daily backtest --split-day 20261009
```

`repair-archive`仅处理匹配`swing-v1.0`且确实截断的旧报告。原始行情、优先候选与已发布信号依据保持不变，补充候选记录重建时间并单独列组。冻结指标、命中总数、排序或原始依据不一致时拒绝重建；不会使用后来行情重算历史指标。旧版未保存的成交额门槛按原v1默认5000万元尝试，所有核对均通过才写入。

模拟界面可修改费用，单位“基点”为0.01%；默认值仅为研究假设，不代表券商报价或历史统一费率。支持2023-04-10之后普通沪深主板、创业板与科创板规则候选，排除风险标记和不支持板块；科创板保守按200股整数申报，其余按100股。参考[上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)与[深交所创业板涨跌幅说明](https://www.szse.cn/www/investor/index/update/t20200729_580056.html)。日线无法验证撮合、队列与冲击成本，历史行业/名称/风险状态及行情修订也不完全具有时点性；该模拟不能当作无偏策略验证。前后段对比不是预先留出的样本外检验，不自动调参。

### 可选AI证据摘要

```dotenv
AI_API_KEY=
AI_API_BASE_URL=https://api.openai.com/v1
AI_API_STYLE=responses
AI_MODEL=
```

配置账号可用的模型，重启服务后，通过网页“生成AI摘要”或`.venv/bin/python -m stock_daily evidence-summary --date YYYYMMDD`手动调用。网页调用需要管理密钥。官方Responses使用结构化输出和`store=false`；兼容服务使用`AI_API_STYLE=chat_completions`，将JSON模式输出交给本地严格校验。`AI_API_BASE_URL`填写HTTPS接口根地址，通常以`/v1`结尾。旧版`OPENAI_API_KEY`仍兼容，`AI_API_KEY`非空时优先使用新配置。

例如本地配置的第三方服务可使用`AI_API_BASE_URL=https://aiapi.suunto.cn/v1`、`AI_API_STYLE=chat_completions`、`AI_MODEL=deepseek-v4-pro`，密钥仅保存在`.env`。每次最多发送20条已留存公开证据，不发送自选备注、观察计划或邮箱配置。相同证据、接口地址、接口格式、模型和提示版本复用本地缓存，引用ID不在输入证据中或输出截断时拒绝保存。AI转述仍需核对原文，引用存在不等于结论已独立核实；传闻不自动升级为已证实事实。不配置模型仍可阅读所有证据。官方接口实现参考[OpenAI结构化输出文档](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)。

本机当前网页和worker由用户级临时服务`stock-daily-web`、`stock-daily-worker`运行。修改`.env`后可执行`systemctl --user restart stock-daily-web stock-daily-worker`让配置生效；运行时直接打开网页，避免重复启动脚本。临时服务不会设置开机启动，重启电脑后仍按下面的“本地启动”操作。

## 本地启动

需要 Python 3.11+（推荐3.12，AKShare要求3.11以上）。Ubuntu 缺少 venv 时先安装对应版本的 `python3-venv`。只有旧版Python时可先执行 `uv python install 3.12` 和 `uv venv --seed --python 3.12 .venv`。

```bash
cd /home/vincent/code/ai_gaoqian
bash scripts/run_local.sh
```

打开 <http://127.0.0.1:8000>。脚本会启动网页和定时 worker；按 Ctrl+C 结束。首次创建的 `.env` 带有随机 `APP_API_KEY`，网页添加和移除自选股需要此密钥。首次真实数据初始化需在另一个终端执行下面的盘后采集命令。

服务已经运行时，直接打开网页查看即可。电脑重启或服务停止后，再执行上述启动命令；请保持启动终端开启，避免重复启动网页或 worker。启动脚本不会自动设置开机启动。

电脑需要开机、不休眠，并能联网。默认按北京时间 **11:35、15:05、18:30** 执行三个独立任务，成功后自动导出 HTML 与 PNG，并更新 `data/html/baostock-latest.html` 和 `data/html/baostock-latest.png`。保留 `.env` 和 `data/` 目录：已有行情和复权记录会复用，已经完成历史初始化的电脑无需从头采集。

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
.venv/bin/python -m playwright install chromium
cp .env.example .env
.venv/bin/python -m stock_daily run --date 20261009
.venv/bin/python -m stock_daily serve
```

另开终端运行 `.venv/bin/python -m stock_daily worker` 才会执行定时任务。手动复制配置时可自行设置 `APP_API_KEY`；没有设置密钥，网页只允许读取，自选股仍可用 CLI 管理。

## 每天三次运行与 HTML / PNG

| 北京时间 | 工作内容 | 保存文件（以20261012为例） |
| --- | --- | --- |
| 11:35 | 免费新浪午间报价；追踪行业热点、午间量价异动及自选/既有信号变化 | `baostock-20261012-1135.html`、`.png` |
| 15:05 | 免费新浪收盘报价；追踪热点延续、全天放缩量、午后走强/降温及风险异动 | `baostock-20261012-1505.html`、`.png` |
| 18:30 | BaoStock完整日线、复权和历史指标分析，公告按配置核验，发布收盘规则信号 | `baostock-20261012-1830.html`、`.png`，同时保存 `baostock-20261012.html/.png` |

三次成功导出后均更新 `data/html/baostock-latest.html` 和 `baostock-latest.png`；带日期及时分的文件分别保留。任务时间是**开始采集时间**，全量采集、分析和图片渲染完成后才更新文件。网页上方提供最新HTML和PNG入口；下方日线工作台继续显示已核验的日线报告及其真实日期。

午间和15:05快照使用免费新浪实时接口，无需Tushare积分；保留供应商每只股票的报价日期和时间。旧行情、错时段、无成交和无效报价不计入统计；有效报价须覆盖至少98%的缓存股票范围。范围来自此前日线采集的沪深A股基础信息，不含北交所，超过7天的缓存拒绝使用；可能遗漏缓存之后新上市的股票，因此不会声称实时统计覆盖全部A股。新浪接口口径可参考[AKShare官方实时行情说明](https://akshare.akfamily.xyz/data/stock/stock.html)。

### 午间与收盘的热点、量价观察

实时报告优先展示观察要点、热点追踪、量价异动和既有观察名单，全部报价及涨幅排名收纳到可展开的搜索区。

- **热点追踪**：按缓存行业分类统计平均涨跌、上涨家数、成交额占比及代表性上涨样本。至少5只样本、平均涨幅≥1%、上涨家数≥60%，且跑赢当时沪深300的行业标为“活跃”。与上一交易日同时间快照比较，区分新近活跃、持续活跃和热度回落；历史快照不足时不推断持续性。当前不包含概念题材识别，成交额占比不代表资金净流入。
- **放量与缩量**：收盘累计量比较此前20个交易日全天均量；午间累计量比较此前5～20个连续交易日已保存的午间均量。≥1.5倍为放量，≤0.7倍为缩量，并结合涨跌区分放量上涨、下跌、横盘和缩量回调、上涨。午间历史不足时不判断缩量；若上午量已经达到历史全天均量1.5倍，只给出这个已发生的量能事实，不外推全天量。
- **关键位置与风险**：展示截至前一交易日的20日均线、20日高点及当前价格偏离；提示冲高回落（最高价至当前价回落≥昨收的3%）、跌破均线参考和价格偏离风险。缺少连续20日历史、近期复权变化或供应商昨收与历史收盘不一致时，量价参考明确显示不可用。异动清单排除ST/退市等风险标记及成交额不足的股票；全量报价仍保留。
- **收盘追踪午后变化**：使用当天真实保存的午间快照，统计个股与行业相对午间的价格变化、午后新增成交额和交易参与度。累计量额回退的报价不参与比较；行业可比样本覆盖不足98%时不显示其午后比较。没有午间快照时不会补造午后走势。

昨日行业比较要求两日样本重合至少98%，成交额占比变化还要求市场样本重合至少98%；午后成交额占比以有效可比股票为分母。连续活跃仅计算连续已保存的同时间快照。所有阈值为可解释的观察规则，不是经过验证的买卖信号。HTML可切换放量、缩量、风险清单；PNG保存新闻主题概览、热点，分别完整展示已保存的放量、缩量、风险优先清单，并展开涨幅排序前40只报价及数据口径；其他报价在HTML搜索。各清单标明满足条件总数与保存/展示条数。

旧版已保存快照可用 `export-snapshot --date YYYYMMDD --slot midday` 或 `--slot close` 重导出为新版报告，无需联网重采；保留原始报价、采集时间和观察名单，参考价格窗口仍只取报告日之前的数据。新版采集会保存当时的完整分析，之后重导出直接复用，不随后续行情变化。

午间累计成交量不会外推成全天成交量，不用于放量突破判定。两次实时快照保存到独立表，不写入日线行情、正式信号或5/10/20日观察统计。18:30的BaoStock日线需另行核验；接口失败会记录失败，保留先前成功快照。缓存基础信息在报告当日已采集时，晚间任务可使用经过校验的个股历史恢复路径；否则使用常规接口重新获取范围，不绕过基础信息日期要求。合成demo模式跳过真实报价快照，仅运行晚间合成日线任务。

现有 `.env` 需要使用以下配置，修改后重启网页和worker（此电脑的配置已更新）：

```dotenv
MIDDAY_TIME=11:35
SCHEDULE_TIME=15:05
ANNOUNCEMENT_TIME=18:30
PORTFOLIO_INTERVAL_MINUTES=10
EXPORT_HTML=true
EXPORT_PNG=true
HTML_REPORT_DIR=./data/html
```

PNG由本地Chromium渲染独立HTML。启动脚本自动检查并安装浏览器；手动安装可执行：

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
# Ubuntu若提示缺少浏览器系统库，执行（会使用sudo安装系统依赖）
.venv/bin/python -m playwright install-deps chromium
```

Docker镜像也包含Chromium及中文字体。初次安装浏览器需要联网；HTML及PNG生成后可离线查看和分享。

手动测试或重新导出：

```bash
# 当天午间执行；13:00后禁止用实时接口补造午间行情
.venv/bin/python -m stock_daily snapshot --slot midday
# 当天收盘后执行
.venv/bin/python -m stock_daily snapshot --slot close
# 数据源已提供完整日线后执行，通常留给18:30定时任务
.venv/bin/python -m stock_daily evening
# 将最新HTML重新生成同名PNG，不重新请求行情
.venv/bin/python -m stock_daily export-png
# 指定已有HTML与图片路径
.venv/bin/python -m stock_daily export-png --html data/html/baostock-20261012-1135.html --output data/html/midday.png
# 恢复图片导出失败的已保存午间快照，任意时间都可重导出
.venv/bin/python -m stock_daily export-snapshot --date 20261012 --slot midday
```

旧版实时HTML没有图片展开功能时，先用 `export-snapshot` 更新HTML与PNG；直接对旧版HTML执行 `export-png` 会提示重导出并保留原PNG。

重新导出指定历史快照会把它设为 `latest`，请以文件内日期和时间为准。采集成功而图片导出失败时，原数据仍保留；安装浏览器或修复写入权限后，用上述导出命令重试。电脑仍需保持开机、不休眠、联网，worker持续运行；文件本身不会执行定时任务。

## 热点新闻追踪

真实数据源默认启用免费东方财富快讯与财联社电报，无需新闻Token。worker在北京时间08:00～21:00的空闲循环中每10分钟采集并留存一次；行情任务执行期间可能推迟采集，午间和收盘快照将同期新闻一起保存到报告。两家接口分别只返回最近200条和20条快讯，具体口径见[AKShare新闻接口文档](https://akshare.akfamily.xyz/data/stock/stock.html)；停机期间、采集间隔内或供应商返回范围之外的新闻可能遗漏，不承诺全天完整。

```dotenv
ENABLE_NEWS=true
NEWS_INTERVAL_MINUTES=10
NEWS_TIMEOUT=8
```

已有 `.env` 不包含这些项时使用上述默认值；修改配置后重启worker。`NEWS_INTERVAL_MINUTES`支持5～60分钟，`NEWS_TIMEOUT`支持1～20秒。新闻功能与公告开关独立；合成demo不采集或混入真实新闻。新闻接口失败保留之前收录，报告显示不可用、部分来源可用或采集记录较早，行情任务继续运行。

报告新增“热点新闻与行情对照”：

- **主题追踪**：人工智能、半导体、机器人、新能源、电力、医药、传媒、金融、地产、消费、能源等主题按关键词归类。统计过去24小时已留存报道的同日同标题去重条数；收录渠道数量不等于独立核实，提及数不代表全网热度。
- **对照行情**：主题关键词只映射到宽泛的行业观察范围；个股仅列出快讯直接提及的完整名称或代码，并展示快照中的涨跌和可比量能。优先显示与自选/既有观察名单、行业活跃情况相关的快讯。关键词关联不能证明公司业务属于该题材，也不能证明新闻导致涨跌。
- **时间与核对**：保留新闻发布时间、来源链接和首次采集时间，传闻、澄清、风险事项单独标记；行情复盘类报道单独分类，不当作独立催化事件。规则归类不判断利好利空，不替代正文和原始公告核对。
- **午间到收盘**：有同期午间新闻记录时显示“较午间新增收录”，用全部已跟踪新闻ID比较，避免把午间因展示条数限制而未显示的消息误当新增；不是“午后新发布”的同义词。没有同期基线时不推断新增。

每个新闻版本的首次采集时间保持不变，报告只使用截止时点已经发布且已被本地采集的版本。普通 `export-snapshot` 不联网取新闻，也不把之后才收录的内容加回旧报告。已保存的新闻分析与行情一起固定，后续重导出复用。

手动留存当前快讯，或为**当天已有快照**显式补充新消息：

```bash
.venv/bin/python -m stock_daily news
.venv/bin/python -m stock_daily news-refresh --date 20261012 --slot close
```

`news`仅采集；`news-refresh`补充新闻并更新该快照的HTML/PNG与latest，不重新请求行情，也不排队或发送邮件。补充报告保留原行情时间和价格，并明确显示较晚的新闻采集截止时间；晚于行情的消息标记“等待后续快照观察”，不能用旧行情判断消息后的反应。补充采集的午间新闻不能作为原始同期午间基线；不允许用当前新闻改写历史日期报告。

HTML保存最多200条优先快讯供筛选与搜索，浏览器默认展示前8条，可展开当前筛选的前25条；手机附件预览直接显示前8条，其余已保存快讯在原生展开区，阅读数据无需执行脚本；PNG保存主题卡片与默认新闻概览，并展示三类量价观察和前40只报价。全文及原始链接需联网核对，报告本身的展示与筛选可离线使用。

## Gmail自动发送报告邮件

邮件功能默认关闭。配置完成并重启worker后，11:35、15:05、18:30任务成功生成HTML和PNG时，各自排入一封报告邮件，再由worker发送。任务时间是开始采集时间，邮件会在采集与导出完成后发送。分析或导出失败不会发送尚未完成的报告；邮箱配置、认证或连接失败不会使已经生成的分析结果失效。

### 配置 Gmail

1. 在Google账号开启“两步验证”，然后[创建应用专用密码](https://myaccount.google.com/apppasswords)，名称可填“股票报告”。本功能使用应用专用密码；账号登录密码不适用。部分组织账号、仅安全密钥的两步验证或高级保护账号可能没有此入口，具体参见[Google官方说明](https://support.google.com/accounts/answer/185833)。当前实现使用SMTP授权，不包含Google OAuth登录。
2. 在项目终端执行本地配置命令，输入发件Gmail地址、收件地址和应用专用密码。收件地址留空时使用发件地址。密码输入不显示，直接保存到权限为600的本地 `.env`，不会写进报告、日志或Git：

```bash
cd /home/vincent/code/ai_gaoqian
.venv/bin/python -m stock_daily mail-setup
```

该命令只保存配置，不发送邮件；自动填写Gmail SMTP SSL连接。也可以手动在 `.env` 中设置：

```dotenv
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USERNAME=你的Gmail地址
SMTP_PASSWORD=你的16字符应用专用密码
MAIL_TO=你的收件邮箱地址
SMTP_TIMEOUT=20
MAIL_MAX_MB=20
EXPORT_HTML=true
EXPORT_PNG=true
```

应用专用密码分组显示的空格会在发信时移除。收件地址可以与发件地址相同，当前支持一个收件地址。Gmail可使用465/SSL；若网络只允许587，可改为 `SMTP_PORT=587`、`SMTP_SECURITY=starttls`，认证在TLS建立后进行，不使用明文SMTP。[Google应用发信设置](https://support.google.com/a/answer/176600)

3. 发送已有午间报告，验证真实收件。此命令直接读取新配置，不需要重新采集行情：

```bash
.venv/bin/python -m stock_daily mail-send --slot midday
# 指定已有报告日期及另一个时段
.venv/bin/python -m stock_daily mail-send --date 20261012 --slot close
.venv/bin/python -m stock_daily mail-send --date 20261009 --slot evening
```

设置好后重启worker，才会让持续运行的进程读取新配置。如果用 `bash scripts/run_local.sh` 启动，先在启动终端按Ctrl+C，再重新运行脚本；不要重复启动多个worker。此前已经成功的时段不会因为刚开启邮件而自动重跑，需要使用上面的 `mail-send` 补发。

### 预览、状态及重试

不配置账号也能生成本地邮件预览，里面包含真实已保存报告的HTML与PNG，未配置地址时使用明确的占位地址，不联网、不发送：

```bash
.venv/bin/python -m stock_daily mail-preview --slot midday --output data/mail-preview.eml
.venv/bin/python -m stock_daily mail-status
# 从mail-status取得失败邮件的id；只重新发信，不重新执行分析
.venv/bin/python -m stock_daily mail-retry --id 邮件id
```

同一数据源、日期、时段和收件地址只创建一条邮件记录，重复执行不会重复发送已成功邮件。排队时将完整MIME邮件（含附件）保存在数据库 `mail_outbox`，后续重试发送当时的版本，不读取可能已经变化的 `latest` 文件。成功后清除队列里的附件副本，保留发送记录；原HTML和PNG仍保留。

发送失败每15分钟重试，最多4次，跨服务重启保留。用 `mail-retry` 可以重置指定失败邮件的次数并立即重试；发送中或已经成功的邮件不能被这个命令重发。更换 `MAIL_TO` 后不会把旧收件地址的队列发给新地址。

`sent` 表示SMTP服务器确认接收，不等于收件箱已投递；首次发送请检查收件箱和垃圾邮件。连接中断或进程在接收确认后崩溃时，SMTP无法保证只投递一次，重试可能出现相同Message-ID的重复邮件。整封邮件编码后默认限20MiB，超限明确记录而不发送。网页任务列表和 `mail-status` 显示发送结果，不显示SMTP密码。

电脑需要保持开机、worker运行并能连接Gmail的SMTP服务器；没有worker时，可用 `mail-send` / `mail-retry` 手动处理。邮件收到后，即使运行分析的电脑关机，仍能查看附件。修改Google账号密码可能撤销应用专用密码，需更新 `.env` 并重启worker。[Google说明](https://support.google.com/accounts/answer/185833)

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

AKShare的 `stock_notice_report` 获取指定日期的公告标题、链接和日期；仅采集当前报告日公告，历史行情补齐不逐日抓取公告。SDK操作也受总超时限制。接口没有精确发布时间，因此本系统留空该字段。启用后18:30核验；接口不可用显示“公告未核验”，不会宣称没有风险。标题提示不读取正文，也不判断利好利空。

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

公告使用 `anns_d`，需要单独权限。启用 `ENABLE_ANNOUNCEMENTS=true` 后会采集公告标题、日期、链接和可用的发布时间；未启用或接口失败时显示“公告未核验”。18:30补查公告只更新公告视图，保留原信号发布时的依据。此版本只按标题关键词提示，没有读取 PDF 正文，不判断事件的利好或利空方向。

接口参考：[日线](https://tushare.pro/document/2?doc_id=27)、[每日指标](https://tushare.pro/document/2?doc_id=32)、[股票列表](https://tushare.pro/document/2?doc_id=25)、[交易日历](https://tushare.pro/document/2?doc_id=26)、[停复牌](https://tushare.pro/document/2?doc_id=214)、[公告](https://tushare.pro/document/2?doc_id=176)、[HTTP API](https://tushare.pro/document/1?doc_id=40)。使用和展示数据时遵循数据提供方的授权范围。

## 合成示例

在 `.env` 设置 `DATA_SOURCE=demo`，执行 `.venv/bin/python -m stock_daily demo` 并重启服务，可生成近30日示范报告。全部行情、公司名称、公告和工作日历都是合成数据；统计不能证明策略有效。切回 `baostock` 会读取独立的真实数据区。

## 本地 HTML 文件

无需服务器即可双击打开导出的单个HTML。午间/收盘与日线HTML都直接包含可阅读的数据正文；手机邮箱附件预览或禁用JavaScript时仍显示市场、新闻（实时报告）、行业、量价/候选及观察名单。更多记录可通过原生展开区阅读；浏览器启用脚本后提供搜索、筛选和图表。样式、报告数据、候选股近65日图表和观察统计均内嵌，不使用CDN，也不发起行情或后台请求。搜索、筛选、详情图表和导航可离线操作；自选股管理在本地服务或CLI中完成，HTML为只读。Token和管理密钥不写入文件。

```bash
# 当前来源的最新复盘，默认输出 data/html/数据源-latest.html
.venv/bin/python -m stock_daily export-html
# 导出特定日期到指定文件；只使用截至该日的价格和观察统计
.venv/bin/python -m stock_daily export-html --date 20261009 --output data/html/my-report.html
# 预览完整示例界面，明确标记为合成数据；不修改主数据源
.venv/bin/python -m stock_daily export-html --source demo --output data/html/demo-preview.html
```

真实源尚未发布报告时，最新导出会明确显示采集状态，不会代入示例行情。指定日期没有报告会报错，并保留原输出文件。文件标注导出时间和数据日期，已打开的文件不会自动获取行情；更新后重新打开，或点击“重载文件”读取当前文件内容。

`.env` 默认启用 `EXPORT_HTML=true`、`EXPORT_PNG=true` 和 `HTML_REPORT_DIR=./data/html`。手动日线任务生成 `数据源-YYYYMMDD.html/.png`；定时任务另存带时分的文件，见下方时间表。`latest` 指向最近一次成功导出的版本，因此午间或下午可能是实时快照，晚间才是完整日线分析。文件内注明数据日期、时间和来源。HTML为可交互的完整数据快照；日线PNG是默认概览长图；实时快照PNG包含市场、热点新闻、行业热点，分别展开已保存的放量/缩量/风险清单、观察名单、前40只报价与数据口径；其他报价在HTML中搜索。PNG不包含HTML切换或搜索后才能查看的全部内容。

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

**跟踪是收盘到收盘的观察，不是策略回测。** 停牌或某日缺数据时该观察区间标记缺失，不按“下一条可用行情”凑足交易日。重叠信号不是独立样本；统计最近最多10000个已保存信号，明细展示前200条。模拟成交、费用和组合回撤在独立“模拟回测”入口查看，其限制见前文。

股票基础信息包含当前可取得的上市、退市和暂停上市股票，但历史行业分类、名称和风险警示状态并非完整的时点历史。手工生成过去日期报告会使用采集时的基础信息，不能作为无前视偏差的历史策略回测。原始行情历史修订目前需人工复核和重算。

## 数据质量和定时任务

行情须满足日期一致、代码唯一、数值有限、价格/量/额/复权因子为正、OHLC一致、复权齐备、基准存在。BaoStock PE/PB等可为空；Tushare须有对应每日指标记录。扣除全天停牌后默认覆盖率至少98%，未达到则拒绝发布。BaoStock覆盖率的分母是其基础信息中的沪深A股，不是包含北交所的全部A股。达到阈值但仍有缺失时明确显示“部分行情缺失”。可将 `MIN_COVERAGE=1` 设为严格完整。

当天行情、元信息、报告和信号在同一数据库事务中发布；失败保留之前的已发布结果，成功采集的历史可用于断点续采。示例和真实数据按 `source` 完全隔离。自选股列表共用，切换数据源后会重新分析，示例添加的代码可自行移除。

worker 使用 `Asia/Shanghai`，默认11:35午间快照、15:05收盘快照、18:30完整日线分析及公告核验。三个时段的成功记录独立保存。启动时补执行当前时段已到时间且未成功的任务；午间仅在13:00之前补采，收盘快照仅在18:30之前补采，避免用下午行情伪造午间数据。失败在时段内每15分钟重试，每个时段每天最多4次。非交易日按交易日历跳过。**worker 停止时没有调度；电脑需开机且进程运行。** 停机数日后的下一次运行会补齐计算窗口内的行情，但不会自动生成停机期间的每一天报告。

数据库任务锁避免重复写入；进程异常退出后锁最长6小时过期。可配置 `ALERT_WEBHOOK`，任务失败后向该地址 POST 通用 JSON；默认关闭，失败也会记录在页面中。

## 自选股与 API

```bash
.venv/bin/python -m stock_daily watch add 600000.SH --note '关注回调后量价变化'
.venv/bin/python -m stock_daily watch list
.venv/bin/python -m stock_daily watch remove 600000.SH
.venv/bin/python -m stock_daily track
```

只读 API（`/api/backtest?live=true`附加盘中估值，默认历史回测保持日线口径）：`/health`、`/api/dashboard?day=YYYYMMDD`、`/api/report/{day}`、`/api/report/{day}/download`、`/api/stocks/{code}/history?day=YYYYMMDD`、`/api/tracking?day=YYYYMMDD`、`/api/watchlist`、`/api/operations`、`/api/backtest?day=YYYYMMDD`。自选股 POST/DELETE、观察计划 PUT `/api/watchlist/{code}/plan`、AI摘要 POST `/api/evidence/{day}/summary`要求 `Authorization: Bearer APP_API_KEY`。网页不存储管理密钥。

## GitHub Pages：固定公网报告链接

网站由本机使用已保存数据生成，GitHub Pages负责托管；手机和电脑访问同一网址。电脑关机不影响已发布报告的访问，新数据采集和上传仍需要本机worker运行。GitHub Pages不能运行本项目的Python采集服务。

部署步骤基于[GitHub官方Pages工作流说明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

公开网站有最新报告、午间/收盘/日线入口、最近5个报告日期的归档及HTML/PNG下载。它使用单独渲染的公开版：移除个人自选、备注、快讯的自选标记、邮件队列和本机任务记录；市场、新闻、行业与量价清单保留。公开图片也从公开HTML重新渲染，不会复制包含自选的邮件附件。`.env`、数据库和原邮件附件不会上传。

### 首次启用

1. 安装并登录GitHub CLI：`gh auth login`。运行账号需要仓库写入和工作流权限，Pages设置需要仓库管理权限。本仓库为公开仓库，可用GitHub Free的Pages。
2. 将包含 `.github/workflows/pages.yml` 的代码提交并推送到GitHub的 `main` 分支。
3. 在GitHub仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。也可以只在首次启用时执行：

```bash
gh api --method POST repos/vincentsx/Class_A_Share_Market_Analysis/pages -f build_type=workflow
```

若Pages已存在，使用网页设置，或将上述API的 `POST` 改为 `PUT` 更新，不能重复创建。

4. 先生成本地公开网站并检查，再首次发布：

```bash
# 仅导出，不联网、不发送邮件、不改变现有HTML/PNG
.venv/bin/python -m stock_daily export-pages
python3 -m http.server 8080 --bind 127.0.0.1 --directory data/pages
# 浏览器打开 http://127.0.0.1:8080；检查后Ctrl+C结束预览

# 上传公开数据，触发GitHub Actions部署
.venv/bin/python -m stock_daily publish-pages --repo vincentsx/Class_A_Share_Market_Analysis
gh run list --repo vincentsx/Class_A_Share_Market_Analysis --workflow pages.yml --limit 1
# 使用上一步的运行ID等待部署，成功后再打开公网网址
gh run watch 运行ID --repo vincentsx/Class_A_Share_Market_Analysis --exit-status
```

默认公网地址为 `https://vincentsx.github.io/Class_A_Share_Market_Analysis/`。固定入口为 `portfolio.html`、`latest.html`、`midday.html`、`close.html`、`evening.html`；尚未生成的时段不创建入口。首次部署完成前可能返回404。绑定自定义域名后，以Actions部署结果中的网址为准。

### 持续自动更新

首次部署成功后，在本地 `.env` 增加并重启worker：

```dotenv
PAGES_ENABLED=true
PAGES_REPOSITORY=vincentsx/Class_A_Share_Market_Analysis
PAGES_RETAIN_DAYS=5
```

worker每10分钟在行情任务和邮件处理之后检查一次：数据或渲染代码有变化时发布；未变化的报告复用已验证的公开PNG，已成功部署的相同内容不重复上传。部署失败会在后续检查重试。`PAGES_RETAIN_DAYS`为1～20个有报告的日期，默认5；本机数据库和邮件附件保留不受此设置影响。执行worker的系统账号也必须能运行已登录的 `gh`；Docker容器不会自动继承宿主机的gh登录。

发布使用固定的预发布Release `pages-report-data` 保存 `site.tar.gz`，每次替换该公开资源，再触发 `pages.yml` 下载并部署。报告大文件不进入Git提交历史。这个Release是公开数据的一部分。Pages部署仅需要GitHub Actions的内置Token，不需要在GitHub配置SMTP密码、数据Token或数据库。上传、工作流和网站上线是三个阶段，命令返回 `dispatched` 仅表示已触发工作流，上线以Actions成功为准。

没有报告时拒绝发布空站点；构建/图片失败保留上一版公开网站；发布失败保留此前已上线页面。网站显示真实报告日期、行情采集时间及新闻截止时间，不把旧报告标为实时行情。公网访问速度需在实际使用的手机和电脑网络下验证。

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

## 模拟持仓的盘中更新

交易日09:30～11:30、13:00～15:00，worker默认每10分钟采集模拟信号涉及股票与沪深300的新浪行情，15:05再采集一次收盘报价。午休、夜间、周末及交易日历核验为休市的日期不采集。报价独立缓存，不改写日线、信号、历史收益曲线或成交记录，不增加邮件发送次数。修改间隔可设置 `PORTFOLIO_INTERVAL_MINUTES=10`。

本地网页选择“跟随最新报告”并打开“模拟持仓”，即可查看已确认持仓的盘中市值、总资产、盈亏和报价时间。现金、股数和费用来自最近完整日线核验的模拟账户；新入场或退出仍待完整日线确认，盘中估值不是新的模拟成交。最大回撤和前后段统计仍按日线口径。选择具体历史日期时只显示该日的日线回测，避免把今天的行情混入历史报告。

公开网站的 `portfolio.html` 和首页独立读取轻量持仓JSON：worker采集后将默认模拟账户同步到公开仓库的 `pages-live` 分支，浏览器每分钟检查一次，并显示行情采集与公开同步时间；主读取通道失败或返回过旧数据时尝试GitHub API备用通道，自动备用请求最多每两分钟一次。手机从后台或历史页面返回时会重新检查，资金卡片旁显示同步状态并提供重试按钮；不再为每次盘中报价上传整站HTML/PNG或等待Pages部署。持仓数据只包含模拟账户、估值和时间，不包含密钥、个人自选、备注或邮件记录。原始日线与快照仍按报告任务发布，图片和下载到本地的HTML是生成时的固定快照。轻量同步失败或行情过期时保留已有数据并提示。日线任务默认北京时间18:30开始，尚未获得完整日线时仍按15分钟间隔、最多4次重试。

持仓报价缺失、昨收尺度与日线不一致（可能除权）或中间缺少交易日日线时，不发布混合或补造的盘中净值，显示最近完整日线账户并说明原因。worker停止、网络故障或报价过期时显示保存的估值及过期提示。电脑需开机、不休眠且联网。

## 开发和验证

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check stock_daily tests
node --check stock_daily/static/app.js
```

测试涵盖复权、指标窗口、停牌区间、两种真实源的单位转换、复权缓存和增量更新、分页截断、SDK超时重连、防止重复运行、失败原子性、断点续采、来源隔离、写入鉴权与调度。自动化测试使用合成数据、HTTP mock和SDK替身，不等同于真实接口全量验证。实时连通性检查使用上述脚本。实时分析测试还涵盖午间同时间量能口径、交易日缺口、复权/昨收异常、热点延续、午后累计量额回退和数据源隔离；新闻测试涵盖去重、首次采集和发布双重截止、内容版本、传闻标记、来源故障、补采时点与不发信约束。附件预览测试用禁用JavaScript的390px浏览器验证实时与日线报告数据可见，同时验证启用脚本后的交互与HTML转义。图片导出测试在截图前检查放量/缩量/风险三组同时可见、报价展开且只展示前40只，并确认HTML自身的交互状态不被导出修改。Chromium未安装时该浏览器测试明确跳过，其余导出测试仍运行。

目录：`providers.py` Tushare/示例源；`free_provider.py` 免费数据源；`sdk_client.py` SDK进程与超时；`pipeline.py` 采集校验发布；`analysis.py` 指标与信号；`service.py` 报告和观察视图；`web.py` API；`scheduler.py` 定时；`db.py` 存储；`intraday.py` 实时采集与导出；`snapshot_analysis.py` 热点、量价与午后分析；`news.py` 滚动快讯留存、主题和行情对照；`static/` 和 `templates/` 页面。
