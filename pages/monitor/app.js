/* The AstrBot bridge handles authentication and theme updates inside the iframe. */
"use strict";
const bridge = window.AstrBotPluginPage;
const byId = (id) => document.getElementById(id);
const state = {
  offset: 0,
  limit: 50,
  generation: 0,
  detailGeneration: 0,
  selected: null,
  paused: false,
  timer: null,
  lastSuccess: null,
  total: 0,
};
const labels = {
  running: "运行中",
  completed: "已完成",
  error: "失败",
  aborted: "已停止",
  cancelled: "已取消",
  interrupted: "中断",
  recovered: "异常恢复",
  empty: "无返回值",
};
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const num = (value) => Number(value || 0).toLocaleString();
const when = (value) => (value ? new Date(value * 1000).toLocaleString() : "—");
const seconds = (value) =>
  value == null ? "—" : Number(value).toFixed(2) + " s";
const duration = (item) =>
  seconds(
    item.duration ??
      Math.max(
        0,
        (item.finished_at || Date.now() / 1000) -
          (item.started_at || item.created_at || Date.now() / 1000),
      ),
  );
const badge = (status, label) =>
  '<span class="badge ' +
  esc(status) +
  '">' +
  esc(label || labels[status] || status) +
  "</span>";
const clock = (item) =>
  "<span" +
  (item.status === "running"
    ? ' data-clock-start="' + esc(item.started_at || item.created_at) + '"'
    : "") +
  ">" +
  duration(item) +
  "</span>";

function filters() {
  return {
    hours: Number(byId("hours").value),
    status: byId("status").value,
    model: byId("model").value.trim(),
    platform: byId("platform").value.trim(),
  };
}

function message(id, text) {
  const node = byId(id);
  node.hidden = !text;
  node.textContent = text;
}

function setHealth(health) {
  byId("health").dataset.state = health.status;
  const text = {
    healthy: "采集正常",
    disabled: "采集已关闭",
    degraded: "监控异常",
    stopped: "服务已停止",
  };
  const losses =
    Number(health.storage?.failed || 0) + Number(health.storage?.dropped || 0);
  byId("health-text").textContent =
    (text[health.status] || "状态未知") +
    (losses ? " · 丢失 " + losses + " 条事件" : "");
}

function renderSummary(result) {
  for (const [id, key] of Object.entries({
    tokens: "total_tokens",
    tasks: "tasks",
    running: "running_tasks",
    calls: "llm_calls",
    failed: "failed_calls",
    fallback: "fallback_calls",
    retry: "retry_calls",
    provider: "provider_retries",
    request: "request_retries",
    http: "http_retries",
  })) {
    byId("sum-" + id).textContent = num(result[key]);
  }
  byId("model-rows").innerHTML =
    (result.models || [])
      .map(
        (model) =>
          "<tr><td>" +
          esc(model.provider_model || "未识别模型") +
          '<div class="secondary">' +
          esc(model.provider_id) +
          "</div></td><td>" +
          num(model.calls) +
          " / " +
          num(model.running) +
          "</td><td>" +
          num(model.errors) +
          " / " +
          (model.calls
            ? ((100 * model.errors) / model.calls).toFixed(1)
            : "0.0") +
          "%</td><td>" +
          num(
            Number(model.input_other || 0) + Number(model.input_cached || 0),
          ) +
          " / " +
          num(model.output) +
          "</td><td>" +
          seconds(model.avg_ttft) +
          "</td><td>" +
          seconds(model.p50) +
          " / " +
          seconds(model.p95) +
          '</td><td class="retry-counts">框架 ' +
          num(model.retry_calls) +
          " · 适配器 " +
          num(model.provider_retries) +
          "<br>请求 " +
          num(model.request_retries) +
          " · SDK/HTTP " +
          num(model.http_retries) +
          "</td></tr>",
      )
      .join("") ||
    '<tr><td colspan="7" class="empty">当前范围没有模型调用</td></tr>';
}

