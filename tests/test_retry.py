"""Exercise real AstrBot retry source against deterministic providers and HTTP transports."""

import ast
import asyncio
import logging
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from support import Context, Event, main, store_module
from test_astrbot_contract import build_method, method_source

try:
    import httpx
    import tenacity
except ImportError:
    httpx = tenacity = None
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

SOURCE = os.environ.get("ASTRBOT_SOURCE_PATH")


class EmptyModelOutputError(Exception):
    pass


class Response:
    def __init__(self, role="assistant", completion_text="ok", is_chunk=False, **kwargs):
        self.role, self.completion_text, self.is_chunk = role, completion_text, is_chunk
        self.usage = NS(input_other=5, input_cached=0, output=2)
        self.__dict__.update(kwargs)


class RetryableError(Exception):
    status_code = 503


def request_source():
    path = Path(SOURCE) / "astrbot/core/provider/sources/request_retry.py"
    tree = ast.parse(path.read_text())
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module.startswith("astrbot"))
    ]
    module = types.ModuleType("retry_source_contract")
    module.logger = logging.getLogger("retry-source")
    module.coerce_int_config = lambda value, **kwargs: max(1, int(value))
    module.is_connection_error = lambda error: isinstance(error, ConnectionError)
    exec(compile(tree, str(path), "exec"), module.__dict__)
    module.REQUEST_RETRY_WAIT_MIN_S = 0
    module.REQUEST_RETRY_WAIT_MAX_S = 0
    return module


