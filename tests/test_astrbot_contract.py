"""Execute selected unmodified AstrBot methods without installing the whole host.

Set ASTRBOT_SOURCE_PATH to an AstrBot 4.28.0 checkout. CI checks out that tag.
Only imports and surrounding framework services are replaced by test doubles.
"""

import ast
import os
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

from support import probe_module

SOURCE = os.environ.get("ASTRBOT_SOURCE_PATH")


def method_source(relative, class_name, method_name):
    tree = ast.parse((Path(SOURCE) / relative).read_text())
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name
    )


def build_method(node, namespace):
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            deepcopy(node),
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "<AstrBot source method>", "exec"), namespace)
    return namespace[node.name]


@unittest.skipUnless(SOURCE, "Set ASTRBOT_SOURCE_PATH to enable real source contracts")
class AstrBotContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_probe_targets(self):
        runner_path = "astrbot/core/agent/runners/tool_loop_agent_runner.py"
        llm = method_source(runner_path, "ToolLoopAgentRunner", "_iter_llm_responses")
        step = method_source(runner_path, "ToolLoopAgentRunner", "step")
        tool = method_source(
            "astrbot/core/astr_agent_tool_exec.py", "FunctionToolExecutor", "execute"
        )
        self.assertIn("include_model", [arg.arg for arg in llm.args.kwonlyargs])
        self.assertEqual([arg.arg for arg in tool.args.args], ["cls", "tool", "run_context"])
        self.assertEqual(tool.args.kwarg.arg, "tool_args")
        self.assertTrue(
            any(
                isinstance(node, ast.Name) and node.id == "classmethod"
                for node in tool.decorator_list
            )
        )
        for node in (llm, step, tool):
            self.assertTrue(any(isinstance(child, ast.Yield) for child in ast.walk(node)))

    async def test_actual_llm_payload_model_and_stream_closure(self):
        node = method_source(
            "astrbot/core/agent/runners/tool_loop_agent_runner.py",
            "ToolLoopAgentRunner",
            "_iter_llm_responses",
        )
        original = build_method(node, {})
        calls = []
        final = NS(is_chunk=False, role="assistant", completion_text="ok")

        class Provider:
            async def text_chat(self, **payload):
                calls.append(payload)
                return final

            async def text_chat_stream(self, **payload):
                calls.append(payload)
                try:
                    yield NS(is_chunk=True, completion_text="first")
                    yield final
                finally:
                    self.closed = True

        class Runner:
            _iter_llm_responses = original

            def _sanitize_contexts_for_provider(self, messages):
                return messages

            def _func_tool_for_provider(self):
                return None

            async def _await_or_stop(self, result):
                return await result

            async def _close_executor(self, stream):
                await stream.aclose()

            async def step(self):
                yield None

        class Executor:
            @classmethod
            async def execute(cls, tool, run_context, **tool_args):
                yield None

        probe = probe_module.RunnerProbe(
            NS(
                start_llm=lambda *args: None,
                finish_span=lambda *args, **kwargs: None,
                record_failure=lambda exc: self.fail(str(exc)),
            )
        )
        self.assertTrue(probe.install(Runner, Executor))
        try:
            runner = Runner()
            runner.provider = Provider()
            runner.req = NS(
                model="primary-override", session_id="session", extra_user_content_parts=[]
            )
            runner.run_context = NS(messages=[])
            runner._abort_signal = None
            runner.request_max_retries = 0
            runner.streaming = False
            result = [item async for item in runner._iter_llm_responses(include_model=False)]
            self.assertIs(result[0], final)
            self.assertNotIn("model", calls[-1])
            [item async for item in runner._iter_llm_responses()]
            self.assertEqual(calls[-1]["model"], "primary-override")
            runner.streaming = True
            stream = runner._iter_llm_responses()
            await anext(stream)
            await stream.aclose()
            self.assertTrue(runner.provider.closed)
        finally:
            probe.restore()

    async def test_actual_executor_forwards_local_and_mcp_failures(self):
        node = method_source(
            "astrbot/core/astr_agent_tool_exec.py", "FunctionToolExecutor", "execute"
        )

        class MCPTool:
            name = "mcp"

        class HandoffTool:
            pass

        descriptor = build_method(node, {"MCPTool": MCPTool, "HandoffTool": HandoffTool})
        calls = []

        class Executor:
            execute = descriptor

            @classmethod
            async def _execute_local(cls, tool, run_context, **tool_args):
                calls.append((tool.name, tool_args))
                raise ValueError("local failure")
                yield None

            @classmethod
            async def _execute_mcp(cls, tool, run_context, **tool_args):
                yield {"isError": True, "content": "mcp failure"}

        class Runner:
            async def _iter_llm_responses(self, *, include_model=True):
                yield None

            async def step(self):
                yield None

        ends = []
        plugin = NS(
            start_tool=lambda *args: {"clock": 0},
            finish_span=lambda *args, **kwargs: ends.append((args, kwargs)),
            record_failure=lambda exc: self.fail(str(exc)),
        )
        probe = probe_module.RunnerProbe(plugin)
        self.assertTrue(probe.install(Runner, Executor))
        try:
            with self.assertRaises(ValueError):
                [
                    item
                    async for item in Executor.execute(
                        NS(name="local", is_background_task=False), None, query="q"
                    )
                ]
            result = [item async for item in Executor.execute(MCPTool(), None)]
            self.assertTrue(result[0]["isError"])
            self.assertEqual(calls, [("local", {"query": "q"})])
            self.assertEqual([args[2] for args, kwargs in ends], ["error", "error"])
        finally:
            probe.restore()
