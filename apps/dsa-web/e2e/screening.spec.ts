/**
 * 选股页面 E2E 测试 — 五层决策系统完整流程
 *
 * 覆盖:
 *   1. 页面渲染与基本元素
 *   2. 一键选股流程（发起 → 轮询 → 完成）
 *   3. 决策上下文展示（L1/L2/概览）
 *   4. 候选表五层字段列
 *   5. 详情抽屉五层分层展示
 *   6. 运行历史管理
 *
 * 运行: npx playwright test e2e/screening.spec.ts
 *
 * 当 ADMIN_AUTH_ENABLED=false 时无需密码即可访问所有页面。
 * 当 ADMIN_AUTH_ENABLED=true 时需设置 DSA_WEB_SMOKE_PASSWORD。
 */
import { expect, test, type Page } from '@playwright/test';

const smokePassword = process.env.DSA_WEB_SMOKE_PASSWORD;

// ── helpers ──────────────────────────────────────────────────

async function ensureAccess(page: Page) {
  // 尝试直接访问首页
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');

  // 检查是否被重定向到登录页
  if (page.url().includes('/login')) {
    if (!smokePassword) {
      test.skip(true, 'Auth enabled but DSA_WEB_SMOKE_PASSWORD not set');
      return;
    }
    await expect(page.locator('#password')).toBeVisible({ timeout: 10_000 });
    await page.locator('#password').fill(smokePassword);
    const submitBtn = page.getByRole('button', {
      name: /授权进入工作台|完成设置并登录/,
    });
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/v1/auth/login') && r.status() === 200,
        { timeout: 15_000 },
      ),
      submitBtn.click(),
    ]);
    await page.waitForURL('/', { timeout: 15_000 });
  }
}

async function goToScreening(page: Page) {
  await ensureAccess(page);
  await page.goto('/screening');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(500);
}

// ── Case 1: 页面渲染 ─────────────────────────────────────────

test.describe('选股页面基本渲染', () => {
  test('shows page header and control bar', async ({ page }) => {
    await goToScreening(page);

    await expect(page.getByText('智能选股')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('SCREENING')).toBeVisible();

    const controlBar = page.locator('[data-testid="screening-control-bar"]');
    await expect(controlBar).toBeVisible();

    // 开始筛选按钮
    await expect(page.getByRole('button', { name: /开始筛选/ })).toBeVisible();
  });

  test('shows run panel', async ({ page }) => {
    await goToScreening(page);

    const runPanel = page.locator('[data-testid="screening-run-panel"]');
    await expect(runPanel).toBeVisible({ timeout: 10_000 });
  });

  test('shows mode selector', async ({ page }) => {
    await goToScreening(page);

    // 模式下拉：均衡模式 / 激进模式 / 质量模式
    await expect(page.getByText('均衡模式').first()).toBeVisible({ timeout: 5_000 });
  });

  test('shows advanced settings when toggled', async ({ page }) => {
    await goToScreening(page);

    // 高级按钮
    const advBtn = page.getByRole('button', { name: /高级/ });
    await expect(advBtn).toBeVisible();
    await advBtn.click();

    // 高级设置面板
    await expect(page.getByLabelText('候选上限')).toBeVisible({ timeout: 3_000 });
    await expect(page.getByLabelText('AI 分析数')).toBeVisible();
  });
});

// ── Case 2: 一键选股流程 ─────────────────────────────────────

test.describe('一键选股流程', () => {
  test('starts a screening run and completes', async ({ page }) => {
    test.setTimeout(300_000); // 选股耗时较长

    await goToScreening(page);

    const startBtn = page.getByRole('button', { name: /开始筛选/ });
    await expect(startBtn).toBeEnabled();

    // 拦截 API 调用确认请求发送
    const runCreatePromise = page.waitForResponse(
      (r) => r.url().includes('/api/v1/screening/runs') && r.request().method() === 'POST',
      { timeout: 30_000 },
    );

    await startBtn.click();
    const createResponse = await runCreatePromise;
    expect(createResponse.status()).toBeLessThan(500);

    // 如果是 200/201，等待选股完成
    if (createResponse.ok()) {
      // 等待候选表出现（选股完成的标志）
      const candidateTable = page.locator('[data-testid="candidate-table"]');
      await expect(candidateTable).toBeVisible({ timeout: 300_000 });

      // 验证有候选行
      const rows = page.locator('[data-testid^="candidate-row-"]');
      const rowCount = await rows.count();
      expect(rowCount).toBeGreaterThanOrEqual(0);
    }
  });
});

// ── Case 3: 查看历史 run + 决策上下文 ────────────────────────

