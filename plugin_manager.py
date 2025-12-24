# plugin_manager.py
from __future__ import annotations

import importlib.util
import logging
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginEvent:
    name: str
    ts: float
    data: Dict[str, Any]


class PluginManager:
    """Local-only plugin/event hook dispatcher.

    - Plugins are Python modules in ./plugins/*.py (excluding files starting with '_' or '__').
    - Each plugin module may define callables named after events, e.g.:
        def on_message_received(event: dict) -> None: ...
      or a function named `handle_event(name: str, event: dict) -> None`.

    Safety:
    - All plugin exceptions are isolated.
    - Events are delivered on a background worker thread so critical RF paths are not blocked.
    - If no plugins are present, this is a no-op.
    """

    def __init__(self, plugins_dir: str = "plugins", queue_size: int = 1000) -> None:
        self._plugins_dir = str(plugins_dir)
        self._plugins: List[ModuleType] = []
        self._running = threading.Event()
        self._q: queue.Queue[PluginEvent] = queue.Queue(maxsize=int(queue_size))
        self._thread: Optional[threading.Thread] = None

        self._load_plugins()

    # ------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------

    def start(self) -> None:
        if self._running.is_set():
            return
        if not self._plugins:
            return
        self._running.set()
        self._thread = threading.Thread(target=self._worker, name="plugins-worker", daemon=True)
        self._thread.start()
        LOG.info("PluginManager started (%d plugins)", len(self._plugins))

    def stop(self, timeout: float = 2.0) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        try:
            self._q.put_nowait(PluginEvent(name="__stop__", ts=time.time(), data={}))
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        LOG.info("PluginManager stopped")

    def is_enabled(self) -> bool:
        return bool(self._plugins)

    def get_loaded_plugins(self) -> List[str]:
        names: List[str] = []
        for m in self._plugins:
            names.append(str(getattr(m, "PLUGIN_NAME", m.__name__)))
        return names

    # ------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------

    def emit(self, name: str, **data: Any) -> None:
        if not self._plugins:
            return
        if not isinstance(name, str) or not name:
            return
        ev = PluginEvent(name=name, ts=time.time(), data=dict(data))
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            # Drop rather than block mesh operation.
            return

    # ------------------------------------------------------------
    # internals
    # ------------------------------------------------------------

    def _load_plugins(self) -> None:
        pdir = self._plugins_dir
        if not os.path.isdir(pdir):
            return

        for fname in sorted(os.listdir(pdir)):
            if not fname.endswith(".py"):
                continue
            if fname.startswith("_") or fname.startswith("__"):
                continue
            fpath = os.path.join(pdir, fname)

            mod_name = f"plugins.{os.path.splitext(fname)[0]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, fpath)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[assignment]
                self._plugins.append(mod)
                LOG.info("Loaded plugin %s", getattr(mod, "PLUGIN_NAME", mod_name))
            except Exception:
                LOG.warning("Failed to load plugin %s", fpath, exc_info=True)

    def _worker(self) -> None:
        while self._running.is_set():
            try:
                ev = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if ev.name == "__stop__":
                continue
            self._dispatch(ev)

    def _dispatch(self, ev: PluginEvent) -> None:
        payload: Dict[str, Any] = {
            "name": ev.name,
            "ts": ev.ts,
            "data": ev.data,
        }

        for mod in list(self._plugins):
            try:
                # Preferred: function named after event
                fn = getattr(mod, ev.name, None)
                if callable(fn):
                    fn(payload)
                    continue

                # Fallback: handle_event(name, payload)
                h = getattr(mod, "handle_event", None)
                if callable(h):
                    h(ev.name, payload)
                    continue
            except Exception:
                # Isolation boundary: plugins cannot crash the mesh.
                LOG.error("Plugin error in %s for event %s\n%s",
                          getattr(mod, "__name__", "<plugin>"),
                          ev.name,
                          traceback.format_exc())
