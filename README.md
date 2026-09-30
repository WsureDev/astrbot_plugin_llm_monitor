# AstrBot LLM Monitor

AstrBot 插件：记录 Agent 任务、每轮 LLM 请求、模型、耗时、用量和工具调用，并在 Plugin Page 中查看。

当前版本针对 AstrBot 4.28.x。插件只在运行时做可恢复的内存插桩，不修改 AstrBot 源码；探针不兼容时会自动停用并通过 `/self-check` 暴露原因。

## 本地验证

```bash
python -m compileall -q .
```

安装后在 AstrBot WebUI 的插件详情页打开 `monitor` 页面，使用 self-check 查看探针状态。

页面会显示运行中任务的动态耗时、失败尝试和可观测的 fallback Provider。Provider 适配器内部的 HTTP 重试属于 Provider 内部实现，当前按一次逻辑 Provider 调用统计。

插件只在存在 `running` 任务时启动运行状态校正器，校正周期为 60 秒；找不到 AstrBot 活跃事件或 Agent runner 的历史任务会标记为 `recovered`，不会伪造为成功完成。

## 过滤思考标签

在插件配置的「监控设置」中，通过「过滤回复中的 thinking/think 块」
（`monitor.strip_thinking_blocks`）控制，默认开启。关闭后回复文本保持原样。
这个开关独立于「启用监控采集」，停止采集不会停止过滤。

过滤使用 `on_decorating_result` 钩子，在 AstrBot 内置分段回复之前，
删除文本消息段内完整的 `<thinking>…</thinking>` 和 `<think>…</think>` 块。
支持空块、多行内容、大小写和多个块；图片、语音、文件、At 等非文本组件保持不变。
该功能只清理发送内容，不修改模型请求、思考参数、会话历史或监控记录。

例如 `<thinking>先分析。再检查！</thinking>最终答案。` 会变为 `最终答案。`。
过滤同样会删除正常正文或代码示例中使用这两类完整标签的内容。

适用范围：**非流式回复，完整标签位于同一文本消息段**。流式输出会绕过
AstrBot 的这个发送前阶段；跨消息段的标签、未闭合标签以及通过 `event.send()`
直接发送并绕过结果处理流水线的消息不在此功能的处理范围内。
使用此功能时请关闭 AstrBot 的流式输出。保存配置并按 WebUI 提示重载插件即可生效。
