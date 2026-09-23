import { test, expect } from "@playwright/test";

// 新前端（Vue 3 + Vite，ADR-0018）的冒烟。旧的 site.spec.ts 绑的是 site/static 那套 DOM id
// （#search/#token/#suggest/#prompt-zone/…），新前端除挂载点外一个 id 都不用，所以两套并行：
// 旧件退役（05 Q1-② 批准删除）时把 site.spec.ts 一起删。
//
// 定位一律走 data-testid：class 是视觉的，改样式不该弄红测试。
// 行情与提示词的数据正确性由 Python 侧对照测试保证（tests/test_site_prompt.py），这里只测壳。

const TOKEN = process.env.ZX_E2E_TOKEN || "e2e-local-token";

test("页面加载且关键控件在位", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/知行/);
  await expect(page.locator("#app")).toBeAttached();
  await expect(page.locator('[data-testid="search"]')).toBeVisible();
  await expect(page.locator('[data-testid="token"]')).toBeVisible();
  await expect(page.locator(".seal")).toContainText("知行");
});

test("无口令时明确提示而不是静默空转", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[role="status"]')).toContainText("口令不对", { timeout: 5_000 });
});

test("口令清空时连接状态跟着掉下去", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="token"]').fill(TOKEN);
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  // 清空口令 = 没口令：绿点不许继续亮着说「已连接」（A6）
  await page.locator('[data-testid="token"]').fill("");
  await expect(page.locator(".dot-off")).toBeVisible({ timeout: 5_000 });
});

test("带正确口令后搜出候选、可选、生成出提示词", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="token"]').fill(TOKEN);
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });

  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] button').first()).toBeVisible({
    timeout: 10_000,
  });
  await page.screenshot({ path: "test-results/vue-search-suggest.png" });

  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  await expect(page.locator('[data-testid="templates"]')).toBeVisible();
  await page.screenshot({ path: "test-results/vue-prompt.png", fullPage: true });
});

test("选过票之后留痕出现在页面上（06 §四）", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="token"]').fill(TOKEN);
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });

  await page.locator('[data-testid="search"]').fill("");
  await expect(page.getByText("最近搜过")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /茅台/ }).first()).toBeVisible();
});
