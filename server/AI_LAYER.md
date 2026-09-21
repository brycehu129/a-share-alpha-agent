# AI 分析层 0.1（盘后分析）

本文件规定 AI 在这套系统里**能做什么、不能做什么**。改代码前先改这里。

## 为什么要单独写一份边界

这个仓库原本是纯规则系统，花了很大力气维持一条纪律：只报有样本支撑的数字，
不把未经验证的东西说成胜率（见 [STRATEGY.md](STRATEGY.md)）。接入大模型天然
会削弱这条纪律——模型语气笃定、随时能编出听起来合理的理由。所以 AI 层从第一天
起就按"可以解释、不可以决策"来设计。

## 分层

```
实时行情(live_quote) ─┐
日线缓存 + 候选池归档 ─┼→ live_check：纯规则，算出结构化事实 ─→ ai_analyst：只解读
持仓/自选账本 ────────┘        ↑ 这一层单独就有用           ↓
                               AI 挂了照常出报告      postclose_report：留档+推送
```

**规则层和 AI 层之间只传结构化 JSON。** 这不是洁癖：后面几期要让代码按 AI 产出的
持仓合约自动检查卖出条件，自由文本解析不了，一次格式漂移就会让监控静默失效。

## 硬边界

| 项目 | 状态 |
|---|---|
| 写入 `predictions/` `outcomes/` | **禁止**。那是规则策略的冻结留档，AI 不得污染其胜率统计 |
| 写入虚拟账户 `agent/` | **禁止**（本期）。产线A的 AI 账户是独立账本，见分期计划 |
| 修改风控参数 | **永久禁止**。本金、最大持仓数、单票上限、止损止盈、回撤暂停线都是 `SHORT_POLICY` 里的代码常量 |
| 下单 | **永久禁止**。系统不接券商接口 |
| 修改策略数值参数 | **只能提议，人批准才生效。** `proposals.py` 队列 + 后台 `/proposals`：白名单 + 绝对边界 + 步长上限 + 证据门槛（n≥30、日期组≥15、且来自当前执行版本），批准时再复核一遍。红线由代码拒收并留档，见 STRATEGY.md「参数提议 + 人确认」 |
| 报告落到公开 Git 分支 | **禁止**。报告含真实持仓成本价，只存 `server/data/private/` |

## 提示词纪律（`ai_analyst.system_prompt()`）

1. **禁止提及消息面。** 本期没有接新闻、公告、研报、业绩数据，模型一旦谈这些就
   一定是编的。这是金融场景里代价最高的一类幻觉，在提示词里明确堵死，并有测试
   （`test_ai_analyst.test_prompt_forbids_inventing_news`）守着。
2. **必须引用具体数值。** 只给方向性形容词的结论没法复盘，也没法判断它错在哪。
3. **缺数据要明说**，写进 `data_caveats` 并相应降低 confidence，不许推测填补。
4. **confidence 是依据强度自评（1–5），不是胜率。** 禁止输出百分比形式的胜率。
5. 候选池的策略口径（两条 track 的阈值）原文写进提示词，让候选研判和规则对齐，而不是让模型
   自由发挥另一套标准。**这份口径只适用于候选池**：提示词明确写明它"不适用于用户的持仓"，
   持仓只依据用户录入的买入成本，以及系统按 ATR 从成本价算出的止损/止盈**参考位**来判断，不替用户另设止损位（`postclose-analyst-3` 起；此前是依据用户声明的持有类型/止损/目标，2026-09-21 取消声明）。

## 留档

每份报告记录：模型名、effort、`prompt_version`、`strategy_version`、请求/完成时间、
输入输出 token 数、以及模型输出与实际送入标的的核对结果（漏判哪些、虚构了哪些）。

**这是为了日后能算出 AI 研判的真实命中率。** 在积累够前瞻样本之前，AI 的结论一律
标注"研究假设，未经任何前瞻验证"。任何地方都不得把它说成已验证的胜率或策略 Alpha。

