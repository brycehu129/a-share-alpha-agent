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

`/etc/alpha-shadow.env` 增加 `ANTHROPIC_API_KEY=sk-ant-...`，由盘后分析任务和后台页面读取。
可选变量：`CLAUDE_MODEL`（默认 `claude-opus-5`）、`CLAUDE_EFFORT`（默认 `high`）。

没有配置时盘后分析照常运行，只是降级成纯规则报告并在报告里说明原因；日线流程和开盘观察完全不读这个凭证。凭证只从环境变量读取，不写日志、不进报告、不进归档，错误信息返回前统一脱敏。

实时行情走腾讯 `qt.gtimg.cn`，**不需要任何凭证**；同一接口覆盖沪深个股、A股指数、港股和美股指数。
