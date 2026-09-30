"""Small public-API doubles; source contract tests validate private targets."""

import importlib
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("monitor_test_plugin")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package

for name in (
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.message_components",
    "astrbot.api.star",
    "astrbot.api.web",
    "astrbot.core",
    "astrbot.core.utils",
    "astrbot.core.utils.active_event_registry",
):
    sys.modules[name] = types.ModuleType(name)
sys.modules["astrbot"].__version__ = "4.28.0"
sys.modules["astrbot.api"].AstrBotConfig = dict
sys.modules["astrbot.api"].logger = logging.getLogger("monitor-test")


class Event:
    unified_msg_origin = "test:GroupMessage:1"

    def __init__(self):
        self.extra = {}
        self.result = types.SimpleNamespace(chain=[])

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_result(self):
        return self.result

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


class Filters:
    def __getattr__(self, name):
        return lambda **kwargs: lambda handler: handler


class Plain:
    def __init__(self, text):
        self.text = text


class Context:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, *route):
        self.registered_web_apis.append(route)

    async def get_using_provider_async(self, umo):
        return types.SimpleNamespace(
            provider_config={"id": "primary"}, get_model=lambda: "default-model"
        )


sys.modules["astrbot.api.event"].AstrMessageEvent = Event
sys.modules["astrbot.api.event"].filter = Filters()
sys.modules["astrbot.api.message_components"].Plain = Plain
sys.modules["astrbot.api.star"].Context = Context
sys.modules["astrbot.api.star"].Star = type("Star", (), {"__init__": lambda self, context: None})
sys.modules["astrbot.api.star"].StarTools = types.SimpleNamespace(
    get_data_dir=lambda name: "unused-test-path"
)
sys.modules["astrbot.api.web"].json_response = lambda data, **kwargs: {
    "data": data,
    "status": kwargs.get("status_code", 200),
}
sys.modules["astrbot.api.web"].request = types.SimpleNamespace(query={})
registry = types.SimpleNamespace(_events={}, _agent_stop_callbacks={})
sys.modules["astrbot.core.utils.active_event_registry"].active_event_registry = registry

main = importlib.import_module("monitor_test_plugin.main")
store_module = importlib.import_module("monitor_test_plugin.store")
serialization = importlib.import_module("monitor_test_plugin.serialization")
probe_module = importlib.import_module("monitor_test_plugin.probe")
reply_filter = importlib.import_module("monitor_test_plugin.reply_filter")
