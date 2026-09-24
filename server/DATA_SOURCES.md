# 数据源配置

GitHub Settings → Secrets and variables → Actions → New repository secret：

- `XIAODEFA_TOKEN`：用户提供的 `https://t.xiaodefa.top/` 服务凭证。
- `TUSHARE_TOKEN`：原 Tushare 官方凭证，保留原值。

配置 XIAODEFA_TOKEN 后，日历、股票清单、daily、adj_factor 同步优先使用新服务。
没有新凭证时继续使用原官方服务。每个凭证只提交到对应固定 HTTPS 地址，
不跟随重定向，不在第三方请求失败时将同一个凭证发送给官方或其他地址。
请求和数据记录包含实际 endpoint/source，访问频次冷却按来源区分。

添加密钥后，在 Actions 手动运行「Tushare基础数据与日线增量同步」。
每轮补最近缺失的最多10个交易日，目标60日；已有数据与检查点保留。
每日定时整理同样读取新密钥。工作流成功不代表所有数据完整，需检查报告。

2026-09-16 接入时，trade_cal、daily、adj_factor 小样本实测均返回 code=0。
尚不能仅凭该样本认定全市场覆盖、所有接口权限或服务稳定性。
该域名是用户提供的第三方服务，不标记为 Tushare 官方来源。

BaoStock 因明确返回黑名单错误暂停，保留已下载数据，需服务方解除后再恢复。

## AI 分析层凭证（2026-09-19）

`/etc/alpha-shadow.env` 里配 `OPENROUTER_API_KEY=sk-or-v1-...`（经 OpenRouter，不需要装任何包）或 `ANTHROPIC_API_KEY=sk-ant-...`（直连）二选一，
由盘后分析任务、盘中哨兵研判进程和后台页面读取。完整变量说明和上线前自检命令见 [AI_LAYER.md](AI_LAYER.md) 的"配置"一节。
通用可选变量：`CLAUDE_EFFORT`（默认 `high`）。

没有配置时盘后分析照常运行，只是降级成纯规则报告并在报告里说明原因；日线流程和开盘观察完全不读这个凭证。凭证只从环境变量读取，不写日志、不进报告、不进归档，错误信息返回前统一脱敏。

实时行情走腾讯 `qt.gtimg.cn`，**不需要任何凭证**；同一接口覆盖沪深个股、A股指数、港股和美股指数。

## 看板「市场行情」的数据（2026-09-21）

看板不再读 GitHub 上 `dashboard/latest.json` 里那份日级行情快照（它会因为批处理没跑而停在几天前），
指数、成交额、资金流向、涨跌家数、涨跌停池、龙虎榜都直接取自公开接口，**不需要任何凭证、不占 Tushare 积分**：

| 数据 | 来源 | 取数时机 | 缓存 |
|---|---|---|---|
| 6 个指数 + 今日两市成交额 | 腾讯 `qt.gtimg.cn`（`live_quote`） | 每次打开/每 30 秒刷新 | 30 秒 |
| 沪深北涨跌家数 | 东财 `push2 ulist.np`（f104/f105/f106） | 同上 | 60 秒 |
| 沪深合计资金流向 | 东财 `push2 fflow/daykline`（主力=超大单+大单） | 同上 | 120 秒 |
| 上一交易日成交额 | 东财 `push2his kline`（沪、深指数日 K） | 同上 | 1 小时 |
| 涨停/跌停/炸板/昨日涨停/强势池 | 东财 `push2ex getTopic*Pool` | 16:30、17:30 落盘；页面只展示最近落盘名单 | 收盘更新 |
| 龙虎榜个股汇总与席位 | 东财 `datacenter-web RPT_DAILYBILLBOARD_DETAILSNEW` / `RPT_BILLBOARD_DAILYDETAILS{BUY,SELL}` | 16:30、17:30 落盘；席位按需 | — |
| 个股行情/日 K | 腾讯 `qt.gtimg.cn`、`fqkline`（前复权） | 点开个股时 | 60 秒 |
| 公司资料/概念 | 东财 F10 `RPT_F10_ORG_BASICINFO` | 点开个股时 | 60 秒 |

- 落盘文件：`server/data/market_review/<YYYYMMDD>.json`（`MARKET_REVIEW_DIR` 可改；带校验和；公开行情，**不进 git**）。
  由 `alpha-shadow-review.timer`（工作日 16:30、17:30）调 `cron_review.sh` 生成：先 `market_review.py`（抓取），
  再 `watch_outcome.py`（结算上一交易日「次日关注」名单的兑现情况，写进那份旧文件自己的 `next_day_watch.items[].outcome`
  和今天这份文件的 `prev_watch`），最后 `next_day_watch.py`（规则打分；17:30 那次两个脚本都加 `--ai`）。
  `health_check` 的「市场复盘数据」在 17:45 之后检查当天文件是否齐全（含 `prev_watch`，仅当存在待结算的上一交易日名单时才要求）。
- 东财是非官方接口，字段可能变；解析集中在 `market_review.parse_*`，形状不对就当该组失败，看板对应块显示「暂无 + 原因」，其它块照常。
  东财对个别出口 IP 会在短时间密集请求后断开 push2* 的连接（本地开发时出现过）：所以慢变化的数据各有缓存，`push2*` 主机 https 失败后会再试一次 http（只返回公开行情、不带凭证）。
- 涨跌停家数 = 涨跌停池长度（交易所口径，含 ST/北交所）。`market_context.py` 里按涨跌幅阈值近似的那份仅供盘后 AI 的市场背景使用，看板不再用。
- 「次日关注」是**短线情绪视图**（`next_day_watch.py`，规则版本 `nextday-rules-2`），独立于「候选池」的策略选股，不影响其回测口径。
  rules-2 把候选按 `bucket` 分三组（`core` 可参与主榜 / `high` 高位只观察 / `unbuyable` 一字买不进），并对强度排名前 30 的
  候选补取前复权日 K（`stock_detail.fetch_kline`）算位置/空间指标（近 10 日累计涨幅、对 20 日线乖离等）；日 K 取不到的
  那只不做位置加减分，只标「位置未知」，不猜。
- 「上一交易日兑现」（`watch_outcome.py`）用次日开盘价作为参与成本，判定每只票是继续涨停/一字/炸板/跌停还是按开盘价
  涨跌分类；一字板单独算「买不进」，不计入命中率。所需行情来自 `live_quote.snapshot()`（批量快照，只支持 sh/sz，
  北交所代码直接标 `quote_missing`）和 `minute_data.fetch_minute()`（逐只、尽力而为的日内路径一句话，单只失败不影响其它只）。
