import { defineConfig } from "@playwright/test";

// 站点 e2e：webServer 拉起 zx-site（口令用测试令牌，页面与鉴权流程都可测；
// 行情数据依赖本机数据根 ZX_DATA_ROOT，与采集端同一根——ADR-0006）。
const port = 8010;

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "uv run --frozen zx-site",
    port,
    reuseExistingServer: true,
    env: {
      ZX_DATA_ROOT: process.env.ZX_DATA_ROOT || "/home/lei/zhixing_data",
      ZX_SITE_HOST: "127.0.0.1",
      ZX_SITE_PORT: String(port),
      ZX_SITE_TOKEN: process.env.ZX_E2E_TOKEN || "e2e-local-token",
    },
  },
});
