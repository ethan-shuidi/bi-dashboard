# BI Dashboard

IdeaDock 全栈 BI 看板。

## 架构

- `frontend/`：Vue 3 + Vite 静态前端。
- `backend/`：FastAPI + SQLAlchemy + MySQL。
- 数据同步：用户打开看板时按冷却窗口自动同步；点击“立即同步”会跳过冷却窗口并立刻拉取。
- 增量策略：首次默认读取近 90 天，之后按 Shopify `updated_at` 回看 3 天并 upsert。

## 运行环境变量

数据库变量由 IdeaDock 自动注入。Shopify 凭据必须通过 IdeaDock 后端服务的 Secret 配置页设置，不要写入代码或前端。

推荐配置 Secret `SHOPIFY_STORES_JSON`，结构与原项目一致，但不需要飞书 Webhook。

可选普通环境变量：

- `SHOPIFY_INITIAL_SYNC_DAYS`：首次同步天数，默认 `90`。
- `SYNC_COOLDOWN_SECONDS`：打开看板自动同步的冷却时间，默认 `600`；不限制“立即同步”。

## API

- `GET /health`
- `GET /api/status`
- `POST /api/sync?trigger=button`
- `GET /api/dashboard?days=30&auto_sync=true`

## 本地验证

```bash
python3 -m compileall backend
cd frontend && npm install && npm run build
shuidi ideadock backend lint backend
shuidi ideadock frontend lint frontend/dist
```