@unittest.skipUnless(
    SOURCE and tenacity and httpx, "Requires AstrBot source, tenacity and httpx dev dependencies"
)
class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="retry-tests-")
        self.plugin = main.LLMMonitorPlugin(Context(), {"monitor": {"enabled": True}})
        self.plugin.store = store_module.EventStore(Path(self.temp.name) / "events.sqlite3")
        await self.plugin.store.start()
        self.plugin._started = True
        self.event = Event()
        self.request_module = request_source()
        module = types.ModuleType("runner_source_contract")
        module.__dict__.update(
            AsyncRetrying=tenacity.AsyncRetrying,
            retry_if_exception_type=tenacity.retry_if_exception_type,
            stop_after_attempt=tenacity.stop_after_attempt,
            wait_exponential=tenacity.wait_exponential,
            EmptyModelOutputError=EmptyModelOutputError,
            LLMResponse=Response,
            logger=logging.getLogger("runner-source"),
        )
        methods = {
            name: build_method(
                method_source(
                    "astrbot/core/agent/runners/tool_loop_agent_runner.py",
                    "ToolLoopAgentRunner",
                    name,
                ),
                module.__dict__,
            )
            for name in ("_iter_llm_responses", "_iter_llm_responses_with_fallback")
        }

        class Runner:
            _iter_llm_responses = methods["_iter_llm_responses"]
            _iter_llm_responses_with_fallback = methods["_iter_llm_responses_with_fallback"]
            EMPTY_OUTPUT_RETRY_ATTEMPTS = 3
            EMPTY_OUTPUT_RETRY_WAIT_MIN_S = 0
            EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 0
            _abort_signal = None
            request_max_retries = 3
            streaming = False

            def _is_stop_requested(self):
                return False

            def _sanitize_contexts_for_provider(self, messages):
                return messages

            def _func_tool_for_provider(self):
                return None

            def _sanitize_malformed_tool_calls(self, response):
                pass

            async def _await_or_stop(self, operation):
                return await operation

            async def _close_executor(self, iterator):
                await iterator.aclose()

            async def step(self):
                yield None

        class Executor:
            @classmethod
            async def execute(cls, tool, run_context, **tool_args):
                yield None

        self.Runner, self.module = Runner, module
        self.assertTrue(self.plugin.probe.install(Runner, Executor))
        self.assertTrue(
            self.plugin.probe.retry.install(Runner, module, self.request_module, httpx.AsyncClient)
        )
        owner = self

        class Provider:
            __module__ = "astrbot.core.provider.sources.openai_source"

            def __init__(self, name, outcomes):
                self.provider_config = {"id": name}
                self.outcomes = list(outcomes)
                self.seen = []

            def get_model(self):
                return self.provider_config["id"] + "-model"

            async def _query(self, payloads=None, tools=None, *, request_max_retries=None):
                async def operation():
                    item = self.outcomes.pop(0)
                    self.seen.append(item)
                    if isinstance(item, BaseException):
                        raise item
                    if callable(item):
                        return await item()
                    return item

                result = await owner.request_module.retry_provider_request(
                    "test", operation, max_attempts=request_max_retries
                )
                return result if isinstance(result, Response) else Response(raw_completion=result)

            async def _query_stream(self, payloads=None, tools=None, *, request_max_retries=None):
                result = await self._query(payloads, tools, request_max_retries=request_max_retries)
                yield Response(completion_text="first", is_chunk=True)
                yield result

            async def text_chat(self, **kwargs):
                return await self._query(
                    {}, None, request_max_retries=kwargs.get("request_max_retries")
                )

            async def text_chat_stream(self, **kwargs):
                async for response in self._query_stream(
                    {}, None, request_max_retries=kwargs.get("request_max_retries")
                ):
                    yield response

        self.Provider = Provider
        self.runner = self.new_runner(self.event, Provider("primary", [Response()]))

    def new_runner(self, event, provider, fallback=None):
        runner = self.Runner()
        runner.provider = provider
        runner.fallback_providers = fallback or []
        runner.req = NS(model="override", session_id="session", extra_user_content_parts=[])
        runner.run_context = NS(
            context=NS(event=event), messages=[{"role": "user", "content": "test"}]
        )
        return runner

    async def run_round(self, runner=None):
        return [
            response
            async for response in (runner or self.runner)._iter_llm_responses_with_fallback()
        ]

    async def data(self, event=None):
        await self.plugin.store.flush()
        return await self.plugin.store.get_task((event or self.event).get_extra(main.TASK_EXTRA))

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_empty_retry_and_normal_next_round(self):
        self.runner.provider.outcomes = [
            EmptyModelOutputError("empty token=secret-value"),
            Response(),
            Response(),
        ]
        await self.run_round()
        await self.run_round()
        calls = (await self.data())["llm_calls"]
        self.assertEqual([c["attempt_kind"] for c in calls], ["round", "retry", "round"])
        self.assertEqual([c["attempt_number"] for c in calls], [1, 2, 1])
        self.assertEqual(calls[0]["round_id"], calls[1]["round_id"])
        self.assertNotEqual(calls[1]["round_id"], calls[2]["round_id"])
        self.assertIn("EmptyModelOutputError", calls[1]["retry_reason"])
        self.assertNotIn("secret-value", calls[1]["retry_reason"])
        self.assertEqual(calls[0]["retry_wait_status"], "completed")
        self.assertEqual((await self.plugin.store.summary())["retry_calls"], 1)

    async def test_fallback_retry_keeps_both_dimensions(self):
        self.runner.provider.outcomes = [ValueError("primary failed")]
        fallback = self.Provider("backup", [EmptyModelOutputError("empty"), Response(), Response()])
        self.runner.fallback_providers = [fallback]
        await self.run_round()
        self.runner.fallback_providers = []
        await self.run_round()
        calls = (await self.data())["llm_calls"]
        self.assertEqual(
            [c["attempt_kind"] for c in calls], ["round", "fallback", "retry", "round"]
        )
        self.assertEqual([c["is_fallback"] for c in calls], [0, 1, 1, 0])
        self.assertEqual([c["attempt_number"] for c in calls], [1, 1, 2, 1])
        self.assertEqual(calls[2]["provider_model"], "backup-model")

    async def test_request_retries_do_not_duplicate_llm_usage(self):
        self.runner.provider.outcomes = [RetryableError("503"), RetryableError("503"), Response()]
        await self.run_round()
        record = await self.data()
        requests = [a for a in record["retry_attempts"] if a["layer"] == "request"]
        self.assertEqual([a["attempt_number"] for a in requests], [1, 2, 3])
        self.assertEqual([a["status"] for a in requests], ["error", "error", "completed"])
        self.assertEqual(len({a["retry_group_id"] for a in requests}), 1)
        self.assertEqual(len({a["parent_id"] for a in requests}), 1)
        self.assertEqual(len(record["llm_calls"]), 1)
        summary = await self.plugin.store.summary()
        self.assertEqual(
            (summary["retry_calls"], summary["request_retries"], summary["total_tokens"]), (0, 2, 7)
        )
        self.assertEqual(summary["models"][0]["request_retries"], 2)

    async def test_provider_recovery_uses_actual_openai_loop(self):
        provider = self.runner.provider
        namespace = dict(
            random=NS(choice=lambda keys: keys[0]),
            logger=logging.getLogger("provider-source"),
            is_connection_error=lambda error: False,
        )
        for name in ("text_chat", "_handle_api_error"):
            method = build_method(
                method_source(
                    "astrbot/core/provider/sources/openai_source.py", "ProviderOpenAIOfficial", name
                ),
                namespace,
            )
            setattr(self.Provider, name, method)

        async def payload(*args, **kwargs):
            return {"messages": []}, []

        async def pop(messages):
            return None

        provider._prepare_chat_payload, provider.pop_record = payload, pop
        provider.api_keys, provider.client = ["dummy-key-not-recorded"], NS()
        provider.outcomes = [ValueError("maximum context length"), Response()]
        await self.run_round()
        attempts = (await self.data())["retry_attempts"]
        adapters = [a for a in attempts if a["layer"] == "provider"]
        self.assertEqual([a["attempt_number"] for a in adapters], [1, 2])
        self.assertIn("maximum context length", adapters[1]["retry_reason"])
        self.assertEqual(adapters[1]["wait_kind"], "recovery_gap")
        self.assertNotIn("dummy-key-not-recorded", str(attempts))
        summary = await self.plugin.store.summary()
        self.assertEqual((summary["provider_retries"], summary["request_retries"]), (1, 0))

    async def test_http_attempts_and_sdk_retry_headers(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                503 if req.headers.get("x-stainless-retry-count") == "0" else 200
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:

            async def sdk_operation():
                for count in (0, 1):
                    response = await client.send(
                        client.build_request(
                            "POST",
                            "https://fake.test/v1/chat?token=never-store",
                            headers={
                                "x-stainless-retry-count": str(count),
                                "Authorization": "Bearer never-store",
                            },
                        )
                    )
                    if response.status_code == 200:
                        return Response()
                raise AssertionError("missing success")

            self.runner.provider.outcomes = [sdk_operation]
            await self.run_round()
        record = await self.data()
        http = [a for a in record["retry_attempts"] if a["layer"] == "http"]
        self.assertEqual([a["attempt_number"] for a in http], [1, 2])
        self.assertEqual([a["http_status"] for a in http], [503, 200])
        self.assertEqual([a["is_retry"] for a in http], [0, 1])
        self.assertNotIn("never-store", str(record))
        self.assertEqual((await self.plugin.store.summary())["http_retries"], 1)

    async def test_cancel_during_backoff_does_not_count_unstarted_retry(self):
        self.runner.EMPTY_OUTPUT_RETRY_WAIT_MIN_S = 10
        self.runner.EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 10
        self.runner.provider.outcomes = [EmptyModelOutputError("empty"), Response()]
        task = asyncio.create_task(self.run_round())
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                record = await self.data()
                if (
                    record
                    and record["llm_calls"]
                    and record["llm_calls"][0]["retry_wait_status"] == "waiting"
                ):
                    break
            else:
                self.fail("retry never reached backoff")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            record = await self.data()
            self.assertEqual(len(record["llm_calls"]), 1)
            self.assertEqual(record["llm_calls"][0]["retry_wait_status"], "cancelled")
            self.assertGreater(record["llm_calls"][0]["next_retry_wait_actual"], 0)
            self.assertEqual((await self.plugin.store.summary())["retry_calls"], 0)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @unittest.skipUnless(AsyncOpenAI, "Requires OpenAI SDK dev dependency")
    async def test_real_openai_sdk_retries_remain_inside_one_request(self):
        received = []

        def handler(request):
            received.append(request)
            if len(received) == 1:
                return httpx.Response(
                    503, json={"error": {"message": "busy"}}, headers={"retry-after-ms": "1"}
                )
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"},
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with AsyncOpenAI(
                api_key="sdk-secret-never-store", base_url="https://fake.test/v1", http_client=http
            ) as client:

                async def operation():
                    response = await client.chat.completions.create(
                        model="test", messages=[{"role": "user", "content": "hello"}]
                    )
                    return Response(completion_text=response.choices[0].message.content)

                self.runner.provider.outcomes = [operation]
                self.assertEqual((await self.run_round())[0].completion_text, "ok")
        record = await self.data()
        attempts = record["retry_attempts"]
        self.assertEqual(len(received), 2)
        self.assertEqual([a["http_status"] for a in attempts if a["layer"] == "http"], [503, 200])
        self.assertEqual(len([a for a in attempts if a["layer"] == "request"]), 1)
        summary = await self.plugin.store.summary()
        self.assertEqual(
            (
                summary["http_retries"],
                summary["request_retries"],
                summary["provider_retries"],
                summary["total_tokens"],
            ),
            (1, 0, 0, 7),
        )
        self.assertNotIn("sdk-secret-never-store", str(record))

    async def test_exhausted_request_preserves_terminal_exception_and_count(self):
        final = RetryableError("last failure")
        self.runner.provider.outcomes = [RetryableError("503"), RetryableError("503"), final]
        response = (await self.run_round())[-1]
        self.assertEqual(response.role, "err")
        self.assertIn("RetryableError: last failure", response.completion_text)
        attempts = [a for a in (await self.data())["retry_attempts"] if a["layer"] == "request"]
        self.assertEqual(len(attempts), 3)
        self.assertEqual([a["status"] for a in attempts], ["error"] * 3)
        self.assertIsNone(attempts[-1]["next_retry_wait"])
        self.assertEqual((await self.plugin.store.summary())["request_retries"], 2)

    async def test_exhausted_framework_preserves_exception_and_count(self):
        final = EmptyModelOutputError("last empty")
        self.runner.provider.outcomes = [
            EmptyModelOutputError("empty"),
            EmptyModelOutputError("empty"),
            final,
        ]
        response = (await self.run_round())[-1]
        self.assertEqual(response.role, "err")
        self.assertIn("EmptyModelOutputError: last empty", response.completion_text)
        calls = (await self.data())["llm_calls"]
        self.assertEqual([c["attempt_number"] for c in calls], [1, 2, 3])
        self.assertIsNone(calls[-1]["next_retry_wait"])

    async def test_context_manager_retries_only_stream_entry(self):
        owner, entries, exits = self, [], []

        class Manager:
            async def __aenter__(self):
                entries.append(1)
                if len(entries) == 1:
                    raise RetryableError("entry failed")
                return Response()

            async def __aexit__(self, *args):
                exits.append(args)

        async def query(provider, *args, **kwargs):
            async with owner.request_module.retry_provider_request_context(
                "stream", Manager, max_attempts=3
            ) as response:
                yield Response(completion_text="chunk", is_chunk=True)
                yield response

        self.Provider._query_stream = query
        self.runner.streaming = True
        await self.run_round()
        # AstrBot returns immediately after the final response. Its abandoned
        # inner async generators are then finalized by the event loop.
        for _ in range(20):
            if exits:
                break
            await asyncio.sleep(0)
        self.assertEqual((len(entries), len(exits)), (2, 1))
        requests = [a for a in (await self.data())["retry_attempts"] if a["layer"] == "request"]
        self.assertEqual([a["attempt_number"] for a in requests], [1, 2])
        self.assertEqual(self.plugin._record_errors, 0)
        adapters = [a for a in (await self.data())["retry_attempts"] if a["layer"] == "provider"]
        self.assertEqual(adapters[0]["status"], "completed")

    async def test_stream_failure_after_chunk_is_not_retried(self):
        error = ValueError("stream read failed")

        async def query(provider, *args, **kwargs):
            yield Response(completion_text="chunk", is_chunk=True)
            raise error

        self.Provider._query_stream = query
        self.runner.streaming = True
        seen = await self.run_round()
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0].is_chunk)
        self.assertEqual(seen[-1].role, "err")
        self.assertIn("ValueError: stream read failed", seen[-1].completion_text)
        record = await self.data()
        self.assertEqual(len(record["llm_calls"]), 1)
        self.assertEqual(record["llm_calls"][0]["status"], "error")
        self.assertEqual(record["retry_attempts"][0]["status"], "error")

    async def test_restore_keeps_foreign_http_wrapper(self):
        await self.run_round()
        owned = httpx.AsyncClient.send
        original = next(
            row[2] for row in self.plugin.probe.retry._patches if row[0] is httpx.AsyncClient
        )

        async def foreign(*args, **kwargs):
            return await owned(*args, **kwargs)

        httpx.AsyncClient.send = foreign
        try:
            self.assertFalse(self.plugin.probe.retry.snapshot()["ok"])
            self.plugin.probe.restore()
            self.assertIs(httpx.AsyncClient.send, foreign)
            self.assertFalse(getattr(self.Provider._query, "_astrbot_retry_monitor_wrapper", False))
        finally:
            httpx.AsyncClient.send = original

    async def test_concurrent_tasks_keep_attempts_separate(self):
        other_event = Event()
        other = self.new_runner(
            other_event, self.Provider("other", [RetryableError("503"), Response()])
        )
        self.runner.provider.outcomes = [EmptyModelOutputError("empty"), Response()]
        await asyncio.gather(self.run_round(), self.run_round(other))
        first, second = await self.data(), await self.data(other_event)
        self.assertEqual(len(first["llm_calls"]), 2)
        self.assertEqual(len(second["llm_calls"]), 1)
        for record in (first, second):
            ids = {c["id"] for c in record["llm_calls"]}
            self.assertTrue(
                all(
                    a["llm_call_id"] in ids and a["task_id"] == record["task"]["task_id"]
                    for a in record["retry_attempts"]
                )
            )

    async def test_generator_cross_task_advances_do_not_leak_context(self):
        from monitor_test_plugin.retry_probe import CALL, PROVIDER, REQUEST, ROUND

        self.runner.streaming = True
        iterator = self.runner._iter_llm_responses_with_fallback()
        try:
            await asyncio.create_task(anext(iterator))
            self.assertTrue(all(scope.get() is None for scope in (CALL, PROVIDER, REQUEST, ROUND)))
            await asyncio.create_task(anext(iterator))
            self.assertTrue(all(scope.get() is None for scope in (CALL, PROVIDER, REQUEST, ROUND)))
        finally:
            await asyncio.create_task(iterator.aclose())
        record = await self.data()
        self.assertTrue(record["retry_attempts"])
        self.assertEqual(self.plugin._record_errors, 0)

    async def test_disabled_collection_and_unrelated_requests(self):
        self.plugin.config["monitor"]["enabled"] = False
        await self.run_round()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200))
        ) as client:
            await client.get("https://fake.test/")
        await self.plugin.store.flush()
        self.assertEqual((await self.plugin.store.list_tasks())["total"], 0)

    async def test_parent_recovery_closes_retry_attempts(self):
        self.runner.provider.outcomes = [Response()]
        await self.run_round()
        record = await self.data()
        parent = record["llm_calls"][0]
        self.plugin.store.enqueue(
            "attempt_start",
            dict(
                id="orphan",
                task_id=parent["task_id"],
                llm_call_id=parent["id"],
                layer="http",
                sequence=100,
                started_at=time.time(),
            ),
        )
        self.plugin.store.enqueue_recovered(parent["task_id"], "test recovery")
        updated = await self.data()
        orphan = next(a for a in updated["retry_attempts"] if a["id"] == "orphan")
        self.assertEqual((orphan["status"], orphan["end_inferred"]), ("recovered", 1))
