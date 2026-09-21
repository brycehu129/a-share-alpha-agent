# Alpha Shadow 前端（Vue 3 + Element Plus）

后台的六个页面（看板 / 哨兵 / 提议 / 盘后分析 / 持仓与自选 / 设置）是同一个单页应用，共用 `src/App.vue` 里的顶栏。
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
- 文本一律用 Vue 插值渲染（自动转义）。只有两处用 `v-html`：盘后报告（服务端 `markdown_to_html` 已整体转义）和
  哨兵分时图（服务端 `_safe_svg` 已拒绝脚本/外链）。不要再新增 `v-html`。
- 涨红跌绿：用 `RiseFall` 组件或 `.rise/.fall/.flat` 类，颜色在 `src/styles/tokens.css`，不要在组件里写死。
- 不依赖任何外部 CDN/字体（国内网络下会卡）。