## 输出校验

模型返回的 `symbol` 会和实际送进去的标的逐一核对（`ai_analyst.validate`）：

- 漏掉的标的 → 记进报告的 issues，不让它在报告里静默消失
- 虚构的代码 → 直接剔除并记录

## 降级

AI 调用失败（没配 key、没装包、限流、超时、被拒、输出截断、JSON 不合法）时，
`postclose_report` 照常出报告，只是没有研判部分，并在报告里写明失败原因。
规则层算出来的入场带、门槛、浮动盈亏本身就有用。

## 成本

`claude-opus-5`，每天一次盘后分析，约 15k 输入 / 5k 输出（adaptive thinking 的
推理 token 计入输出），按 $5/$25 每百万 token 估算约 ¥2–6/天。
模型可用环境变量 `CLAUDE_MODEL` 覆盖，effort 用 `CLAUDE_EFFORT`。

## 配置

**两个后端，同一套接口**（`claude_client.py` 是统一入口，`ai_analyst`/`scenario_analyst`/哨兵不知道也不关心用的是哪个）。
`/etc/alpha-shadow.env` 里二选一：

```
# 方案 A：OpenRouter（只用标准库，不需要装任何包）
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=anthropic/claude-opus-5        # 可选，默认就是它
SENTINEL_MODEL=anthropic/claude-sonnet-5        # 可选：哨兵每天最多15次，可用比盘后报告更便宜的模型

# 方案 B：直连 Anthropic
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-opus-5                      # 可选
```

选择规则：`LLM_PROVIDER=openrouter|anthropic` 显式指定优先；没指定时，配了 `OPENROUTER_API_KEY` 就用 OpenRouter，否则用 Anthropic。
`SENTINEL_MODEL` 的模型 id 必须符合当前后端的写法（OpenRouter 用 `anthropic/claude-sonnet-5`，直连用 `claude-sonnet-5`）。
其他可选：`OPENROUTER_SITE_URL`（归因用的站点 URL）、`OPENROUTER_BASE_URL`（代理或测试用）。

**也可以在后台页面配置（推荐）**：`/` 的“推送配置”页有“大模型（OpenRouter）”一栏，可填 OpenRouter key、主模型、哨兵模型，
带“测试连接”按钮（等价于下面的 `check`）和“清除”。规矩：

- key 存 `server/data/private/llm_settings.json`（0600，不进 git）。**只写不读**：页面只显示末 4 位，输入框永远为空，错误提示不回显你提交的内容。
- **优先级：页面保存的 > `/etc/alpha-shadow.env`**。页面保存后立即生效（不用重启服务），页面会标出当前生效的来源；点“清除”后自动回落到环境变量。只管 OpenRouter 的三项（`OPENROUTER_API_KEY` / `OPENROUTER_MODEL` / `SENTINEL_MODEL`），直连 Anthropic 的 key 仍只走环境变量；若服务器设了 `LLM_PROVIDER=anthropic`，页面会提示 OpenRouter key 不会被用到。
- **只有程序入口读这个文件**（`llm_settings.apply()`：webapp、`sentinel.py`、`postclose_report.py`、`claude_client.py check`）。库代码只看环境变量——cron 每次先跑全量测试，库若自己读文件，服务器上一存了 key，“没配 key”的测试就会变样，连带中止日线流程。新增调用大模型的入口时记得也调用它。
- **防跨站伪造（CSRF）**：所有 POST 校验 `Sec-Fetch-Site` / `Origin`，跨站请求返回 403。这一层同时保护 /book、/config、/postclose/run——后台用 Basic Auth，浏览器会替任何网页自动带上凭证，没有这层的话，你打开的任意网页都能替你改 key（换成对方的，之后你的持仓和止损位就流到对方账户的调用日志里）。
- 后台是自签名 HTTPS，浏览器会有证书警告；不要在不信任的网络下提交 key。key 一旦怀疑泄露，去 OpenRouter 撤销重发，这里点“清除”再填新的。
- 顺带修了一个隐患：`postclose_report.py --no-ai` 以前只去掉 Anthropic 的 key，配了 OpenRouter 时会照样调用并花钱。