function renderTasks(result) {
  state.total = result.total;
  const focusedId = document.activeElement?.dataset.taskId;
  byId("rows").innerHTML =
    result.items
      .map(
        (item) =>
          '<tr aria-selected="' +
          (state.selected === item.task_id) +
          '"><td>' +
          '<button class="link-button" data-task-id="' +
          esc(item.task_id) +
          '" aria-label="查看任务 ' +
          esc(item.task_id) +
          '">' +
          esc(when(item.created_at)) +
          '</button><div class="secondary">' +
          esc(item.platform_name) +
          " · " +
          esc(item.sender_name || item.sender_id || "未知用户") +
          '</div><div class="secondary">' +
          esc(item.umo) +
          "</div></td><td>" +
          esc(item.provider_model || item.provider_id || "未识别模型") +
          '<div class="secondary">' +
          esc(item.provider_id) +
          "</div></td><td>" +
          badge(item.status) +
          '</td><td class="clock">' +
          clock(item) +
          "</td><td>" +
          num(item.llm_call_count) +
          " LLM<br>" +
          num(item.tool_call_count) +
          " 工具</td></tr>",
      )
      .join("") ||
    '<tr><td colspan="5" class="empty">当前筛选没有任务</td></tr>';
  for (const button of byId("rows").querySelectorAll("button")) {
    button.addEventListener("click", () => selectTask(button.dataset.taskId));
    if (focusedId === button.dataset.taskId)
      button.focus({ preventScroll: true });
  }
  byId("prev").disabled = state.offset === 0;
  byId("next").disabled = !result.has_more;
  byId("page-info").textContent =
    "第 " +
    (Math.floor(state.offset / state.limit) + 1) +
    " / " +
    Math.max(1, Math.ceil(result.total / state.limit)) +
    " 页 · 共 " +
    num(result.total) +
    " 条";
}

function retryDetails(item) {
  let html = "";
  if (item.is_retry) {
    const kind =
      {
        backoff: "实际退避",
        recovery_gap: "适配器恢复间隔",
        sdk_gap: "SDK/HTTP 调用间隔",
      }[item.wait_kind] || "间隔";
    html +=
      "<label>重试原因</label><pre>" +
      esc(item.retry_reason || "未捕获") +
      "</pre>" +
      '<div class="secondary">' +
      kind +
      " " +
      seconds(item.retry_wait) +
      (item.retry_wait_planned == null
        ? ""
        : " · 计划退避 " + seconds(item.retry_wait_planned)) +
      "</div>";
  }
  if (item.retry_wait_status) {
    html +=
      '<div class="secondary">后续重试等待：' +
      esc(
        { waiting: "等待中", completed: "等待结束", cancelled: "等待已取消" }[
          item.retry_wait_status
        ] || item.retry_wait_status,
      ) +
      " · 计划 " +
      seconds(item.next_retry_wait) +
      " · 实际 " +
      seconds(item.next_retry_wait_actual) +
      "</div>";
  }
  return html;
}

function renderAttempts(attempts, callId) {
  if (!attempts.length)
    return '<p class="muted">没有分层记录（可能是历史数据或未覆盖的 Provider）。</p>';
  const ids = new Set(attempts.map((a) => a.id));
  const children = new Map();
  for (const attempt of attempts) {
    const parent = ids.has(attempt.parent_id) ? attempt.parent_id : callId;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(attempt);
  }
  const seen = new Set();
  function render(parent, depth) {
    return (children.get(parent) || [])
      .map((attempt) => {
        if (seen.has(attempt.id) || depth > 3) return "";
        seen.add(attempt.id);
        const layer =
          {
            provider: "Provider 适配器",
            request: "请求重试器",
            http: "SDK/HTTP",
          }[attempt.layer] || attempt.layer;
        return (
          '<details data-key="attempt-' +
          esc(attempt.id) +
          '"><summary>' +
          badge(attempt.status) +
          " " +
          esc(layer) +
          " · 尝试 #" +
          num(attempt.attempt_number) +
          (attempt.is_retry ? " " + badge("retry", "Retry") : "") +
          (attempt.http_status ? " · HTTP " + num(attempt.http_status) : "") +
          " · " +
          clock(attempt) +
          '</summary><div class="payload">' +
          '<div class="secondary">' +
          esc(attempt.operation) +
          " · " +
          esc(when(attempt.started_at)) +
          "</div>" +
          retryDetails(attempt) +
          (attempt.error
            ? "<label>本次错误</label><pre>" + esc(attempt.error) + "</pre>"
            : "") +
          (attempt.end_inferred
            ? '<p class="muted">结束状态由父任务推断。</p>'
            : "") +
          '<div class="attempt-tree">' +
          render(attempt.id, depth + 1) +
          "</div></div></details>"
        );
      })
      .join("");
  }
  return render(callId, 0);
}

