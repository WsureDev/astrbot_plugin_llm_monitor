import asyncio
import functools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from support import Context, Event, main, store_module


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="monitor-compat-")
        self.plugin = main.LLMMonitorPlugin(Context(), {"monitor": {"enabled": True}})
        self.plugin.store = store_module.EventStore(Path(self.tmp.name) / "events.sqlite3")
        await self.plugin.store.start()
        self.plugin._started = True
        self.event = Event()
        plugin = self.plugin

        class Executor:
            @classmethod
            async def execute(cls, tool, run_context, **tool_args):
                yield {"content": tool.name}

        class Runner:
            def __init__(self, context):
                self.run_context = context
                self.provider = NS(provider_config={"id": "provider"}, get_model=lambda: "model")
                self.req = NS(model=None)
                self.finished = False

            async def _iter_llm_responses(self, *, include_model=True):
                yield NS(is_chunk=False, role="assistant", completion_text="ok", usage=None)

            async def step(self):
                async for response in self._iter_llm_responses():
                    yield NS(type="llm", data=response)
                async for result in Executor.execute(NS(name="comfyui_txt2img"), self.run_context):
                    yield NS(type="tool", data=result)
                if getattr(self, "gate", None):
                    await self.gate.wait()
                self.finished = True

            def done(self):
                return self.finished

            def get_final_llm_resp(self):
                return NS(role="assistant")

        self.Runner, self.Executor = Runner, Executor
        self.assertTrue(plugin.probe.install(Runner, Executor))

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.tmp.cleanup()

    def outer_wrappers(self):
        original = self.Runner.step

        @functools.wraps(original)
        async def step(runner):
            iterator = original(runner)
            try:
                async for result in iterator:
                    yield result
            finally:
                await iterator.aclose()

        self.Runner.step = step
        execute = self.Executor.execute

        @functools.wraps(execute)
        async def wrapped(cls, tool, run_context, **args):
            async for result in execute(tool, run_context, **args):
                yield result

        self.Executor.execute = classmethod(wrapped)

    async def test_health_accepts_connected_outer_wrappers_and_records(self):
        self.outer_wrappers()
        self.assertTrue(self.plugin.probe.snapshot()["enabled"])
        runner = self.Runner(NS(context=NS(event=self.event), messages=[]))
        [r async for r in runner.step()]
        await self.plugin.store.flush()
        record = await self.plugin.store.get_task(self.event.get_extra(main.TASK_EXTRA))
        self.assertEqual(len(record["llm_calls"]), 1)
        self.assertEqual(len(record["tool_calls"]), 1)

    async def test_child_shared_event_is_recorded_after_parent_finishes(self):
        parent = self.Runner(NS(context=NS(event=self.event), messages=[]))
        [r async for r in parent.step()]
        parent_id = self.event.get_extra(main.TASK_EXTRA)
        self.assertTrue(self.event.get_extra(main.TASK_STATE_EXTRA)["ended"])
        child = self.Runner(NS(context=NS(event=self.event), messages=[]))
        [r async for r in child.step()]
        await self.plugin.store.flush()
        records = (await self.plugin.store.list_tasks())["items"]
        self.assertEqual(len(records), 2)
        record = next(r for r in records if r["task_id"] != parent_id)
        self.assertEqual(record["parent_task_id"], parent_id)
        self.assertEqual(record["trigger"], "subagent")
        self.assertEqual(record["status"], "completed")
        child_record = await self.plugin.store.get_task(record["task_id"])
        self.assertEqual(len(child_record["llm_calls"]), 1)
        self.assertEqual(len(child_record["tool_calls"]), 1)
        self.assertEqual(self.event.get_extra(main.TASK_EXTRA), parent_id)

    async def test_child_and_parent_can_overlap_without_ending_each_other(self):
        parent = self.Runner(NS(context=NS(event=self.event), messages=[]))
        parent.gate = asyncio.Event()
        iterator = parent.step()
        await anext(iterator)
        parent_id = self.event.get_extra(main.TASK_EXTRA)
        child = self.Runner(NS(context=NS(event=self.event), messages=[]))
        [r async for r in child.step()]
        self.assertFalse(self.event.get_extra(main.TASK_STATE_EXTRA).get("ended", False))
        parent.gate.set()
        [r async for r in iterator]
        await self.plugin.store.flush()
        records = (await self.plugin.store.list_tasks())["items"]
        self.assertEqual({r["status"] for r in records}, {"completed"})
        self.assertTrue(any(r["parent_task_id"] == parent_id for r in records))

    async def test_unload_and_reload_with_outer_wrapper_is_not_duplicate_probe(self):
        self.outer_wrappers()
        outer = self.Runner.step
        self.plugin.probe.restore()
        self.assertIs(self.Runner.step, outer)
        self.assertTrue(self.plugin.probe.install(self.Runner, self.Executor))
        [r async for r in self.Runner(NS(context=NS(event=self.event), messages=[])).step()]
        await self.plugin.store.flush()
        record = await self.plugin.store.get_task(self.event.get_extra(main.TASK_EXTRA))
        self.assertEqual(len(record["llm_calls"]), 1)
        self.assertEqual(len(record["tool_calls"]), 1)

    async def test_cancelled_child_does_not_cancel_parent(self):
        parent = self.Runner(NS(context=NS(event=self.event), messages=[]))
        parent.gate = asyncio.Event()
        iterator = parent.step()
        await anext(iterator)
        child = self.Runner(NS(context=NS(event=self.event), messages=[]))
        child.gate = asyncio.Event()

        async def execute():
            [r async for r in child.step()]

        task = asyncio.create_task(execute())
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.event.get_extra(main.TASK_STATE_EXTRA).get("ended", False))
        parent.gate.set()
        [r async for r in iterator]
        await self.plugin.store.flush()
        statuses = {r["status"] for r in (await self.plugin.store.list_tasks())["items"]}
        self.assertEqual(statuses, {"completed", "cancelled"})

    async def test_real_media_and_monitor_wrappers_work_in_both_install_orders(self):
        source = Path(__file__).resolve().parents[2] / "astrbot_plugin_channel_media_tools"
        if not source.is_dir():
            self.skipTest("sibling channel media plugin checkout required")
        import sys

        sys.path.insert(0, str(source.parent))
        from astrbot_plugin_channel_media_tools.integration import RunnerIntegration

        for media_first in (False, True):
            self.plugin.probe.restore()
            self.Runner._sanitize_contexts_for_provider = lambda _, messages: messages
            owner = NS(
                policy=NS(
                    before_step=AsyncMock(), finish=AsyncMock(), sanitize_payload=lambda _, p: p
                ),
                channels=NS(release_transfers=Mock()),
                enabled_for_runner=lambda r: True,
                enabled_event=lambda e: True,
            )
            media = RunnerIntegration(owner)
            if media_first:
                media.install(self.Runner, self.Executor)
                self.assertTrue(self.plugin.probe.install(self.Runner, self.Executor))
            else:
                self.assertTrue(self.plugin.probe.install(self.Runner, self.Executor))
                media.install(self.Runner, self.Executor)
            event = Event()
            runner = self.Runner(NS(context=NS(event=event), messages=[]))
            try:
                self.assertTrue(self.plugin.probe.snapshot()["enabled"])
                [r async for r in runner.step()]
                await self.plugin.store.flush()
                record = await self.plugin.store.get_task(event.get_extra(main.TASK_EXTRA))
                self.assertEqual(len(record["llm_calls"]), 1)
                self.assertEqual(len(record["tool_calls"]), 1)
                self.assertEqual(owner.policy.before_step.await_count, 1)
            finally:
                media.terminate()
                self.plugin.probe.restore()
