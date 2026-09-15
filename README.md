# A股 Alpha Agent

当前阶段：通过 GitHub Actions 获取腾讯行情，生成6指数市场快照，并将报告与原始字段追加保存到私有仓库的 `market-data` 分支。

[打开报告历史](https://github.com/brycehu129/a-share-alpha-agent/tree/market-data) · [运行市场快照工作流](https://github.com/brycehu129/a-share-alpha-agent/actions/workflows/market-report.yml)

## 手动更新

打开工作流 → Run workflow → 选择 master → Run workflow。完成后，报告历史入口会增加一份报告。工作流还会在另一台全新运行机器恢复已提交的数据，验证报告内容与校验和。

目前没有开启定时运行。无需服务器或 API Key，使用仓库内置 GITHUB_TOKEN 写入数据分支。私有仓库 Actions 运行受账户额度约束。

## 数据与程序

- `master`：Python 标准库采集器、报告程序、工作流和测试。
- `market-data/records/`：按运行编号保存的 JSON，包含原始字段、来源、时间和 SHA-256。
- `market-data/reports/`：可直接阅读的 Markdown 报告。
- 临时 SQLite 和诊断日志作为 Actions 附件保留7天；正式历史保存在数据分支，不依赖附件保留期。
- 历史禁止程序覆盖，但仓库管理员可修改 Git 历史，因此不等同防篡改审计。

采集失败也会保存失败报告，并使最终验证失败；不冒用旧行情。报价陈旧或日期不齐会明确标注。重复采集不计为独立交易样本。

## 尚未实现

交易日历、全市场个股扫描、历史日线、板块评分、新闻、LLM 分析、预测胜率、虚拟交易与 Dashboard 数据接入均未完成。现有 Dashboard 仍为演示数据；本程序不生成买卖建议。

项目目标参数：10万元虚拟本金、无杠杆、3–20交易日、最多3只持仓，最大回撤约束50%。这些是后续策略约束，当前并未执行交易。

## 本地验证

```bash
python3 -m unittest discover -s server -p 'test_report_pipeline.py' -v
```

原始连通性测试说明见 [server/GITHUB_TEST.md](server/GITHUB_TEST.md)。