function renderCall(call, attempts) {
  return (
    '<details data-key="llm-' +
    esc(call.id) +
    '"><summary>' +
    badge(call.status) +
    " LLM #" +
    esc(call.sequence) +
    " · " +
    esc(call.provider_model || "未识别模型") +
    (call.is_fallback ? " " + badge("fallback", "Fallback") : "") +
    (call.is_retry
      ? " " + badge("retry", "Retry #" + num(call.attempt_number))
      : "") +
    " · " +
    clock(call) +
    "</summary>" +
    '<div class="payload"><label>请求模型 / Provider</label><div class="secondary">' +
    esc(call.provider_model) +
    " / " +
    esc(call.provider_id) +
    '</div><label>响应模型</label><div class="secondary">' +
    esc(call.response_model || "未返回") +
    "</div><label>用量与首片段耗时</label>" +
    '<div class="secondary">输入 ' +
    num(Number(call.input_other || 0) + Number(call.input_cached || 0)) +
    "（含缓存 " +
    num(call.input_cached) +
    "） · 输出 " +
    num(call.output) +
    " · TTFT " +
    seconds(call.ttft) +
    "</div>" +
    '<div class="secondary">轮次 ' +
    esc(call.round_id || "历史记录未标记") +
    " · 框架尝试 #" +
    num(call.attempt_number || 1) +
    "</div>" +
    retryDetails(call) +
    (call.end_inferred
      ? '<p class="muted">结束状态由父任务推断，不计入耗时分位数。</p>'
      : "") +
    (call.error
      ? "<label>错误</label><pre>" + esc(call.error) + "</pre>"
      : "") +
    '<h4 class="section-title">分层尝试</h4>' +
    renderAttempts(attempts, call.id) +
    "</div></details>"
  );
}

function renderTool(tool) {
  return (
    '<details data-key="tool-' +
    esc(tool.id) +
    '"><summary>' +
    badge(tool.status) +
    " 工具 #" +
    esc(tool.sequence) +
    " · " +
    esc(tool.tool_name) +
    " · " +
    clock(tool) +
    '</summary><div class="payload">' +
    "<label>输入</label><pre>" +
    esc(tool.input_json) +
    "</pre><label>输出（最后一个执行结果）</label><pre>" +
    esc(tool.output_json) +
    "</pre>" +
    (tool.end_inferred ? '<p class="muted">结束状态由父任务推断。</p>' : "") +
    (tool.error
      ? "<label>错误</label><pre>" + esc(tool.error) + "</pre>"
      : "") +
    "</div></details>"
  );
}

