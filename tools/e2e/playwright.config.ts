import { defineConfig } from "@playwright/test";

// 站点 e2e：**两个壳各起一个 zx-site**（ADR-0018 代价二的并存期），各测各的。
//
// 为什么要两个服务：旧壳 `site/static` 与新壳 `frontend/dist` 的 DOM 一个 id 都不重叠
// （site.spec.ts 走 `#search/#suggest`，site-vue.spec.ts 走 `data-testid`），
// 共用一个服务就必然红一半——2026-09-24 实测 dist 在场时旧壳那套 3 条全红。`ZX_SITE_SHELL`
// 就是为这件事开的定点开关：指名了就不许退回去发另一个壳。
//
// 行情数据依赖本机数据根 ZX_DATA_ROOT，与采集端同一根（ADR-0006）。
// 口令鉴权已按 ADR-0019 撤销：服务不要求 ZX_SITE_TOKEN，测试也不再发 X-Token。
const builtPort = 8010;
const legacyPort = 8011;
const dataRoot = process.env.ZX_DATA_ROOT || "/home/lei/zhixing_data";

function serve(port: number, shell: "built" | "legacy") {
  return {
    command: "uv run --frozen zx-site",
    port,
    reuseExistingServer: true,
    env: {
      ZX_DATA_ROOT: dataRoot,
      ZX_SITE_HOST: "127.0.0.1",
      ZX_SITE_PORT: String(port),
      ZX_SITE_SHELL: shell,
    },
  };
}

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    screenshot: "only-on-failure",
  },
  webServer: [serve(builtPort, "built"), serve(legacyPort, "legacy")],
  projects: [
    {
      name: "vue",
      testMatch: /\/site-vue\.spec\.ts$/,
      use: { baseURL: `http://127.0.0.1:${builtPort}` },
    },
    {
      name: "legacy",
      testMatch: /\/site\.spec\.ts$/,
      use: { baseURL: `http://127.0.0.1:${legacyPort}` },
    },
  ],
});
