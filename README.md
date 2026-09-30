# AstrBot LLM Monitor

记录 Agent 任务、每次逻辑 LLM 请求、模型、耗时、Token 用量和工具执行，并在插件的 monitor 页面查看。

v0.4.0 的实现与测试以 **AstrBot v4.28.0 源码**为依据。运行时探针检查目标方法的签名及 async generator / classmethod 形态；不兼容时停止安装对应探针，在页面自检中显示原因。其他 AstrBot 版本仍需验证。

## 使用与升级

1. 在 AstrBot 安装或更新本插件，保存配置并按 WebUI 提示重载。
2. 在插件详情页打开 monitor 页面。页面 API 复用 AstrBot 的登录鉴权和 Plugin Page Bridge；资源由宿主处理认证参数和主题。
3. 点击「运行自检」检查采集、探针、存储、运行状态恢复及回复过滤。

数据库仍位于 StarTools.get_data_dir("astrbot_plugin_llm_monitor") 下的 events.sqlite3。升级在原有表上增加字段和索引，保留历史记录。更新前可按日常运维流程备份该文件。当前数据库 schema 为 4，拒绝打开更高版本的 schema。

插件没有新增运行时第三方依赖；Node、Playwright、Prettier、Ruff 仅用于开发与测试。

## 配置

| 配置项 | 默认值 | 行为 |
| --- | --- | --- |
| monitor.enabled | true | 控制任务、LLM、工具及各层重试的新记录采集。关闭后仍可查询历史记录；已采集的在途调用会写入结束事件。 |
| monitor.strip_thinking_blocks | true | 独立控制非流式回复中的完整 thinking / think 块过滤。 |
| monitor.max_text_chars | 20000 | 每条工具输入、输出与错误文本的保存长度上限，限制在 1000–100000；0 使用默认值。截断标记可能额外占用少量字符。 |
| monitor.retention_days | 30 | 保留 1–365 天；启动及运行期间每小时清理。运行中任务及其子记录暂不删除。 |
| monitor.redact_secrets | true | 对常见凭据字段、Bearer 文本及 MCP 文本内的嵌套 JSON 脱敏。 |

存储配置在插件重载时应用。采集与回复过滤开关在每次事件处理中读取；配置保存后的重载规则由 AstrBot 管理。

### 过滤思考标签

on_decorating_result 在 AstrBot 的非流式分段回复之前，删除同一文本消息段内完整的 &lt;thinking&gt;…&lt;/thinking&gt;、&lt;think&gt;…&lt;/think&gt; 块。支持空块、多行、大小写及多个块；其他消息组件保持原样。

例如「&lt;thinking&gt;先分析。再检查！&lt;/thinking&gt;最终答案。」会变为「最终答案。」。

该功能不修改模型请求、思考参数或会话历史。正文或代码示例中的这两类完整标签也会被删除。**流式输出、跨消息段标签、未闭合标签，以及通过 event.send() 绕过结果处理流水线的发送不在过滤范围内。**需要这一过滤功能时，请关闭 AstrBot 的流式输出。

## 统计口径