function renderDetail(result) {
  const open = new Set(
    [...byId("detail").querySelectorAll("details[open]")].map(
      (node) => node.dataset.key,
    ),
  );
  const focusedKey = document.activeElement?.closest("details")?.dataset.key;
  const scrollTop = byId("task-dialog").scrollTop;
  const task = result.task;
  const calls = result.llm_calls || [];
  const tools = result.tool_calls || [];
  const attempts = result.retry_attempts || [];
  const tokens = calls.reduce(
    (sum, call) =>
      sum +
      Number(call.input_other || 0) +
      Number(call.input_cached || 0) +
      Number(call.output || 0),
    0,
  );
  const timeline = calls
    .map((call) => ({
      at: call.started_at,
      html: renderCall(
        call,
        attempts.filter((a) => a.llm_call_id === call.id),
      ),
    }))
    .concat(
      tools.map((tool) => ({ at: tool.started_at, html: renderTool(tool) })),
    )
    .sort((a, b) => a.at - b.at);
  byId("detail").innerHTML =
    '<div class="detail-summary"><h3>' +
    esc(task.provider_model || task.provider_id || "未识别模型") +
    "</h3>" +
    "<p>任务 " +
    esc(task.task_id) +
    "</p><p>" +
    esc(task.umo) +
    "</p><p>" +
    esc(task.sender_name || task.sender_id) +
    " · " +
    esc(task.platform_name) +
    " · " +
    badge(task.status) +
    '</p><div class="detail-metrics"><div class="mini"><b>' +
    clock(task) +
    '</b><span>任务耗时</span></div><div class="mini"><b>' +
    num(tokens) +
    '</b><span>总 Token</span></div><div class="mini"><b>' +
    calls.length +
    " / " +
    tools.length +
    "</b><span>LLM / 工具</span></div></div></div>" +
    (task.error ? "<pre>" + esc(task.error) + "</pre>" : "") +
    '<h3 class="section-title">调用时间线</h3>' +
    (timeline.map((item) => item.html).join("") ||
      '<div class="empty">暂未记录到调用</div>');
  for (const node of byId("detail").querySelectorAll("details")) {
    node.open = open.has(node.dataset.key);
    if (focusedKey === node.dataset.key)
      node.querySelector("summary").focus({ preventScroll: true });
  }
  byId("task-dialog").scrollTop = scrollTop;
}

async function refreshDetail(id) {
  const generation = ++state.detailGeneration;
  try {
    const result = await bridge.apiGet("tasks/" + encodeURIComponent(id));
    if (generation !== state.detailGeneration || state.selected !== id) return;
    if (!result?.task) throw new Error("任务不存在或已清理");
    renderDetail(result);
    message("detail-error", "");
  } catch (error) {
    if (generation !== state.detailGeneration || state.selected !== id) return;
    message("detail-error", "详情更新失败，保留最近一次数据：" + error.message);
  }
}

async function selectTask(id) {
  state.selected = id;
  byId("detail").innerHTML = '<div class="empty">正在加载…</div>';
  message("detail-error", "");
  for (const button of byId("rows").querySelectorAll("button")) {
    button
      .closest("tr")
      .setAttribute("aria-selected", String(button.dataset.taskId === id));
  }
  if (!byId("task-dialog").open) byId("task-dialog").showModal();
  await refreshDetail(id);
}

function scheduleRefresh() {
  clearTimeout(state.timer);
  if (!state.paused) state.timer = setTimeout(refresh, 5000);
}

async function refresh() {
  clearTimeout(state.timer);
  const generation = ++state.generation;
  const query = filters();
  byId("updated").textContent = "正在更新…";
  try {
    const [tasks, summary, health] = await Promise.all([
      bridge.apiGet("tasks", {
        ...query,
        limit: state.limit,
        offset: state.offset,
      }),
      bridge.apiGet("summary", query),
      bridge.apiGet("health"),
    ]);
    if (generation !== state.generation) return;
    if (tasks.total > 0 && !tasks.items.length && state.offset >= tasks.total) {
      state.offset = Math.floor((tasks.total - 1) / state.limit) * state.limit;
      return refresh();
    }
    renderTasks(tasks);
    renderSummary(summary);
    setHealth(health);
    state.lastSuccess = new Date().toLocaleTimeString();
    byId("updated").textContent = "更新于 " + state.lastSuccess;
    byId("meta").textContent =
      (state.paused ? "自动刷新已暂停" : "每 5 秒刷新") +
      " · 最近成功 " +
      state.lastSuccess;
    message(
      "error",
      health.ok
        ? ""
        : "监控处于异常状态，数据可能不完整。点击“运行自检”查看原因。",
    );
    if (state.selected) await refreshDetail(state.selected);
  } catch (error) {
    if (generation !== state.generation) return;
    setHealth({ status: "degraded" });
    byId("health-text").textContent = "连接异常";
    byId("updated").textContent = "更新失败";
    message(
      "error",
      "刷新失败，保留最近一次数据：" +
        error.message +
        "。最近成功：" +
        (state.lastSuccess || "尚无") +
        "。",
    );
  } finally {
    if (generation === state.generation) scheduleRefresh();
  }
}

