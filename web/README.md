# Alpha Shadow 前端（Vue 3 + Element Plus）

后台的五个页面（看板 / 候选池 / 持仓与自选 / 提议 / 设置）是同一个单页应用，共用 `src/App.vue` 里的顶栏。
看板有两个 tab（市场行情 / 盘后分析，`?tab=postclose`）；哨兵不是独立页面，告警详情在「持仓与自选」每一行的「告警」按钮（抽屉）里。
旧地址 `/sentinel`、`/postclose` 仍可打开，由 `src/router.js` 重定向到新位置（企业微信推送里还写着 `/postclose`）。
导航条目来自 `src/router.js` 的 `NAV`，所以任何页面上导航都一模一样。

## 日常开发

```bash
cd web
npm install                 # 只需一次
ADMIN_PASSWORD=pw PORT=8080 python ../server/webapp.py   # 另开终端：起后端
npm run dev                 # 热更新开发服务器，/api 自动代理到 http://127.0.0.1:8080
                            # 后端不在本机时：API_TARGET=https://host:8443 npm run dev
npm test                    # 前端单测（vitest）
```

## 发布

```bash
cd web && npm run build     # 产物写到 ../server/static/
```

**必须把 `server/static/` 一起提交。** 服务器不装 Node，部署只是 `git reset --hard` 后重启（`server/deploy.sh`）；
忘了构建/提交的话 `server/test_webapp.py` 里有一条用例会变红。

## 约定

- 数据全部走 `/api/*`（见 `server/api.py`、`server/api_pages.py`），POST 必须是 `application/json`。
- 文本一律用 Vue 插值渲染（自动转义）。只有一处用 `v-html`：盘后报告（服务端 `markdown_to_html` 已整体转义）。
  不要再新增 `v-html`。
- 涨红跌绿：用 `RiseFall` 组件或 `.rise/.fall/.flat` 类，颜色在 `src/styles/tokens.css`，不要在组件里写死。
- 不依赖任何外部 CDN/字体（国内网络下会卡）。
