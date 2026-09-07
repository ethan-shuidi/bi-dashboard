# BI 数据看板

Amazon 运营数据看板项目，已与 Shopify 数据看板拆分。项目包含 Vue 3 前端、FastAPI 后端，并通过 IdeaDock 发布。

## 项目边界

- `frontend/`：Vue 3 + Vite 前端源码，包含 Amazon 看板和看板导航。
- `backend/`：FastAPI + SQLAlchemy 后端，负责领星 API、Shopify API、数据聚合和接口服务。
- `shopify-bi-dashboard`：独立的 Shopify 看板项目，不属于本仓库的线上 Amazon 看板交付范围。
- `frontend/dist/`：前端构建产物，部署到 IdeaDock 静态站点。

## Amazon 看板能力

- 按日、周、月自然周期查询并展示数据。
- 支持站点、系列、产品和日期范围筛选。
- 产品归类以站点 + ASIN 映射为准。
- 日本站会合并 `Comu-JP` 和 `Comulytic-JP` 两个店铺的数据。
- 产品表现来源：销量、净销售额、订单量、B2B、Session、CVR，以及展示量、点击、广告花费、广告销量、广告订单量、CTR、CPC、广告CVR、ACOS；其中净销售额读取 `net_amount`，广告字段按 SP、SB、SBV、SD 四类产品表现字段合计。
- 点击、广告花费、广告销量、广告订单量旁的明细按钮展示产品表现返回的 SP、SB、SBV、SD 分项；广告销量占比和广告订单占比仍由看板按总量计算。
- `ACoAS = 广告花费 / 净销售额`；`广告销量占比 = 广告销量 / 销量`；`广告订单占比 = 广告订单量 / 订单量`。
- `广告销量占比` 与原列保持不变，不能使用 `adv_rate` 代替；`广告订单占比` 为新增列。
- 系列和周期汇总行分别汇总产品表现与广告报表明细；比例类指标按对应分子、分母汇总后计算。

## Shopify 看板

Shopify 看板已经拆分为独立项目，由单独的仓库和 IdeaDock 项目维护。本仓库仅保留与 Amazon 看板共用的应用外壳和部署配置，修改 Amazon 功能时不要将 Shopify 项目重新合并进来。

## 后端环境变量

`DATABASE_URL` 由 IdeaDock 后端服务自动注入，不需要手工配置。

领星凭据应配置在 IdeaDock 后端服务的 Secret 或环境变量管理页面，不要写入源码、提交记录或前端构建产物：

- `LINGXING_APP_ID`
- `LINGXING_APP_SECRET`

其他可选变量：

- `SYNC_API_KEY`：保护主动同步接口的内部调用密钥；看板只读查询在公司内网免密。
- `DASHBOARD_API_KEY`：当前不用于只读看板查询，保留为未来重新启用鉴权时的兼容配置。
- `SHOPIFY_STORES_JSON`：仅 Shopify 独立后端使用，不能配置到 Amazon 前端。
- `SYNC_COOLDOWN_SECONDS`：同步冷却时间，默认 600 秒。
- `SHOPIFY_INITIAL_SYNC_DAYS`：Shopify 首次同步天数，默认 90 天。

## 本地运行

### 前端

```bash
cd frontend
npm install
npm run dev
```

构建：

```bash
npm run build
```

### 后端

```bash
python3 -m uvicorn app:app --app-dir backend --reload --port 8000
```

常用接口：

- `GET /health`
- `GET /api/status`
- `GET /api/amazon/stores`
- `GET /api/amazon/dashboard`
- `POST /api/sync`

## 发布前检查

```bash
python3 -m compileall backend
cd frontend && npm run build
cd ..
shuidi ideadock backend lint backend
shuidi ideadock frontend lint frontend/dist
```

## IdeaDock 发布

当前 Amazon 项目：

- 命名空间：`8uxh4y1ulu`
- 项目：`bi-dashboard`
- 前端交付轨道：`prototype`
- 后端服务：`bi-dashboard-api`

前端发布的是完整的 `frontend/dist/` 快照；后端发布的是 `backend/` 服务包。发布后应检查前端预览、`/health`、`/api/status` 和一个实际看板查询。

线上静态站点是构建产物，不是 Vue 源码目录。长期维护应以本仓库源码为准，修改后重新构建并发布，不要直接编辑线上压缩后的 JavaScript 或 CSS。

## 交接建议

交接运营同事时，同时提供：

1. GitHub 仓库访问权限。
2. IdeaDock `bi-dashboard` 项目访问权限。
3. 本 README 和部署权限说明。
4. 环境变量名称及用途说明；敏感值只在 IdeaDock Secret 中配置。

不要把领星 AppSecret、数据库密码、同步密钥等敏感值提交到 GitHub。