**上线前先自检**（一次极小的真实请求，花费约几分钱；没配好会明确报错，而不是等周一盘中哨兵触发时才发现研判一直静默失败）：

```
python3 server/claude_client.py check
python3 server/claude_client.py check --model deepseek/deepseek-v4.1-flash   # 试别的模型
```

### OpenRouter 的几个坑（都已在 `openrouter_client.py` 里处理）

- **推理 token 和可见输出共用同一个 `max_tokens` 预算。** 推理把预算吃光时返回 `finish_reason: length` 且**内容为空，但推理 token 照样计费**。客户端专门识别并报出"推理消耗了几乎全部 max_tokens"，别的报错都不会这么说。
- **strict 结构化输出各家实现不同**：有的服务商保证合规，有的只当参考。所以返回的 JSON 不能盲信——客户端容忍 ` ```json ` 围栏并在元信息里标 `json_repaired`，`check` 命令遇到会警告"该模型结构化输出不够可靠"；真正的兜底是上层的价位/schema 校验。
- 请求带 `provider.require_parameters=true`，只路由到真正支持 `json_schema` 的服务商，不会被悄悄转给不支持的端点。
- 402 = 余额不足，单独分类（`payment_required`），不是"服务挂了"，重试没用；HTTP 200 但 body 里带 `error` 也按失败处理。
- 有 systemd 时限的调用（哨兵研判）传 `max_retries=0`，让"总耗时 ≤ timeout"成立。
- 不依赖 OpenRouter 的响应修复插件——其文档页面无法核实确切参数，没验证过的参数不发。

OpenRouter 上 `anthropic/claude-opus-5`、`claude-sonnet-5`、`claude-fable-5.1` 都声明支持 `structured_outputs` 和 `reasoning`（2026-09-19 查询公开模型列表所得，随时会变）。

凭证只从环境变量读，不写日志、不进报告、不进归档；错误信息返回前统一脱敏（`sk-ant-…` 和 `sk-or-…` 两种格式，以及环境里配置的真实 key）。


## 自选股哨兵与情景研判（2026-09-19）

**有两份 AI 提示词，彼此隔离，不得互相借用口径。** `ai_analyst`（盘后报告）服务候选池；`scenario_analyst`（盘中哨兵）服务你自己的持仓与自选股。后者不含任何候选池策略口径——系统不知道你为什么买，就不能拿"突破/回调"的标准去衡量你的持仓。`ai_analyst` 的持仓部分同步收紧：只依据你录入的买入成本和系统算出的止损/止盈参考位（不是你声明的，`sentinel-scenario-2` / `postclose-analyst-3` 起），不另设止损位，且去掉了没有数据支撑的 `swing_t`（做T提示只在盘中哨兵里有，那里有分时数据和系统按 T+1 算出的可卖老仓）。

权限边界同前：**只提醒，永不下单**；不写 `predictions/` `outcomes/` 与任何虚拟账户；不改风控参数。新增两条：

- 模型给的每个价位由代码校验后才可能被推送/存档（方向、相对现价的位置、涨跌停区间、目标与失效价的顺序），不合格丢弃并记录。
- 每个情景存档后收盘自动对账，**这是为了让你能机械地检验 AI 的情景判断有没有预测力**，而不是凭印象。样本不足 20 条有结论前不给命中率。

情景研判用 `effort=medium`、单次最多等 100 秒且**不重试**（SDK 默认 2 次重试会让总耗时超过 systemd 时限），`max_tokens=8000`。成本：每天最多 15 次调用（可配），每次约 4–9k 输入字符。

`prompt_version = sentinel-scenario-1`（盘中）、`postclose-analyst-2`（盘后，本次因持仓口径修正而升版）。
