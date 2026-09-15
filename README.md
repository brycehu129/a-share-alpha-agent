# A股 Alpha Agent

第一阶段：验证 GitHub Actions 能获取腾讯行情并保存快照。

首次提交工作流后自动运行；也可在 Actions → A股行情连接测试 → Run workflow 手动启动。

测试上证指数和平安银行，连续采集3次，成功时保存6条快照。数据库与日志在该次运行的 Artifacts 中保留7天。

这是数据访问测试，不提供选股建议，不连接券商，不更新 Dashboard，不启用定时任务。每次运行独立建库，尚未实现跨运行历史累积。

详细说明见 server/GITHUB_TEST.md。
