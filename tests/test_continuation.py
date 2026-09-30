import unittest

from monitor_test_plugin.continuation import CONTINUATION_EXTRA, IDENTITY_EXTRA, ContinuationProbe


class _Source:
    unified_msg_origin = "test:GroupMessage:1"

    def __init__(self):
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_name(self):
        return "test"

    def get_platform_id(self):
        return "platform-1"

    def get_message_type(self):
        return "GroupMessage"

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "User"

    def get_group_id(self):
        return "group-1"


class _Cron:
    def __init__(self, source):
        self.unified_msg_origin = source.unified_msg_origin
        self.extras = {}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value


class _Context:
    sent = 0

    async def send_message(self, session, chain):
        self.sent += 1
        return True


class _Handler:
    def __init__(self, context):
        self.context, self.event = context, None

    async def wake_ai_for_generation_task_result(self, *, task_id, source_event):
        self.event = _Cron(source_event)
        await self.context.send_message(source_event.unified_msg_origin, "image")


class _Plugin:
    def __init__(self):
        self.finished = []

    def capture_enabled(self, event):
        return True

    def finish_continuation(self, scope, status):
        self.finished.append((scope, status))

    def record_failure(self, error):
        raise error


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_identity_and_delivery_result_are_bound(self):
        plugin = _Plugin()
        context = _Context()
        probe = ContinuationProbe(plugin)
        probe.install(_Handler, _Cron, _Context)
        handler = _Handler(context)
        source = _Source()
        await handler.wake_ai_for_generation_task_result(
            task_id="generation-1", source_event=source
        )
        self.assertEqual(context.sent, 1)
        self.assertEqual(handler.event.get_extra(IDENTITY_EXTRA)["sender_id"], "user-1")
        self.assertIs(handler.event.get_extra(CONTINUATION_EXTRA)["source"], source)
        self.assertEqual(plugin.finished[0][1], "completed")
        probe.restore()
