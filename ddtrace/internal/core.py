from collections import defaultdict
from contextlib import contextmanager
import logging
import threading
from typing import TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover
    from typing import Any
    from typing import Callable
    from typing import Dict
    from typing import List
    from typing import Optional
    from typing import Tuple

    from ddtrace.span import Span  # noqa


try:
    import contextvars
except ImportError:
    import ddtrace.vendor.contextvars as contextvars  # type: ignore


log = logging.getLogger(__name__)


_CURRENT_CONTEXT = None
ROOT_CONTEXT_ID = "__root"


class EventHub:
    def __init__(self):
        self._dispatch_lock = threading.Lock()
        self.reset()

    def has_listeners(self, event_id):
        # type: (str) -> bool
        return event_id in self._listeners

    def on(self, event_id, callback):
        # type: (str, Callable) -> None
        with self._dispatch_lock:
            self._listeners[event_id].append(callback)

    def reset(self):
        with self._dispatch_lock:
            self._listeners = defaultdict(list)

    def dispatch(self, event_id, args):
        # type: (str, List[Optional[Any]]) -> Tuple[List[Optional[Any]], List[Optional[Exception]]]
        with self._dispatch_lock:
            log.debug("Dispatching event %s", event_id)
            results = []
            exceptions = []
            for listener in self._listeners.get(event_id, []):
                log.debug("Calling listener %s", listener)
                result = None
                exception = None
                try:
                    result = listener(*args)
                except Exception as exc:
                    exception = exc
                results.append(result)
                exceptions.append(exception)
            return results, exceptions


class ExecutionContext:
    __slots__ = ["identifier", "_data", "_parents", "_event_hub", "_span", "_token"]

    def __init__(self, identifier, parent=None, span=None, **kwargs):
        self.identifier = identifier
        self._data = {}
        self._parents = []
        self._event_hub = EventHub()
        self._span = span
        if parent is not None:
            self.addParent(parent)
        self._data.update(kwargs)
        if self._span is None and _CURRENT_CONTEXT is not None:
            self._token = _CURRENT_CONTEXT.set(self)

    def __repr__(self):
        return "ExecutionContext '" + self.identifier + "'"

    @property
    def parents(self):
        return self._parents

    @property
    def parent(self):
        return self._parents[0] if self._parents else None

    def end(self):
        if self._span is None:
            try:
                _CURRENT_CONTEXT.reset(self._token)
            except ValueError:
                log.debug(
                    "Encountered ValueError during core contextvar reset() call. "
                    "This can happen when a span holding an executioncontext is "
                    "finished in a Context other than the one that started it."
                )
            except LookupError:
                log.debug(
                    "Encountered LookupError during core contextvar reset() call. I don't know why this is possible."
                )
        return dispatch("context.ended.%s" % self.identifier, [])

    def addParent(self, context):
        if self.identifier == ROOT_CONTEXT_ID:
            raise ValueError("Cannot add parent to root context")
        self._parents.append(context)
        self._data.update(context._data)

    @classmethod
    @contextmanager
    def context_with_data(cls, identifier, parent=None, span=None, **kwargs):
        new_context = cls(identifier, parent=parent, span=span, **kwargs)
        try:
            yield new_context
        finally:
            new_context.end()

    def get_item(self, data_key):
        # type: (str) -> Optional[Any]
        # NB mimic the behavior of `ddtrace.internal._context` by doing lazy inheritance
        current = self
        while current is not None and current.identifier != ROOT_CONTEXT_ID:
            if data_key in current._data:
                return current._data.get(data_key)
            current = current.parent
        return None

    def get_items(self, data_keys):
        # type: (List[str]) -> Optional[Any]
        return [self.get_item(key) for key in data_keys]

    def set_item(self, data_key, data_value):
        # type: (str, Optional[Any]) -> None
        self._data[data_key] = data_value

    def set_items(self, keys_values):
        # type: (Dict[str, Optional[Any]]) -> None
        for data_key, data_value in keys_values.items():
            self.set_item(data_key, data_value)


_CURRENT_CONTEXT = contextvars.ContextVar("ExecutionContext_var", default=ExecutionContext(ROOT_CONTEXT_ID))


def context_with_data(identifier, parent=None, **kwargs):
    return ExecutionContext.context_with_data(identifier, parent=(parent or _CURRENT_CONTEXT.get()), **kwargs)


def get_item(data_key, span=None):
    # type: (str, Optional[Span]) -> Optional[Any]
    if span is not None and span._local_root is not None:
        return span._local_root._get_ctx_item(data_key)
    else:
        return _CURRENT_CONTEXT.get().get_item(data_key)  # type: ignore


def get_items(data_keys, span=None):
    # type: (List[str], Optional[Span]) -> Optional[Any]
    if span is not None and span._local_root is not None:
        return [span._local_root._get_ctx_item(key) for key in data_keys]
    else:
        return _CURRENT_CONTEXT.get().get_items(data_keys)  # type: ignore


def set_item(data_key, data_value, span=None):
    # type: (str, Optional[Any], Optional[Span]) -> None
    if span is not None and span._local_root is not None:
        span._local_root._set_ctx_item(data_key, data_value)
    else:
        _CURRENT_CONTEXT.get().set_item(data_key, data_value)  # type: ignore


def set_items(keys_values, span=None):
    # type: (Dict[str, Optional[Any]], Optional[Span]) -> None
    if span is not None and span._local_root is not None:
        span._local_root._set_ctx_items(keys_values)
    else:
        _CURRENT_CONTEXT.get().set_items(keys_values)  # type: ignore


def has_listeners(event_id):
    # type: (str) -> bool
    return _CURRENT_CONTEXT.get()._event_hub.has_listeners(event_id)  # type: ignore


def on(event_id, callback):
    # type: (str, Callable) -> None
    return _CURRENT_CONTEXT.get()._event_hub.on(event_id, callback)  # type: ignore


def reset_listeners():
    # type: () -> None
    current = _CURRENT_CONTEXT.get()  # type: ignore
    while current is not None:
        current._event_hub.reset()
        current = current.parent


def dispatch(event_id, args):
    # type: (str, List[Optional[Any]]) -> Tuple[List[Optional[Any]], List[Optional[Exception]]]
    return _CURRENT_CONTEXT.get()._event_hub.dispatch(event_id, args)  # type: ignore
