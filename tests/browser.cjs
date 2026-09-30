"use strict";
const { chromium } = require(
  process.env.PLAYWRIGHT_MODULE_PATH || "playwright",
);
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const pageRoot = path.resolve(__dirname, "../pages/monitor");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const checks = [];
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      const name = path.basename(url.pathname) || "index.html";
      if (name === "bridge-sdk.js")
        return route.fulfill({
          contentType: "application/javascript",
          body: "",
        });
      const contentType = name.endsWith(".js")
        ? "application/javascript"
        : name.endsWith(".css")
          ? "text/css"
          : "text/html";
      await route.fulfill({
        contentType,
        body: fs.readFileSync(path.join(pageRoot, name)),
      });
    });
    await page.addInitScript(() => {
      window.fixture = {
        completed: false,
        failure: false,
        unhealthy: false,
        detailFailure: false,
        checkFailure: false,
        race: false,
        detailRace: false,
        requests: [],
      };
      const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const task = (id) => ({
        task_id: id,
        created_at: Date.now() / 1000 - 60,
        started_at: Date.now() / 1000 - 55,
        status: fixture.completed ? "completed" : "running",
        finished_at: fixture.completed ? Date.now() / 1000 : null,
        provider_model: id,
        provider_id: "test-provider",
        platform_name: "test",
        sender_name: "Example user",
        umo: "test:GroupMessage:1",
        llm_call_count: 1,
        tool_call_count: 1,
      });
      const health = () => ({
        ok: !fixture.unhealthy,
        status: fixture.unhealthy ? "degraded" : "healthy",
        enabled: true,
        reply_filter: true,
        probe: { enabled: true },
        retry_coverage: {
          ok: true,
          framework: true,
          request: true,
          http: true,
          provider_classes: ["ProviderOpenAIOfficial"],
        },
        storage: { ok: !fixture.unhealthy, failed: fixture.unhealthy ? 3 : 0 },
      });
      window.AstrBotPluginPage = {
        ready: async () => ({ isDark: false }),
        apiGet: async (endpoint, query = {}) => {
          fixture.requests.push({ endpoint, query });
          if (endpoint === "health") return health();
          if (endpoint === "self-check") {
            if (fixture.checkFailure) throw Error("diagnostics unavailable");
            return health();
          }
          if (endpoint === "summary")
            return {
              tasks: 55,
              llm_calls: 55,
              total_tokens: 880,
              running_tasks: 1,
              failed_calls: 3,
              fallback_calls: 2,
              retry_calls: 1,
              provider_retries: 2,
              request_retries: 3,
              http_retries: 4,
              models: [
                {
                  provider_id: "test-provider",
                  provider_model: "test-model",
                  calls: 55,
                  running: 1,
                  errors: 3,
                  input_other: 600,
                  input_cached: 80,
                  output: 200,
                  avg_ttft: null,
                  p50: 1.5,
                  p95: 4,
                  retry_calls: 1,
                  provider_retries: 2,
                  request_retries: 3,
                  http_retries: 4,
                },
              ],
            };
          if (endpoint === "tasks") {
            if (fixture.failure) throw Error("backend unavailable");
            if (fixture.race) await pause(query.model === "old" ? 120 : 10);
            const items = Array.from(
              { length: query.offset ? 5 : 50 },
              (_, i) => task(query.model || "task-" + (query.offset + i)),
            );
            return {
              items,
              total: 55,
              limit: 50,
              offset: query.offset,
              has_more: !query.offset,
            };
          }
          if (endpoint.startsWith("tasks/")) {
            const id = endpoint.slice(6);
            const snapshot = task(id);
            if (fixture.detailRace) await pause(id === "old" ? 120 : 10);
            if (fixture.detailFailure) throw Error("detail unavailable");
            return {
              task: snapshot,
              llm_calls: [
                {
                  id: "call-1",
                  sequence: 1,
                  status: snapshot.status,
                  provider_model: "test-model",
                  started_at: snapshot.started_at,
                  duration: 1.3,
                  ttft: null,
                  input_other: 5,
                  output: 2,
                  round_id: "round-1",
                  is_retry: true,
                  is_fallback: true,
                  attempt_number: 2,
                  retry_reason: "EmptyModelOutputError: empty",
                  retry_wait: 1.01,
                  retry_wait_planned: 1,
                  wait_kind: "backoff",
                },
              ],
              retry_attempts: [
                {
                  id: "adapter-1",
                  llm_call_id: "call-1",
                  parent_id: null,
                  layer: "provider",
                  status: "completed",
                  attempt_number: 1,
                  started_at: snapshot.started_at,
                  duration: 1,
                },
                {
                  id: "request-1",
                  llm_call_id: "call-1",
                  parent_id: "adapter-1",
                  layer: "request",
                  status: "completed",
                  is_retry: true,
                  attempt_number: 2,
                  started_at: snapshot.started_at,
                  duration: 0.8,
                  retry_reason: "503 <script>bad</script>",
                  retry_wait: 0.6,
                  retry_wait_planned: 0.5,
                  wait_kind: "backoff",
                  retry_wait_status: "cancelled",
                  next_retry_wait: 2,
                  next_retry_wait_actual: 0.02,
                },
                {
                  id: "http-1",
                  llm_call_id: "call-1",
                  parent_id: "request-1",
                  layer: "http",
                  status: "completed",
                  is_retry: true,
                  attempt_number: 3,
                  started_at: snapshot.started_at,
                  duration: 0.2,
                  retry_reason: "HTTP 503",
                  retry_wait: 0.1,
                  wait_kind: "sdk_gap",
                  http_status: 200,
                  operation: "POST",
                },
              ],
              tool_calls: [
                {
                  id: "tool-1",
                  sequence: 1,
                  tool_name: "<script>bad</script>",
                  started_at: snapshot.started_at + 2,
                  status: "error",
                  duration: 0.2,
                  input_json: '{"q":"example"}',
                  output_json: '{"isError":true}',
                },
              ],
            };
          }
          throw Error("unexpected endpoint: " + endpoint);
        },
      };
    });
    await page.goto("http://monitor.test/");
    await page.locator("#rows button").first().waitFor();
    assert.equal(await page.locator("#rows button").count(), 50);
    await page.locator("#next").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#rows button").length === 5,
    );
    assert.match(await page.locator("#page-info").textContent(), /2 \/ 2/);
    await page.locator("#prev").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#rows button").length === 50,
    );
    checks.push("pagination reaches all 55 tasks");

    const first = page.locator("#rows button").first();
    await first.focus();
    await page.keyboard.press("Enter");
    await page.locator(".detail-summary").waitFor();
    await page
      .locator("#detail details")
      .first()
      .locator(":scope > summary")
      .click();
    for (const id of ["adapter-1", "request-1", "http-1"]) {
      await page.locator('[data-key="attempt-' + id + '"] > summary').click();
    }
    await page.evaluate(async () => {
      fixture.completed = true;
      await refresh();
    });
    assert.match(await page.locator(".detail-summary").textContent(), /已完成/);
    assert.equal(
      await page.locator("#detail details").first().getAttribute("open"),
      "",
    );
    assert.equal(await page.locator("#detail script").count(), 0);
    assert.equal(await page.locator("#detail .badge.error").count(), 1);
    checks.push(
      "keyboard selection; polling updates open detail and preserves expansion; payload escaping",
    );
    for (const [id, count] of [
      ["retry", 1],
      ["provider", 2],
      ["request", 3],
      ["http", 4],
    ]) {
      assert.equal(
        await page.locator("#sum-" + id).textContent(),
        String(count),
      );
    }
    assert.equal(
      await page
        .locator('[data-key="llm-call-1"] > summary .badge.retry')
        .count(),
      1,
    );
    assert.equal(
      await page
        .locator('[data-key="llm-call-1"] > summary .badge.fallback')
        .count(),
      1,
    );
    assert.equal(
      await page
        .locator(
          '[data-key="attempt-adapter-1"] [data-key="attempt-request-1"] [data-key="attempt-http-1"]',
        )
        .count(),
      1,
    );
    assert.equal(
      await page.locator('[data-key="attempt-http-1"]').getAttribute("open"),
      "",
    );
    assert.match(
      await page.locator('[data-key="attempt-request-1"]').textContent(),
      /等待已取消/,
    );
    assert.match(
      await page.locator('[data-key="attempt-http-1"]').textContent(),
      /HTTP 200/,
    );
    checks.push(
      "four retry counters, simultaneous fallback and retry, nested attempts, cancelled waits and expansion persist",
    );

    await page.evaluate(async () => {
      fixture.detailRace = true;
      await Promise.all([selectTask("old"), selectTask("new")]);
    });
    assert.equal(await page.locator(".detail-summary h3").textContent(), "new");
    await page.evaluate(async () => {
      fixture.detailRace = false;
      fixture.detailFailure = true;
      await refreshDetail("new");
    });
    assert.match(
      await page.locator("#detail-error").textContent(),
      /detail unavailable/,
    );
    assert.equal(await page.locator(".detail-summary h3").textContent(), "new");
    await page.keyboard.press("Escape");
    await page.waitForFunction(
      () => !document.getElementById("task-dialog").open,
    );
    checks.push(
      "latest selected detail wins; failed detail remains visible with error; Escape closes dialog",
    );

    await page.evaluate(async () => {
      fixture.race = true;
      document.getElementById("model").value = "old";
      const old = refresh();
      document.getElementById("model").value = "new";
      await Promise.all([old, refresh()]);
    });
    assert.equal(
      await page.locator("#rows tr td:nth-child(2)").first().innerText(),
      "new\ntest-provider",
    );
    assert.equal(
      await page.evaluate(
        () =>
          fixture.requests.filter((r) => r.endpoint === "summary").at(-1).query
            .model,
      ),
      "new",
    );
    checks.push(
      "stale list response discarded; list and summary share filters",
    );

    await page.evaluate(async () => {
      fixture.failure = true;
      await refresh();
    });
    assert.equal(
      await page.locator("#health").getAttribute("data-state"),
      "degraded",
    );
    assert.match(
      await page.locator("#error").textContent(),
      /backend unavailable/,
    );
    await page.evaluate(async () => {
      fixture.failure = false;
      fixture.unhealthy = true;
      await refresh();
    });
    assert.match(await page.locator("#health-text").textContent(), /丢失 3 条/);
    await page.evaluate(async () => {
      fixture.checkFailure = true;
      await selfCheck();
    });
    assert.match(
      await page.locator("#check-result").textContent(),
      /diagnostics unavailable/,
    );
    assert.equal(errors.length, 0, errors.join("\n"));
    checks.push(
      "refresh, storage loss and self-check failures visible without unhandled exceptions",
    );

    await page.evaluate(async () => {
      fixture.unhealthy = false;
      fixture.race = false;
      fixture.detailFailure = false;
      fixture.checkFailure = false;
      document.getElementById("model").value = "";
      await refresh();
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#rows button").first().click();
    await page.locator(".detail-summary").waitFor();
    const bounds = await page.locator("#task-dialog").boundingBox();
    assert.ok(bounds.width <= 390 && bounds.y < 2);
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    assert.ok(await page.locator("#close-detail").isVisible());
    assert.ok(
      await page.locator("#model").evaluate((node) => node.labels.length > 0),
    );
    checks.push(
      "390px mobile layout has no page overflow and opens full-screen details",
    );
    if (process.env.MONITOR_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.MONITOR_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({
        path: path.join(process.env.MONITOR_SCREENSHOT_DIR, "mobile.png"),
      });
      await page.locator("#close-detail").click();
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.locator("#check").click();
      await page.waitForFunction(() =>
        document
          .getElementById("check-result")
          .textContent.includes("检查通过"),
      );
      assert.match(
        await page.locator("#check-result").textContent(),
        /重试探针：已安装/,
      );
      await page.screenshot({
        path: path.join(process.env.MONITOR_SCREENSHOT_DIR, "desktop.png"),
      });
      await page.evaluate(
        () => (document.documentElement.dataset.theme = "dark"),
      );
      await page.screenshot({
        path: path.join(process.env.MONITOR_SCREENSHOT_DIR, "dark.png"),
      });
    }
    console.log(
      JSON.stringify({ passed: checks, pageErrors: errors }, null, 2),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