- **筛选范围**：时间按任务创建时间计算，所有时间均在浏览器本地时区展示。列表、汇总和模型统计共享时间、状态、模型、渠道筛选。模型是包含匹配，渠道是精确匹配。
- **模型筛选**：任务的初始模型或任一调用的请求/响应模型匹配即可入选；该任务的全部调用均纳入统计，以便保留完整 fallback 路径。
- **LLM 调用**：一次 ToolLoopAgentRunner._iter_llm_responses() 执行。普通 Agent 下一轮使用新的 round_id，尝试序号重新从 1 开始；同一轮的空响应重试使用相同 round_id 和 retry_group_id。绕过该 runner 的直接 Provider 调用不在覆盖范围内。
- **Retry 与 Fallback**：重试独立保存 is_retry、attempt_number、retry_reason、等待时间与父子关系。同一个 fallback Provider 的再次重试可以同时标记 Retry 和 Fallback。历史数据保留原值，不根据相邻调用反推重试；历史重试计数不完整。
- **请求模型**：主尝试使用请求中的 model override；fallback 按 AstrBot 的 include_model=False 语义使用当前 Provider 的模型。响应中返回的模型另存为 response_model。
- **Fallback 调用**：本轮候选 Provider 列表中非首位的尝试次数，不等于 Provider 切换次数。下一轮以已选 Provider 作为首位时按普通调用统计。
- **TTFT**：从调用开始到首个包含文本、reasoning 或工具名称的流式片段的时间。空协议片段不计；非流式、没有有效片段的调用为 null，界面显示 —。v0.1.x 历史记录的 TTFT 按原值保留。
- **耗时**：调用使用单调时钟；任务使用已记录的开始/结束时间，排队时间单独保存。P50/P95 使用 nearest-rank，样本只包含有真实结束记录的 completed/error 调用，不含取消、中断或推断结束的数据。
- **失败率**：error 调用数 / 当前筛选任务的全部模型调用数，包含仍在进行的调用作为分母；取消、中断不计为 error。
- **工具执行**：直接包装 FunctionToolExecutor.execute()；每次执行使用独立 UUID，不依赖 public hook 的 FIFO 配对。isError=true 与抛异常均记为失败；空结果、取消、提前关闭分开记录。UUID 是插件执行 ID，不冒充模型的 tool_call_id。
- **工具输出**：保存执行器的最后一个输出对象。后台工具记录的是「提交执行」过程，不表示后台作业已经完成；不采集后台作业的后续生命周期。
- **运行中恢复**：每 60 秒比对 AstrBot 活跃事件和 runner 回调里的准确 task ID，给予新任务 15 秒宽限。孤立任务及仍运行的子调用标记 recovered；正常父任务先结束但缺少子结束事件时，子调用暂标 interrupted，后续真实结束事件可覆盖。

## 分层 Retry

插件只观察 AstrBot 原有重试过程，不新增重试、不调整次数或退避策略，也不吞掉业务异常。

| 层级 | 观测边界 | 页面计数 |
| --- | --- | --- |
| 框架 | 每轮候选 Provider 的空响应 Tenacity 重试 | 框架重试：该组第 2 次及之后开始的 LLM 调用 |
| Provider 适配器 | 官方适配器 _query / _query_stream；失败后由外层循环恢复上下文、参数或切换 API Key 再调用 | 适配器重试：同次 LLM 调用中失败后的再次适配器尝试 |
| 请求 | AstrBot request_retry._build_retrying 创建的重试器 | 请求层重试：同组第 2 次及之后执行的 request_factory |
| SDK/HTTP | 上述 request_factory 内的 httpx.AsyncClient.send | SDK/HTTP 重试：SDK 重试头标识的后续发送；无该头时，识别同一 SDK 调用内同端点失败后的再次发送 |

四层可能嵌套，**不能相加作为独立请求总数**。Token 仅取外层 LLM 返回用量，内层尝试不重复累计；失败调用未返回的用量无法还原。汇总和模型行分别展示四层计数，任务详情按开始时间平铺显示。

仅在下一次尝试实际开始时增加重试数。在退避等待期间取消，只标记「等待已取消」，不计入尚未开始的重试。框架/请求层同时保存计划退避、实测等待和等待状态；适配器恢复间隔、SDK/HTTP 间隔包含本地处理时间，不冒充精确 sleep 时间。HTTP 耗时为 HTTPX send 的耗时：流式请求到响应头返回，非流式包含响应体读取；流式正文后续失败由外层记录。

适配器边界覆盖 AstrBot 4.28 的 OpenAI、OpenAI Responses、Anthropic、Gemini、SSYCloud 实现，按实际使用延迟挂载。请求层也覆盖流式上下文管理器的进入重试；成功进入后读取流的过程不是该层的重试范围。OpenAI SDK 的自动 HTTP 重试通过真实 SDK + MockTransport 验证。

自定义 Provider、绕过公共 request_retry 的 SDK、自定义非 HTTPX 传输、HTTPX 传输内部的连接重试不保证覆盖。自检展示框架/请求/HTTP 探针状态和已挂载适配器；「已安装」表示观测点有效，不表示任意第三方 Provider 都已被覆盖。插件不采集请求体、请求头或 URL 字段；错误文本仍可能含 SDK 提供的信息，会遵循配置执行限长和脱敏。

## 生图续接与详情

