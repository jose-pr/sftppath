"""The object two paths share when they resolve their names on one server."""

from __future__ import annotations

import threading as _threading
import weakref as _weakref

from ._base import BaseSFTPBackend


class _Namespace:
    """What two paths share when they resolve their names on one server: the
    object `_node_key()` hands out for an endpoint."""

    __slots__ = ("endpoint", "__weakref__")

    def __init__(self, endpoint):
        self.endpoint = endpoint


_NAMESPACES: "_weakref.WeakValueDictionary[object, _Namespace]" = (
    _weakref.WeakValueDictionary()
)
_NAMESPACES_LOCK = _threading.Lock()


def _namespace_of(backend: BaseSFTPBackend) -> object:
    endpoint = backend.endpoint
    if endpoint is None:
        return backend
    with _NAMESPACES_LOCK:
        found = _NAMESPACES.get(endpoint)
        if found is None:
            found = _NAMESPACES[endpoint] = _Namespace(endpoint)
        return found