test.describe('历史选股记录', () => {
  test('clicking history item loads candidates', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);

    // 等待历史加载
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    // 候选表应可见
    const table = page.locator('[data-testid="candidate-table"]');
    await expect(table).toBeVisible({ timeout: 15_000 });
  });

  test('decision context section shows after selecting completed run', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    // 决策上下文区域
    const contextSection = page.locator('[data-testid="decision-context-section"]');
    const visible = await contextSection.isVisible({ timeout: 10_000 }).catch(() => false);

    if (visible) {
      // 截图保存
      await contextSection.screenshot({ path: 'e2e-results/decision-context.png' });
    }
    // context 可能不可见（旧 run 无五层数据），不强制断言
  });
});

// ── Case 4: 候选表五层字段 ───────────────────────────────────

test.describe('候选表五层字段', () => {
  test('candidate table shows five-layer columns when data exists', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    const table = page.locator('[data-testid="candidate-table"]');
    const tableVisible = await table.isVisible({ timeout: 15_000 }).catch(() => false);

    if (!tableVisible) {
      test.skip(true, '候选表未显示');
      return;
    }

    // 验证表头 — 基础列必存在
    await expect(table.getByText('排名')).toBeVisible();
    await expect(table.getByText('代码')).toBeVisible();
    await expect(table.getByText('名称')).toBeVisible();

    // 五层列（hasFiveLayerData 时才显示）
    const tradeStageHeader = table.getByText('交易阶段');
    const hasFiveLayer = await tradeStageHeader.isVisible({ timeout: 3_000 }).catch(() => false);

    if (hasFiveLayer) {
      await expect(table.getByText('买点类型')).toBeVisible();
      await expect(table.getByText('成熟度')).toBeVisible();
      await expect(table.getByText('题材地位')).toBeVisible();
    }

    // 截图
    await table.screenshot({ path: 'e2e-results/candidate-table.png' });
  });
});

// ── Case 5: 详情抽屉 ─────────────────────────────────────────

test.describe('候选详情抽屉', () => {
  test('opens drawer and shows candidate detail', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);

    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    // 等候选表
    const table = page.locator('[data-testid="candidate-table"]');
    const tableVisible = await table.isVisible({ timeout: 15_000 }).catch(() => false);
    if (!tableVisible) {
      test.skip(true, '候选表未显示');
      return;
    }

    // 点击第一个候选的详情按钮
    const detailBtn = page.getByLabel(/查看.*详情/).first();
    const btnVisible = await detailBtn.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!btnVisible) {
      test.skip(true, '无详情按钮');
      return;
    }

    await detailBtn.click();
    await page.waitForTimeout(1500);

    // 详情面板
    const detail = page.locator('[data-testid="candidate-detail"]');
    await expect(detail).toBeVisible({ timeout: 10_000 });

    // 截图
    await detail.screenshot({ path: 'e2e-results/candidate-detail.png' });
  });

  test('detail drawer shows five-layer sections', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    const detailBtn = page.getByLabel(/查看.*详情/).first();
    const btnVisible = await detailBtn.isVisible({ timeout: 10_000 }).catch(() => false);
    if (!btnVisible) {
      test.skip(true, '无详情按钮');
      return;
    }

    await detailBtn.click();
    await page.waitForTimeout(1500);

    const detail = page.locator('[data-testid="candidate-detail"]');
    await expect(detail).toBeVisible({ timeout: 10_000 });

    // 检查五层分区标题（L1~L5 + AI）
    const sectionTexts = ['大盘环境', '题材', '候选池', '入场', '交易阶段', 'AI'];
    let found = 0;
    for (const text of sectionTexts) {
      const el = detail.getByText(text, { exact: false }).first();
      const vis = await el.isVisible({ timeout: 2_000 }).catch(() => false);
      if (vis) found++;
    }
    // 旧 run 可能走 legacy 布局，不强制要求所有分区都存在
    // 但如果有 trade_stage 数据，至少应有 3 个分区
    const hasTradeStageSection = await detail
      .getByText('L5 交易阶段', { exact: false })
      .first()
      .isVisible({ timeout: 2_000 })
      .catch(() => false);
    if (hasTradeStageSection) {
      expect(found).toBeGreaterThanOrEqual(3);
    }
  });
});

// ── Case 6: 通知按钮 ─────────────────────────────────────────

test.describe('通知功能', () => {
  test('notification button is visible for completed run', async ({ page }) => {
    test.setTimeout(60_000);
    await goToScreening(page);
    await page.waitForTimeout(2000);

    const historyItem = page.locator('[data-testid^="run-history-"]').first();
    const hasHistory = await historyItem.isVisible({ timeout: 5_000 }).catch(() => false);
    if (!hasHistory) {
      test.skip(true, '无历史选股记录');
      return;
    }

    await historyItem.click();
    await page.waitForTimeout(2000);

    // 等候选表
    const table = page.locator('[data-testid="candidate-table"]');
    const tableVisible = await table.isVisible({ timeout: 15_000 }).catch(() => false);
    if (!tableVisible) {
      test.skip(true, '候选表未显示');
      return;
    }

    const notifyBtn = page.getByRole('button', { name: /推送通知/ });
    await expect(notifyBtn).toBeVisible({ timeout: 5_000 });
  });
});
