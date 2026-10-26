"""Regression coverage for complete payloads, stream interruption and Provider inputs."""

import asyncio
import base64
import functools
import importlib
import inspect
import json
import unittest
from types import SimpleNamespace as NS

from support import probe_module, serialization

ResponseCapture = importlib.import_module("monitor_test_plugin.payloads").ResponseCapture


class FullSerializationTests(unittest.TestCase):
    def test_large_payloads_keys_items_and_nested_json_are_complete(self):
        text = '中\\"\n' * 300000 + "TAIL"
        long_key = "field" * 100
        deep = "deep-tail"
        for _ in range(64):
            deep = {"child": deep}
        value = {
            "data": text,
            "blob": text,
            long_key: text,
            "items": list(range(1500)),
            "fields": {str(index): index for index in range(1500)},
            "nested": json.dumps({"body": text, "api_key": "secret-value"}),
            "deep": deep,
        }
        stored = json.loads(serialization.safe_json(value))
        self.assertEqual(stored[long_key], text)
        self.assertEqual(stored["data"], text)
        self.assertEqual(stored["blob"], text)
        self.assertEqual(stored["items"], value["items"])
        self.assertEqual(stored["fields"], value["fields"])
        self.assertEqual(stored["deep"], deep)
        self.assertEqual(json.loads(stored["nested"]), {"body": text, "api_key": "[REDACTED]"})
        self.assertEqual(json.loads(serialization.safe_json(value, redact=False)), value)
        self.assertEqual(serialization.redact_text(text), text)

    def test_binary_and_shared_objects_are_preserved_without_false_cycles(self):
        binary = bytes(range(256)) * 1000
        shared = {"text": "shared"}
        stored = json.loads(serialization.safe_json([binary, shared, shared]))
        self.assertEqual(base64.b64decode(stored[0]["data"]), binary)
        self.assertEqual(stored[0]["encoding"], "base64")
        self.assertEqual(stored[1:], [shared, shared])

    def test_partial_stream_retains_all_text_reasoning_and_tool_fragments(self):
        capture = ResponseCapture()
        text = "x" * 1100000 + "tail"
        chunk = {"is_chunk": True, "completion_text": text, "reasoning_content": text}
        capture.feed(chunk)
        chunk["completion_text"] = "changed-after-feed"
        capture.feed({"is_chunk": True, "tools_call_args": [{"query": text}]})
        value = capture.result()
        self.assertEqual(value["completion_text"], text)
        self.assertEqual(value["reasoning_content"], text)
        self.assertEqual(value["chunks"][0]["completion_text"], text)
        self.assertEqual(value["chunks"][1]["tools_call_args"][0]["query"], text)
        self.assertTrue(value["partial"])

    def test_final_response_keeps_raw_fields_and_honors_redaction(self):
        response = NS(
            completion_text="final", raw_completion={"api_key": "secret-value", "extra": "tail"}
        )
        capture = ResponseCapture()
        capture.feed({"is_chunk": True, "completion_text": "partial"})
        capture.feed(response)
        response.raw_completion["extra"] = "mutated"
        self.assertEqual(
            capture.result()["raw_completion"], {"api_key": "[REDACTED]", "extra": "tail"}
        )
        plain = ResponseCapture(redact=False)
        plain.feed(response)
        self.assertEqual(plain.result()["raw_completion"]["api_key"], "secret-value")


class ProviderInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.snapshots = []
        self.errors = []
        self.ends = []
        self.next_id = 0

        def start(runner, include_model):
            self.next_id += 1
            return {"id": self.next_id, "provider_model": "effective-model", "clock": 0}

        self.plugin = NS(
            store=NS(redact_secrets=True),
            start_llm=start,
            capture_llm_input=lambda span, value: self.snapshots.append(
                (span["id"], json.loads(value))
            ),
            finish_span=lambda *args, **kwargs: self.ends.append(kwargs),
            record_failure=self.errors.append,
        )

        class Runner:
            async def _iter_llm_responses(self, *, include_model=True):
                if self.streaming:
                    iterator = self.provider.text_chat_stream(**self.payload)
                    try:
                        async for response in iterator:
                            yield response
                    finally:
                        await iterator.aclose()
                else:
                    yield await self.provider.text_chat(**self.payload)

            async def step(self):
                yield None

        class Executor:
            @classmethod
            async def execute(cls, tool, run_context, **tool_args):
                yield None

        class Provider:
            async def text_chat(self, contexts, func_tool=None, **kwargs):
                self.seen = (contexts, func_tool, kwargs)
                await asyncio.sleep(0)
                contexts.append({"role": "user", "content": "provider mutation"})
                return {"completion_text": "ok"}

            async def text_chat_stream(self, contexts, func_tool=None, **kwargs):
                self.seen = (contexts, func_tool, kwargs)
                try:
                    yield {"is_chunk": True, "completion_text": "first"}
                    yield {"completion_text": "final"}
                finally:
                    self.closed = True

        self.Runner, self.Provider = Runner, Provider
        self.original = Provider.text_chat
        self.probe = probe_module.RunnerProbe(self.plugin)
        self.assertTrue(self.probe.install(Runner, Executor))

    async def asyncTearDown(self):
        self.probe.restore()

    def runner(self, content="user", *, streaming=False, tools=None):
        runner = self.Runner()
        runner.provider = self.Provider()
        runner.streaming = streaming
        runner.payload = {
            "contexts": [{"role": "user", "content": content}],
            "func_tool": tools,
            "extra_user_content_parts": [{"text": "extra"}],
            "abort_signal": object(),
        }
        return runner

    async def test_actual_tools_contexts_and_extras_are_frozen_without_changing_call(self):
        schema = [{"type": "function", "function": {"description": "x" * 1100000 + "tail"}}]
        tools = NS(openai_schema=lambda: schema)
        runner = self.runner(tools=tools)
        responses = [value async for value in runner._iter_llm_responses()]
        self.assertEqual(responses, [{"completion_text": "ok"}])
        value = self.snapshots[0][1]
        self.assertEqual(value["tools"], schema)
        self.assertEqual(value["messages"], [{"role": "user", "content": "user"}])
        self.assertEqual(value["parameters"]["extra_user_content_parts"], [{"text": "extra"}])
        self.assertNotIn("abort_signal", value["parameters"])
        self.assertEqual(value["capture_source"], "provider_call")
        self.assertEqual(value["tool_schema_format"], "openai")
        self.assertIs(runner.provider.seen[1], tools)
        self.assertIs(runner.provider.seen[0], runner.payload["contexts"])
        self.assertFalse(self.errors)

    async def test_two_parallel_calls_do_not_cross_inputs(self):
        first, second = self.runner("first"), self.runner("second")

        async def consume(runner):
            return [response async for response in runner._iter_llm_responses()]

        await asyncio.gather(consume(first), consume(second))
        self.assertEqual(
            {key: value["messages"][0]["content"] for key, value in self.snapshots},
            {1: "first", 2: "second"},
        )
        self.assertFalse(self.errors)

    async def test_stream_close_and_layered_restore(self):
        runner = self.runner(streaming=True)
        iterator = runner._iter_llm_responses()
        await anext(iterator)
        await iterator.aclose()
        self.assertTrue(runner.provider.closed)
        self.assertEqual(self.ends[0]["output_text"]["completion_text"], "first")
        self.assertEqual(len(self.snapshots), 1)
        wrapped = self.Provider.text_chat

        @functools.wraps(wrapped)
        async def outer(*args, **kwargs):
            return await wrapped(*args, **kwargs)

        self.Provider.text_chat = outer
        self.assertTrue(self.probe.inputs.intact())
        self.probe.restore()
        self.assertIs(self.Provider.text_chat, outer)
        await runner.provider.text_chat([])
        self.assertEqual(len(self.snapshots), 1)
        self.assertFalse(self.errors)

    async def test_bad_schema_does_not_interrupt_provider(self):
        def bad_schema():
            raise ValueError("bad tool schema")

        runner = self.runner(tools=NS(openai_schema=bad_schema))
        result = [response async for response in runner._iter_llm_responses()]
        self.assertEqual(result, [{"completion_text": "ok"}])
        self.assertEqual(str(self.errors[0]), "bad tool schema")
        self.probe.restore()
        self.assertIs(inspect.getattr_static(self.Provider, "text_chat"), self.original)

    async def test_reinstall_inside_outer_wrapper_records_once(self):
        first = self.runner("first")
        [response async for response in first._iter_llm_responses()]
        wrapped = self.Provider.text_chat

        @functools.wraps(wrapped)
        async def outer(*args, **kwargs):
            return await wrapped(*args, **kwargs)

        self.Provider.text_chat = outer
        self.probe.inputs.restore()
        second = self.runner("second")
        [response async for response in second._iter_llm_responses()]
        self.assertEqual(len(self.snapshots), 2)
        self.assertEqual(self.snapshots[-1][1]["messages"][0]["content"], "second")
        self.assertTrue(self.probe.inputs.intact())
        self.assertFalse(self.errors)

    async def test_inherited_provider_restores_without_leaving_overrides(self):
        class Derived(self.Provider):
            pass

        runner = self.runner()
        runner.provider = Derived()
        [response async for response in runner._iter_llm_responses()]
        self.assertEqual(len(self.snapshots), 1)
        self.assertIn("text_chat", vars(Derived))
        self.probe.inputs.restore()
        self.assertNotIn("text_chat", vars(Derived))
        self.assertIs(Derived.text_chat, self.original)
        self.assertFalse(self.errors)
