import { test, expect } from "@playwright/test";

// 新前端（Vue 3 + Vite，ADR-0018）的冒烟。旧的 site.spec.ts 绑的是 site/static 那套 DOM id
// （#search/#suggest/#prompt-zone/…），新前端除挂载点外一个 id 都不用，所以两套并行：
// 旧件退役（05 Q1-② 批准删除）时把 site.spec.ts 一起删。
//
// 定位一律走 data-testid：class 是视觉的，改样式不该弄红测试。
// 行情与提示词的数据正确性由 Python 侧对照测试保证（tests/test_site_prompt.py），这里只测壳。
// 口令鉴权已按 ADR-0019 撤销：没有口令框、没有口令可填，页面加载即 boot。

test("页面加载且关键控件在位、没有口令框", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/知行/);
  await expect(page.locator("#app")).toBeAttached();
  await expect(page.locator('[data-testid="search"]')).toBeVisible();
  // 口令框不许再出现（ADR-0019）
  await expect(page.locator('[data-testid="token"]')).toHaveCount(0);
  await expect(page.locator(".seal")).toContainText("知行");
});

test("不填任何口令直接搜出候选", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] button').first()).toBeVisible({
    timeout: 10_000,
  });
  // 状态条不许出现鉴权话术
  await expect(page.locator('[role="status"]')).not.toContainText("口令");
});

test("页面加载完成即为已连接（无口令可填）", async ({ page }) => {
  await page.goto("/");
  // boot 不再等口令：模板与留痕加载成功，绿点亮起
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
});

test("搜出候选、可选、生成出提示词", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });

  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] button').first()).toBeVisible({
    timeout: 10_000,
  });
  await page.screenshot({ path: "test-results/vue-search-suggest.png" });

  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  // 面板那一行由服务端 /api/kline 的 quote 渲染（docs/11 §六-2：壳不自己推涨跌幅）
  await expect(page.locator('[data-testid="quote"]')).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('[data-testid="quote"]')).toContainText("%");
  await expect(page.locator('[data-testid="templates"]')).toBeVisible();
  await page.screenshot({ path: "test-results/vue-prompt.png", fullPage: true });
});

test("选过票之后留痕出现在页面上（06 §四）", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });

  await page.locator('[data-testid="search"]').fill("");
  await expect(page.getByText("最近搜过")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /茅台/ }).first()).toBeVisible();
});
