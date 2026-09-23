# 知行站点前端

Vue 3 + TypeScript + Vite + shadcn-vue 约定（见 [ADR-0018](../docs/adr/0018-前端技术栈选型.md)）。
视觉与交互以 [docs/11-设计规范.md](../docs/11-设计规范.md) 为准。

- 开发：先在本仓跑 `ZX_SITE_TOKEN=… uv run zx-site`（127.0.0.1:8000），再 `npm run dev`（5173，`/api` 已代理）。
- 构建：`npm run build`（`vue-tsc` 类型检查 + `vite build`），产物 `dist/`。
- 壳的契约不变：只调 `/api/*`、`X-Token` 头、**不在前端产生任何口径**（ADR-0013 决定 5）。
