# AstrBot LLM Monitor

AstrBot 插件：记录 Agent 任务、每轮 LLM 请求、模型、耗时、用量和工具调用，并在 Plugin Page 中查看。

当前版本针对 AstrBot 4.28.x。插件只在运行时做可恢复的内存插桩，不修改 AstrBot 源码；探针不兼容时会自动停用并通过 `/self-check` 暴露原因。

## 本地验证

```bash
python -m compileall -q .
```

安装后在 AstrBot WebUI 的插件详情页打开 `monitor` 页面，使用 self-check 查看探针状态。
