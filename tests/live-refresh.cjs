"use strict"

const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright")

const PAGE_ROOT = path.resolve(__dirname, "../pages/monitor")
const VIEWPORT = { width: 1440, height: 1000 }
const MIME_TYPES = {
  ".js": "application/javascript",
  ".css": "text/css",
  ".html": "text/html",
}
const POSITION_TOLERANCE = 2
const AUTO_REFRESH_MS = 5000

async function installFixture(page) {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url())
    const name = path.basename(url.pathname) || "index.html"
    if (name === "bridge-sdk.js") {
      await route.fulfill({ body: "", contentType: MIME_TYPES[".js"] })
      return
    }
    await route.fulfill({
      body: fs.readFileSync(path.join(PAGE_ROOT, name)),
      contentType: MIME_TYPES[path.extname(name)],
    })
  })
  await page.addInitScript(() => {
    const created = Date.now() / 1000 - 60
    window.fixture = {
      revision: 0,
      prepend: false,
      earlierCall: false,
      failure: false,
      modelCount: 30,
      requests: [],
    }
    function task(id) {
      return {
        task_id: id,
        created_at: created,
        started_at: created,
        status: "running",
        provider_model: "test-model",
        provider_id: "provider",
        platform_name: "test",
        sender_name: "测试用户",
        umo: "test:GroupMessage:1",
        llm_call_count: fixture.revision + 1,
        tool_call_count: 1,
      }
    }
    function call(id, started) {
      return {
        id,
        sequence: 1,
        started_at: started,
        status: "running",
        provider_model: "test-model",
        input_text: "待复制的上下文\n" + "历史聊天内容\n".repeat(200),
        output_text:
          "模型输出 " + fixture.revision + "\n" + "详细分析\n".repeat(200),
        input_other: 100,
        output: fixture.revision,
      }
    }
    window.AstrBotPluginPage = {
      ready: async () => ({ isDark: false }),
      apiGet: async (endpoint, query = {}) => {
        fixture.requests.push({ endpoint, query })
        if (fixture.failure) {
          throw new Error("test offline")
        }
        if (endpoint === "tasks") {
          const items = Array.from({ length: 50 }, (_, i) =>
            task("task-" + (Number(query.offset) + i)),
          )
          if (fixture.prepend) {
            items.unshift(task("new-task"))
            items.pop()
          }
          return { items, total: 100, has_more: Number(query.offset) === 0 }
        }
        if (endpoint === "summary") {
          return {
            tasks: 100,
            total_tokens: fixture.revision,
            models: [
              {
                provider_id: "provider",
                provider_model: "test-model",
                calls: fixture.revision,
              },
            ],
          }
        }
        if (endpoint === "filters") {
          return {
            models: Array.from(
              { length: fixture.modelCount },
              (_, i) => "model-" + i,
            ),
            platforms: ["test"],
          }
        }
        if (endpoint === "health" || endpoint === "self-check") {
          return { ok: true, status: "healthy", storage: {} }
        }
        if (endpoint.startsWith("tasks/")) {
          const calls = [call("call-1", created)]
          if (fixture.earlierCall) {
            calls.unshift(call("earlier", created - 1))
          }
          return {
            task: task(endpoint.slice(6)),
            llm_calls: calls,
            tool_calls: [
              {
                id: "tool-1",
                sequence: 1,
                tool_name: "<script>escaped</script>",
                started_at: created + 1,
                status: "completed",
                input_json: "{}",
                output_json: "result",
              },
            ],
          }
        }
        throw new Error("unexpected endpoint " + endpoint)
      },
    }
  })
}