AstrBot 生图插件会先提交后台任务，再用新的 CronMessageEvent 唤醒 Agent 发送结果。插件通过生图插件的 `wake_ai_for_generation_task_result(source_event, task_id)` 绑定原始事件和精确的统一消息来源，复用原始平台、用户和群组；不会用同一会话里最近一条消息猜测调用者。对成功发送的图片，任务显示已完成；Agent 为了停止继续生成而提前关闭 step 生成器不会再覆盖为中断。自检中的「生图续接」显示该观测点是否已安装；未安装生图插件时不影响普通监控。

详情按开始时间平铺展示 LLM 调用、各层 Retry 和工具调用，不再把三层重试嵌套在 LLM 下。LLM 展开显示来自 AstrBot runner 的上下文输入和最终响应；工具展开显示执行器输入和最后输出；Retry 展示层级、序号、原因、状态和等待时间。输入输出遵循 max_text_chars 和脱敏配置，无法由 AstrBot 对象提供的字段显示为「未采集」，避免伪造内容。

列表整行可点击或用键盘 Enter/Space 打开详情，按钮有 hover/active/focus 反馈。模型和渠道筛选从保留数据动态生成，可多选；选项请求独立于任务列表，切换时间范围后自动刷新。

## 可靠性与健康状态

- 插件分为 hooks/API、调用探针、重试探针、持久化、脱敏、回复过滤六个模块，网页的 HTML、CSS、JS 分离。
- 采集只向有界队列投递；SQLite 写入、工具序列化、查询和清理均在线程中执行。写入批次使用事务；单条坏数据隔离，数据库级错误回滚整批。
- 队列满、写入失败和采集异常会进入健康诊断，不再静默吞掉。损失计数在本次插件运行期间持续保留；后续成功写入不会掩盖已丢失的数据。日志限频，避免监控异常刷屏。
- 页面每次刷新读取真实 health；刷新失败保留最后一次成功数据并提示过期。详情随轮询刷新，保留展开状态；请求序号阻止旧响应覆盖新筛选或新选择。
- 任务支持分页；手机端详情全屏显示，桌面端使用对话框。按钮、筛选输入与弹窗支持键盘操作及 Escape 关闭。
- 脱敏是有边界的尽力处理，不能识别所有自由文本秘密；截断后的工具内容可能不再是完整 JSON。不要将监控数据库视为已彻底去敏的数据集。

## 开发验证

测试使用 unittest；要求 Python 3.12+。重试契约测试依赖 Tenacity、HTTPX 与 OpenAI SDK，版本固定在 requirements-dev.txt（仅用于测试；运行时使用 AstrBot 自带依赖）：

~~~bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
~~~

指定 AstrBot 源码后运行契约与方法执行测试；未指定源码或未装测试依赖时，相应测试会明确跳过：

~~~bash
ASTRBOT_SOURCE_PATH=/path/to/AstrBot-4.28.0 python -m unittest discover -s tests -v
~~~

测试涵盖采集开关、独立回复过滤、异常工具与下一次调用配对、isError、取消与生成器关闭、fallback 模型、流式首片段、失败健康状态、旧库升级、定期清理、锁库时事件循环响应，以及仅恢复自己安装的探针。

Retry 测试执行 AstrBot 原始方法，覆盖正常多轮与空响应重试区分、fallback 中重试、请求层耗尽、适配器上下文恢复、真实 OpenAI SDK 自动重试、流式进入重试、流式中途失败、退避取消、跨任务生成器推进及并发隔离。

~~~bash
pip install -r requirements-dev.txt
ruff check main.py probe.py retry_probe.py store.py serialization.py reply_filter.py tests
ruff format --check main.py probe.py retry_probe.py store.py serialization.py reply_filter.py tests
npm ci
npx playwright install chromium
npm run format:check
npm run test:ui
~~~

浏览器测试使用拦截的模拟 API，不访问生产 AstrBot。覆盖分页、筛选竞态、详情刷新与竞态、错误提示、键盘操作、手机布局和内容转义，以及分层计数、扁平重试时间线、等待取消与 Retry/Fallback 同时显示。可设 MONITOR_SCREENSHOT_DIR 保存截图，或用 PLAYWRIGHT_MODULE_PATH 指定现有 Playwright。

GitHub Actions 在 Python 3.12/3.13 下运行后端及 v4.28.0 源码测试，并运行 Chromium 交互测试和格式检查。
