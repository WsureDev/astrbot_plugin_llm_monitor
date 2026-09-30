import asyncio
import inspect
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from support import (
    Context,
    Event,
    Plain,
    main,
    probe_module,
    registry,
    reply_filter,
    serialization,
    store_module,
)


class SerializationTests(unittest.TestCase):
    def test_nested_mcp_json_and_fields(self):
        secret = "dummy-secret-not-a-real-credential"
        payload = {
            "content": [
                {
                    "text": json.dumps(
                        {"api_key": secret, "safe": "visible", "nested": {"refresh_token": secret}}
                    )
                }
            ],
            "headers": {"Authorization": "Bearer " + secret, "Cookie": secret},
            "password": secret,
        }
        value = serialization.safe_json(payload)
        self.assertNotIn(secret, value)
        self.assertIn("visible", value)
        self.assertIn(secret, serialization.safe_json(payload, redact=False))

    def test_text_truncation_and_recursion(self):
        self.assertNotIn(
            "secret-value", serialization.redact_text("password=secret-value other=value")
        )
        self.assertNotIn("secret-value", serialization.redact_text("Bearer secret-value"))
        self.assertNotIn(
            "secret-value",
            serialization.safe_json({"text": '{"token": "' + "secret-value" * 1000}, 1000),
        )
        circular = {"x": []}
        circular["x"].append(circular)
        self.assertIn("circular", serialization.safe_json(circular))
        self.assertLessEqual(len(serialization.safe_json({"data": "x" * 100000}, 1000)), 1013)

    def test_filter_scope(self):
        for text in (
            "<thinking></thinking> answer",
            "<THINKING>multi\nline</THINKING>answer",
            "<think class='a'>x</think>answer",
            "<think>x</think><thinking>y</thinking>answer",
        ):
            self.assertEqual(reply_filter.strip_thinking(text), "answer")
        self.assertEqual(reply_filter.strip_thinking("  answer  "), "  answer  ")
        self.assertEqual(reply_filter.strip_thinking("<think>unclosed"), "<think>unclosed")


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="monitor-tests-")
        self.store = store_module.EventStore(Path(self.directory.name) / "events.sqlite3")
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    def task(self, task_id="t", **kwargs):
        self.assertTrue(
            self.store.enqueue(
                "task_start", dict(task_id=task_id, created_at=time.time(), **kwargs)
            )
        )

    def llm(self, call_id="c", task_id="t", **kwargs):
        self.assertTrue(
            self.store.enqueue(
                "llm_start",
                dict(
                    id=call_id,
                    task_id=task_id,
                    sequence=1,
                    started_at=time.time(),
                    provider_id="p",
                    provider_model="model-a",
                    **kwargs,
                ),
            )
        )

    async def test_terminal_parent_and_real_child_finish(self):
        self.task()
        self.llm()
        self.store.enqueue(
            "tool_start",
            dict(
                id="x", task_id="t", sequence=1, started_at=time.time(), tool_name="test", input={}
            ),
        )
        self.store.enqueue(
            "task_end", dict(task_id="t", finished_at=time.time(), status="completed")
        )
        await self.store.flush()
        record = await self.store.get_task("t")
        self.assertEqual(record["llm_calls"][0]["status"], "interrupted")
        self.assertEqual(record["tool_calls"][0]["end_inferred"], 1)
        self.store.enqueue(
            "llm_end",
            dict(
                id="c",
                finished_at=time.time(),
                status="completed",
                duration=2,
                ttft=None,
                usage={"output": 3},
            ),
        )
        await self.store.flush()
        call = (await self.store.get_task("t"))["llm_calls"][0]
        self.assertEqual(
            (call["status"], call["end_inferred"], call["output"]), ("completed", 0, 3)
        )
        self.assertIsNone(call["ttft"])

    async def test_recovery_ends_children(self):
        self.task()
        self.llm()
        self.store.enqueue_recovered("t", "orphan")
        await self.store.flush()
        record = await self.store.get_task("t")
        self.assertEqual(record["task"]["status"], "recovered")
        self.assertEqual(record["llm_calls"][0]["status"], "recovered")

    async def test_bad_event_visible_and_valid_neighbors_survive(self):
        self.task("before")
        self.store.enqueue("llm_start", {"id": "bad"})
        self.task("after")
        await self.store.flush()
        self.assertEqual((await self.store.list_tasks())["total"], 2)
        health = self.store.health()
        self.assertFalse(health["ok"])
        self.assertEqual((health["failed"], health["persisted"]), (1, 2))
        self.assertIn("KeyError", health["last_error"])

    async def test_transaction_failure_rolls_back_whole_batch(self):
        original = self.store._apply_event

        def fail_second(db, kind, payload):
            if payload["task_id"] == "second":
                raise sqlite3.OperationalError("simulated disk failure")
            original(db, kind, payload)

        with patch.object(self.store, "_apply_event", side_effect=fail_second):
            self.task("first")
            self.task("second")
            await self.store.flush()
        self.assertEqual((await self.store.list_tasks())["total"], 0)
        self.assertEqual(self.store.health()["failed"], 2)

    async def test_queue_overflow_is_visible(self):
        self.store.queue._maxsize = 1
        self.task("first")
        self.assertFalse(self.store.enqueue("task_start", {"task_id": "second"}))
        self.assertEqual(self.store.health()["dropped"], 1)
        self.assertFalse(self.store.health()["ok"])
        await self.store.flush()

    async def test_lock_does_not_block_event_loop(self):
        ready = threading.Event()

        def hold_lock():
            with sqlite3.connect(self.store.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                ready.set()
                time.sleep(0.3)
                connection.rollback()

        thread = threading.Thread(target=hold_lock)
        thread.start()
        await asyncio.to_thread(ready.wait)
        self.task()
        start = time.perf_counter()
        await asyncio.sleep(0.02)
        delay = time.perf_counter() - start
        self.assertLess(delay, 0.15, f"heartbeat delayed {delay:.3f}s")
        await self.store.flush()
        await asyncio.to_thread(thread.join)
        self.assertTrue(self.store.health()["ok"])

    async def test_filters_pagination_and_model_metrics(self):
        for index in range(55):
            self.task(str(index), platform_name="channel")
        self.llm(is_fallback=True, task_id="0")
        self.store.enqueue(
            "llm_end",
            dict(
                id="c",
                finished_at=time.time(),
                status="error",
                duration=3,
                ttft=0.2,
                usage={"input_other": 4, "input_cached": 2, "output": 5},
                response_model="resolved-model",
            ),
        )
        await self.store.flush()
        first = await self.store.list_tasks(limit=50)
        second = await self.store.list_tasks(limit=50, offset=50)
        self.assertEqual((len(first["items"]), len(second["items"]), first["total"]), (50, 5, 55))
        filtered = await self.store.list_tasks(model="resolved-model", platform="channel")
        summary = await self.store.summary(model="resolved-model", platform="channel")
        self.assertEqual((filtered["total"], summary["tasks"], summary["total_tokens"]), (1, 1, 11))
        self.assertEqual(summary["models"][0]["p95"], 3)
        self.assertEqual(summary["models"][0]["avg_ttft"], 0.2)
        self.assertEqual((await self.store.list_tasks(model="%"))["total"], 0)

    async def test_periodic_cleanup_preserves_active_trees(self):
        old = time.time() - 40 * 86400
        for task_id in ("active", "ended"):
            self.store.enqueue("task_start", dict(task_id=task_id, created_at=old))
            self.store.enqueue(
                "llm_start", dict(id=task_id, task_id=task_id, sequence=1, started_at=old)
            )
            self.store.enqueue(
                "attempt_start",
                dict(
                    id=task_id,
                    task_id=task_id,
                    llm_call_id=task_id,
                    layer="request",
                    sequence=1,
                    started_at=old,
                ),
            )
        self.store.enqueue("task_end", dict(task_id="ended", finished_at=old, status="completed"))
        await self.store.flush()
        self.store._next_cleanup = time.monotonic() - 1
        self.task("wake-writer")
        await self.store.flush()
        for _ in range(20):
            if await self.store.get_task("ended") is None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(await self.store.get_task("ended"))
        self.assertEqual(len((await self.store.get_task("active"))["llm_calls"]), 1)
        self.assertEqual(len((await self.store.get_task("active"))["retry_attempts"]), 1)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM retry_attempts WHERE task_id='ended'").fetchone()[
                    0
                ],
                0,
            )

    async def test_upgrade_schema_two_adds_retry_fields_without_reclassifying_history(self):
        self.task("existing")
        self.llm(task_id="existing")
        await self.store.flush()
        await self.store.close()
        with sqlite3.connect(self.store.path) as db:
            for column in store_module.RETRY_COLUMNS:
                db.execute(f"ALTER TABLE llm_calls DROP COLUMN {column}")
            db.execute("DROP TABLE retry_attempts")
            db.execute("PRAGMA user_version=2")
        await self.store.start()
        record = await self.store.get_task("existing")
        self.assertEqual(len(record["llm_calls"]), 1)
        self.assertEqual(record["llm_calls"][0]["is_retry"], 0)
        self.assertIsNone(record["llm_calls"][0]["round_id"])
        self.assertEqual(record["retry_attempts"], [])
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    async def test_upgrade_existing_schema_keeps_data(self):
        self.task("existing")
        await self.store.flush()
        await self.store.close()
        with sqlite3.connect(self.store.path) as db:
            for table, column in (
                ("tasks", "end_inferred"),
                ("llm_calls", "end_inferred"),
                ("llm_calls", "response_model"),
                ("tool_calls", "end_inferred"),
            ):
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            db.execute("PRAGMA user_version=0")
        await self.store.start()
        self.assertIsNotNone(await self.store.get_task("existing"))
        self.llm(task_id="existing")
        await self.store.flush()
        self.assertIn("response_model", (await self.store.get_task("existing"))["llm_calls"][0])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="monitor-runtime-")
        self.plugin = main.LLMMonitorPlugin(Context(), {"monitor": {"enabled": True}})
        self.plugin.store = store_module.EventStore(Path(self.directory.name) / "events.sqlite3")
        await self.plugin.store.start()
        self.plugin._started = True
        self.event = Event()
        self.context = NS(context=NS(event=self.event))
        self.outputs = [NS(is_chunk=False, role="assistant", completion_text="ok", usage=None)]
        owner = self

        class Runner:
            async def _iter_llm_responses(self, *, include_model=True):
                for output in owner.outputs:
                    if isinstance(output, BaseException):
                        raise output
                    yield output

            async def step(self):
                yield NS(type="done")

        class Executor:
            @classmethod
            async def execute(cls, tool, run_context, **tool_args):
                if tool.name == "throws":
                    raise ValueError("example error")
                if tool.name == "slow":
                    try:
                        await asyncio.sleep(60)
                    finally:
                        owner.closed = True
                try:
                    yield {"isError": tool.name == "error", "content": tool.name}
                finally:
                    owner.closed = True

        self.Runner, self.Executor = Runner, Executor
        self.original_executor = inspect.getattr_static(Executor, "execute")
        self.assertTrue(self.plugin.probe.install(Runner, Executor))
        self.runner = Runner()
        self.runner.provider = NS(
            provider_config={"id": "fallback"}, get_model=lambda: "fallback-model"
        )
        self.runner.req = NS(model="primary-override")
        self.runner.run_context = self.context

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.directory.cleanup()
        registry._events.clear()
        registry._agent_stop_callbacks.clear()

    async def record(self):
        await self.plugin.store.flush()
        return await self.plugin.store.get_task(self.event.get_extra(main.TASK_EXTRA))

    async def tool(self, name):
        return [result async for result in self.Executor.execute(NS(name=name), self.context, x=1)]

    async def test_disabled_monitor_and_independent_filter(self):
        self.plugin.config["monitor"]["enabled"] = False
        await self.plugin.on_waiting_llm(self.event)
        await self.plugin.on_llm_request(self.event, self.runner.req)
        await self.plugin.on_agent_begin(self.event, None)
        await self.tool("ok")
        [item async for item in self.runner._iter_llm_responses()]
        await self.plugin.on_agent_done(self.event, None, self.outputs[0])
        await self.plugin.store.flush()
        self.assertEqual((await self.plugin.store.list_tasks())["total"], 0)
        image = NS(type="Image")
        self.event.result.chain = [Plain("<thinking>x</thinking>answer"), image]
        await self.plugin.on_decorating_result(self.event)
        self.assertEqual(self.event.result.chain[0].text, "answer")
        self.assertIs(self.event.result.chain[1], image)
        self.assertTrue(self.plugin._health()["ok"])
        self.assertEqual(self.plugin._health()["status"], "disabled")

    async def test_tool_exception_does_not_mispair_next_output(self):
        with self.assertRaises(ValueError):
            await self.tool("throws")
        await self.tool("second")
        await self.tool("error")
        calls = (await self.record())["tool_calls"]
        self.assertEqual([call["status"] for call in calls], ["error", "completed", "error"])
        self.assertNotIn("second", calls[0]["output_json"])
        self.assertIn("second", calls[1]["output_json"])
        self.assertEqual(len({call["id"] for call in calls}), 3)

    async def test_cancel_and_early_close_finalize_generator(self):
        self.closed = False
        task = asyncio.create_task(self.tool("slow"))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.closed)
        self.closed = False
        iterator = self.Executor.execute(NS(name="early"), self.context)
        await anext(iterator)
        await iterator.aclose()
        self.assertTrue(self.closed)
        self.assertEqual(
            [call["status"] for call in (await self.record())["tool_calls"]],
            ["cancelled", "interrupted"],
        )

    async def test_fallback_model_usage_and_nonstream_ttft(self):
        self.outputs[0].usage = NS(input_other=5, input_cached=2, output=7)
        self.outputs[0].raw_completion = {"model": "resolved"}
        result = [item async for item in self.runner._iter_llm_responses(include_model=False)]
        self.assertIs(result[0], self.outputs[0])
        call = (await self.record())["llm_calls"][0]
        self.assertEqual(
            (call["provider_model"], call["response_model"], call["is_fallback"]),
            ("fallback-model", "resolved", 1),
        )
        self.assertIsNone(call["ttft"])
        self.assertEqual(call["output"], 7)

    async def test_stream_error_and_first_content_chunk(self):
        self.outputs = [
            NS(is_chunk=True, completion_text=""),
            NS(is_chunk=True, completion_text="hello"),
            NS(is_chunk=False, role="err", completion_text="token=dummy-secret"),
        ]
        [item async for item in self.runner._iter_llm_responses()]
        call = (await self.record())["llm_calls"][0]
        self.assertEqual(call["status"], "error")
        self.assertIsNotNone(call["ttft"])
        self.assertNotIn("dummy-secret", call["error"])

    async def test_empty_and_stopped_llm_are_not_success(self):
        self.outputs = []
        [item async for item in self.runner._iter_llm_responses()]
        self.runner._is_stop_requested = lambda: True
        [item async for item in self.runner._iter_llm_responses()]
        self.assertEqual(
            [call["status"] for call in (await self.record())["llm_calls"]],
            ["empty", "aborted"],
        )

    async def test_live_health_detects_replaced_probe(self):
        async def replacement(runner):
            yield None

        self.Runner.step = replacement
        health = self.plugin._health()
        self.assertFalse(health["ok"])
        self.assertFalse(health["probe"]["enabled"])
        self.assertIn("replaced", health["probe"]["reason"])

    async def test_storage_start_failure_keeps_reply_filter(self):
        await self.plugin.store.close()
        blocked_parent = Path(self.directory.name) / "file-not-directory"
        blocked_parent.write_text("test")
        self.plugin.store = store_module.EventStore(blocked_parent / "events.sqlite3")
        await self.plugin.initialize()
        self.assertFalse(self.plugin._health()["ok"])
        self.event.result.chain = [Plain("<think>hidden</think>answer")]
        await self.plugin.on_decorating_result(self.event)
        self.assertEqual(self.event.result.chain[0].text, "answer")

    async def test_api_validates_filters_and_reports_missing_task(self):
        with patch.object(
            main.request,
            "query",
            {"hours": "bad", "limit": "-1", "offset": "bad", "status": "not-valid"},
        ):
            response = await self.plugin.tasks_api()
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["limit"], 1)
        missing = await self.plugin.task_api("not-found")
        self.assertEqual(missing["status"], 404)
        await self.plugin.store.close()
        unavailable = await self.plugin.tasks_api()
        self.assertEqual(unavailable["status"], 503)

    async def test_monitor_failure_does_not_change_provider_result(self):
        with patch.object(self.plugin, "start_llm", side_effect=ValueError("collection failed")):
            result = [item async for item in self.runner._iter_llm_responses()]
        self.assertIs(result[0], self.outputs[0])
        self.assertFalse(self.plugin._health()["ok"])

    async def test_disable_mid_call_completes_admitted_span(self):
        iterator = self.runner._iter_llm_responses()
        await anext(iterator)
        self.plugin.config["monitor"]["enabled"] = False
        await iterator.aclose()
        self.plugin.end_task(self.event, "completed")
        record = await self.record()
        self.assertEqual(record["llm_calls"][0]["status"], "completed")
        self.assertEqual(
            self.event.get_extra(main.TASK_STATE_EXTRA),
            {"task_id": record["task"]["task_id"], "ended": True},
        )

    async def test_recovery_matches_exact_task_even_in_callbacks(self):
        other = Event()
        other.set_extra(main.TASK_EXTRA, "other")
        snapshot = ({}, {other: object()})
        task = {"umo": other.unified_msg_origin, "task_id": "orphan"}
        self.assertFalse(self.plugin._task_is_live(task, snapshot))
        task["task_id"] = "other"
        self.assertTrue(self.plugin._task_is_live(task, snapshot))

    async def test_restore_and_foreign_wrapper_ownership(self):
        other = probe_module.RunnerProbe(self.plugin)
        self.assertFalse(other.install(self.Runner, self.Executor))
        self.plugin.probe.restore()
        self.assertIs(inspect.getattr_static(self.Executor, "execute"), self.original_executor)
        self.assertTrue(self.plugin.probe.install(self.Runner, self.Executor))

        async def foreign(runner):
            yield None

        self.Runner.step = foreign
        self.plugin.probe.restore()
        self.assertIs(self.Runner.step, foreign)

    async def test_terminate_unregisters_only_own_routes(self):
        def other():
            return None

        self.plugin.context.registered_web_apis.append(("/other", other, ["GET"], "other"))
        await self.plugin.terminate()
        self.assertEqual(len(self.plugin.context.registered_web_apis), 1)
        self.assertIs(self.plugin.context.registered_web_apis[0][1], other)


if __name__ == "__main__":
    unittest.main()