async function run() {
  const launchOptions = { headless: true }
  if (process.env.CHROMIUM_EXECUTABLE_PATH) {
    launchOptions.executablePath = process.env.CHROMIUM_EXECUTABLE_PATH
  }
  const browser = await chromium.launch(launchOptions)
  const checks = []
  try {
    const context = await browser.newContext({
      viewport: VIEWPORT,
      permissions: ["clipboard-read", "clipboard-write"],
    })
    const page = await context.newPage()
    const errors = []
    page.on("pageerror", (error) => {
      errors.push(error.message)
    })
    await page.clock.install()
    await installFixture(page)
    await page.goto("https://monitor.test/")
    try {
      await page.locator("#rows button").first().waitFor()
    } catch (error) {
      console.error({
        errors,
        pageError: await page.locator("#error").textContent(),
      })
      throw error
    }

    await page.locator("#rows button").first().focus()
    await page.keyboard.press("Enter")
    await page.locator('[data-key="llm-call-1"] > summary').click()
    await page.evaluate(() => {
      const pre = document.querySelector(
        '[data-key="llm-call-1"] pre[data-key="input"]',
      )
      pre.scrollTop = 160
      const dialog = document.getElementById("task-dialog")
      dialog.scrollTop = 120
      window.savedInput = pre
      window.savedText = pre.firstChild
      window.savedDetails = pre.closest("details")
      window.savedScroll = { pre: pre.scrollTop, dialog: dialog.scrollTop }
      const range = document.createRange()
      range.setStart(pre.firstChild, 0)
      range.setEnd(pre.firstChild, 8)
      document.getSelection().removeAllRanges()
      document.getSelection().addRange(range)
      fixture.revision = 1
    })
    await page.clock.runFor(AUTO_REFRESH_MS)
    await page.waitForFunction(() =>
      document
        .querySelector('[data-key="llm-call-1"] pre[data-key="output"]')
        .textContent.startsWith("模型输出 1"),
    )
    const reading = await page.evaluate(() => ({
      sameInput:
        savedInput ===
        document.querySelector('[data-key="llm-call-1"] pre[data-key="input"]'),
      sameText: savedText === savedInput.firstChild,
      sameDetails: savedDetails === savedInput.closest("details"),
      open: savedDetails.open,
      selection: document.getSelection().toString(),
      preDelta: savedInput.scrollTop - savedScroll.pre,
      dialogDelta:
        document.getElementById("task-dialog").scrollTop - savedScroll.dialog,
    }))
    assert.equal(
      reading.sameInput &&
        reading.sameText &&
        reading.sameDetails &&
        reading.open,
      true,
    )
    assert.equal(reading.selection, "待复制的上下文\n")
    assert.ok(Math.abs(reading.preDelta) <= POSITION_TOLERANCE)
    assert.ok(Math.abs(reading.dialogDelta) <= POSITION_TOLERANCE)
    await page.keyboard.press("Control+c")
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      reading.selection,
    )
    checks.push(
      "automatic refresh preserves actual text nodes, selection, clipboard, expansion and scroll while other data updates",
    )

    await page.evaluate(async () => {
      const pre = document.querySelector(
        '[data-key="llm-call-1"] pre[data-key="output"]',
      )
      document.getSelection().removeAllRanges()
      const range = document.createRange()
      range.setStart(pre.firstChild, 0)
      range.setEnd(pre.firstChild, 6)
      document.getSelection().addRange(range)
      fixture.revision = 2
      await refresh()
      fixture.revision = 3
      await refresh()
    })
    assert.match(
      await page
        .locator('[data-key="llm-call-1"] pre[data-key="output"]')
        .textContent(),
      /^模型输出 1/,
    )
    assert.equal(await page.locator("#sum-tokens").textContent(), "3")
    await page.evaluate(() => document.getSelection().removeAllRanges())
    await page.clock.runFor(50)
    await page.waitForFunction(() =>
      document
        .querySelector('[data-key="llm-call-1"] pre[data-key="output"]')
        .textContent.startsWith("模型输出 3"),
    )
    checks.push(
      "selected changing payload stays readable; releasing selection applies only the latest update",
    )

    const output = page.locator(
      '[data-key="llm-call-1"] pre[data-key="output"]',
    )
    await output.scrollIntoViewIfNeeded()
    const outputBounds = await output.boundingBox()
    await page.mouse.move(outputBounds.x + 15, outputBounds.y + 15)
    await page.mouse.down()
    await page.evaluate(async () => {
      fixture.revision = 4
      await refresh()
    })
    assert.match(await output.textContent(), /^模型输出 3/)
    await page.mouse.up()
    await page.evaluate(() => document.getSelection().removeAllRanges())
    await page.clock.runFor(50)
    await page.waitForFunction(() =>
      document
        .querySelector('[data-key="llm-call-1"] pre[data-key="output"]')
        .textContent.startsWith("模型输出 4"),
    )
    checks.push("pointer-down protects text before a drag selection has formed")

    await page.evaluate(async () => {
      const pre = document.querySelector(
        '[data-key="llm-call-1"] pre[data-key="input"]',
      )
      pre.scrollIntoView()
      window.beforeTop = pre.getBoundingClientRect().top
      fixture.earlierCall = true
      await refresh()
    })
    assert.ok(
      await page.evaluate(
        (tolerance) =>
          Math.abs(savedInput.getBoundingClientRect().top - beforeTop) <=
          tolerance,
        POSITION_TOLERANCE,
      ),
    )
    assert.equal(await page.locator("#detail script").count(), 0)
    checks.push(
      "inserting earlier calls anchors the visible content and keeps payload escaping",
    )

    await page.locator("#close-detail").click()
    await page.waitForFunction(() => state.selected === null)
    await page.evaluate(async () => {
      const row = document.querySelector('[data-task-id="task-10"]')
      row.scrollIntoView()
      row.querySelector("button").focus({ preventScroll: true })
      window.savedRow = row
      window.savedFocus = document.activeElement
      window.rowTop = row.getBoundingClientRect().top
      fixture.prepend = true
      await refresh()
    })
    const rowState = await page.evaluate(() => ({
      connected: savedRow.isConnected,
      focused: document.activeElement === savedFocus,
      delta: savedRow.getBoundingClientRect().top - rowTop,
      scrollTop: document.scrollingElement.scrollTop,
    }))
    assert.ok(
      rowState.connected &&
        rowState.focused &&
        Math.abs(rowState.delta) <= POSITION_TOLERANCE,
      JSON.stringify(rowState),
    )
    await page.evaluate(async () => {
      const summary = await bridge.apiGet("summary")
      summary.models.push({
        provider_id: "second",
        provider_model: "new-model",
        calls: 1,
      })
      renderSummary(summary)
    })
    assert.ok(
      await page.evaluate(
        (tolerance) =>
          Math.abs(savedRow.getBoundingClientRect().top - rowTop) <= tolerance,
        POSITION_TOLERANCE,
      ),
    )
    checks.push(
      "new tasks and model rows preserve existing rows, keyboard focus and viewport anchor",
    )

    await page.locator("#model-toggle").click()
    await page.evaluate(async () => {
      const menu = document.querySelector("#model-options .menu-options")
      const checkbox = document.querySelector(
        '#model-options input[value="model-20"]',
      )
      checkbox.checked = true
      checkbox.focus()
      menu.scrollTop = 200
      window.savedCheckbox = checkbox
      window.menuScroll = menu.scrollTop
      fixture.modelCount += 1
      await refresh()
    })
    assert.ok(
      await page.evaluate(
        () =>
          savedCheckbox === document.activeElement &&
          savedCheckbox.checked &&
          !document.getElementById("model-options").hidden &&
          document.querySelector("#model-options .menu-options").scrollTop ===
            menuScroll,
      ),
    )
    assert.equal(
      await page.locator('#model-options input[value="model-30"]').count(),
      1,
    )
    const before = await page.evaluate(
      () => fixture.requests.filter((r) => r.endpoint === "tasks").length,
    )
    await page.locator('#model-options [data-filter-action="clear"]').click()
    await page.waitForFunction(
      (count) =>
        fixture.requests.filter((r) => r.endpoint === "tasks").length > count,
      before,
    )
    assert.equal(
      await page.evaluate(
        () => fixture.requests.filter((r) => r.endpoint === "tasks").length,
      ),
      before + 1,
    )
    assert.equal(await page.locator("#model-options input:checked").count(), 0)
    checks.push(
      "filter refresh keeps menu, checkbox focus, scroll and selection; delegated listeners fire once",
    )

    await page.evaluate(async () => {
      fixture.failure = true
      await refresh()
    })
    assert.match(await page.locator("#error").textContent(), /test offline/)
    assert.equal(await page.locator("#rows button").count(), 50)
    await page.evaluate(() => {
      fixture.failure = false
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.evaluate(() => selectTask("task-0"))
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    )
    assert.equal(errors.length, 0, errors.join("\n"))
    checks.push(
      "refresh errors retain data; mobile details remain within the viewport",
    )
    console.log(JSON.stringify({ passed: checks, pageErrors: errors }, null, 2))
  } finally {
    await browser.close()
  }
}

run().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