async function selfCheck() {
  const button = byId("check");
  button.disabled = true;
  const box = byId("check-result");
  box.hidden = false;
  box.textContent = "自检中…";
  try {
    const result = await bridge.apiGet("self-check");
    box.innerHTML =
      '<h2 class="panel-title">运行自检</h2><p>' +
      (result.ok ? "检查通过" : "存在异常，请检查下列诊断信息") +
      "</p>" +
      '<div class="diagnostic-grid"><span>采集：' +
      (result.enabled ? "开启" : "关闭") +
      "</span><span>探针：" +
      (result.probe?.enabled ? "已安装" : "不可用") +
      "</span><span>存储：" +
      (result.storage?.ok ? "正常" : "异常") +
      "</span><span>回复过滤：" +
      (result.reply_filter ? "开启" : "关闭") +
      "</span><span>重试探针：" +
      (result.retry_coverage?.ok ? "已安装" : "未完整覆盖") +
      "</span></div>" +
      '<p class="muted">框架 / 请求 / HTTP：' +
      ["framework", "request", "http"]
        .map((key) => (result.retry_coverage?.[key] ? "已接入" : "未接入"))
        .join(" / ") +
      "。已挂载适配器：" +
      esc(
        (result.retry_coverage?.provider_classes || []).join("、") ||
          "尚未调用",
      ) +
      "</p>" +
      "<details><summary>诊断明细</summary><pre>" +
      esc(JSON.stringify(result, null, 2)) +
      "</pre></details>";
    setHealth(result);
  } catch (error) {
    box.textContent = "自检失败：" + error.message;
    setHealth({ status: "degraded" });
  } finally {
    button.disabled = false;
  }
}

byId("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.offset = 0;
  refresh();
});
for (const id of ["hours", "status", "model", "platform"]) {
  byId(id).addEventListener("change", () => {
    state.offset = 0;
    refresh();
  });
}
byId("prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  refresh();
});
byId("next").addEventListener("click", () => {
  state.offset += state.limit;
  refresh();
});
byId("pause").addEventListener("click", () => {
  state.paused = !state.paused;
  byId("pause").setAttribute("aria-pressed", String(state.paused));
  byId("pause").textContent = state.paused ? "恢复自动刷新" : "暂停自动刷新";
  byId("meta").textContent =
    (state.paused ? "自动刷新已暂停" : "每 5 秒刷新") +
    " · 最近成功 " +
    (state.lastSuccess || "尚无");
  clearTimeout(state.timer);
  if (!state.paused) refresh();
});
byId("check").addEventListener("click", selfCheck);
byId("close-detail").addEventListener("click", () =>
  byId("task-dialog").close(),
);
byId("task-dialog").addEventListener("close", () => {
  const selected = state.selected;
  state.selected = null;
  ++state.detailGeneration;
  for (const button of byId("rows").querySelectorAll("button")) {
    button.closest("tr").setAttribute("aria-selected", "false");
    if (button.dataset.taskId === selected)
      button.focus({ preventScroll: true });
  }
});
setInterval(() => {
  for (const node of document.querySelectorAll("[data-clock-start]")) {
    node.textContent = seconds(
      Math.max(0, Date.now() / 1000 - Number(node.dataset.clockStart)),
    );
  }
}, 1000);

(async function init() {
  try {
    if (!bridge) throw new Error("请从 AstrBot 插件页面打开此页面");
    const context = await bridge.ready();
    document.documentElement.dataset.theme = context.isDark ? "dark" : "light";
    await refresh();
  } catch (error) {
    setHealth({ status: "degraded" });
    message("error", "初始化失败：" + error.message);
  }
})();
