"""Recognize our wrapper in a cooperative chain without removing other owners."""

import inspect


def wrapper_chain(value):
    seen = set()
    while value is not None and id(value) not in seen:
        seen.add(id(value))
        if isinstance(value, (classmethod, staticmethod)):
            value = value.__func__
            continue
        if inspect.ismethod(value):
            value = value.__func__
            continue
        yield value
        value = getattr(value, "__wrapped__", None)


def contains_wrapper(current, expected):
    target = next(wrapper_chain(expected), None)
    return target is not None and any(node is target for node in wrapper_chain(current))


def active_owned_wrapper(value, marker, owner_marker, legacy_class):
    for node in wrapper_chain(value):
        owner = getattr(node, owner_marker, None)
        if owner is not None:
            generation = getattr(node, "_astrbot_monitor_generation", None)
            if owner.active and (
                generation is None or generation is getattr(owner, "_generation", None)
            ):
                return True
            continue
        if not getattr(node, marker, False):
            continue
        # Older releases do not expose an owner marker. They may remain inside
        # another plugin's wrapper after unload; inspect only their closure.
        legacy = None
        for cell in getattr(node, "__closure__", ()) or ():
            try:
                candidate = cell.cell_contents
            except ValueError:
                continue
            if type(candidate).__name__ == legacy_class and hasattr(candidate, "plugin"):
                legacy = candidate
                break
        if legacy is not None:
            if getattr(legacy.plugin, "_started", True):
                return True
        elif getattr(node, "__wrapped__", None) is None:
            return True
    return False


def active_monitor_wrapper(value):
    return active_owned_wrapper(
        value, "_astrbot_llm_monitor_wrapper", "_astrbot_llm_monitor_owner", "RunnerProbe"
    )
