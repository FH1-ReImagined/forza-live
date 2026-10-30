"""Trusted filesystem plugins for Forza Live."""

import hashlib
import importlib.util
import inspect
import logging
import urllib.parse
from pathlib import Path


log = logging.getLogger("forza-live.plugins")


class PluginManager:
    """Loads ``*.py`` files from a plugin directory and exposes their hooks."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.plugins = []
        self.failed = []
        self._routes = {}
        self._startup_hooks = []
        self._tick_hooks = []
        self._loading_name = None

    def load(self, server):
        """Load every plugin once. A broken plugin is logged and does not stop the server."""
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._load_one(server, path)

    def _load_one(self, server, path):
        name = path.stem
        module_name = "forza_live_plugin_%s_%s" % (
            name, hashlib.sha256(str(path).encode()).hexdigest()[:12])
        routes = dict(self._routes)
        startup_hooks = list(self._startup_hooks)
        tick_hooks = list(self._tick_hooks)
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("could not create a module loader")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                raise TypeError("missing register(server) function")
            self._loading_name = name
            result = register(server)
            if inspect.isawaitable(result):
                raise TypeError("register(server) must not be asynchronous")
            self.plugins.append(name)
            log.info("plugin loaded: %s", name)
        except Exception as error:
            self._routes = routes
            self._startup_hooks = startup_hooks
            self._tick_hooks = tick_hooks
            self.failed.append({"name": name, "error": str(error)})
            log.exception("plugin %s was not loaded", name)
        finally:
            self._loading_name = None

    def _plugin_name(self):
        if self._loading_name is None:
            raise RuntimeError("plugin hooks can only be registered from register(server)")
        return self._loading_name

    def route(self, method, path, handler):
        """Register one exact HTTP endpoint returning ``(status, content_type, bytes)``."""
        name = self._plugin_name()
        method = str(method).upper()
        if not method or not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("route requires an HTTP method and an absolute path")
        if not callable(handler):
            raise TypeError("route handler must be callable")
        key = method, path
        if key in self._routes:
            raise ValueError("route %s %s is already registered" % key)
        self._routes[key] = name, handler

    def on_start(self, callback):
        self._register_callback(self._startup_hooks, callback, "start")

    def on_tick(self, callback):
        self._register_callback(self._tick_hooks, callback, "tick")

    def _register_callback(self, hooks, callback, kind):
        self._plugin_name()
        if not callable(callback):
            raise TypeError("%s hook must be callable" % kind)
        hooks.append((self._loading_name, callback))

    async def start(self, server):
        await self._run_hooks(self._startup_hooks, server, "start")

    async def tick(self, server):
        await self._run_hooks(self._tick_hooks, server, "tick")

    async def _run_hooks(self, hooks, server, kind):
        for name, callback in hooks:
            try:
                result = callback(server)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("plugin %s %s hook failed", name, kind)

    async def dispatch_http(self, server, method, path, headers, body, peer):
        """Return a plugin HTTP response, or ``None`` when no route owns the request."""
        url = urllib.parse.urlsplit(path)
        route = self._routes.get((method.upper(), url.path))
        if route is None:
            return None
        name, handler = route
        try:
            result = handler(server, url, headers, body, peer)
            if inspect.isawaitable(result):
                result = await result
            status, content_type, data = result
            if not isinstance(status, int) or not isinstance(content_type, str) or not isinstance(data, bytes):
                raise TypeError("handler must return (int, str, bytes)")
            return status, content_type, data
        except Exception:
            log.exception("plugin %s HTTP route %s %s failed", name, method, path)
            return 500, "text/plain", b"plugin error"

    def status(self):
        return {"loaded": list(self.plugins), "failed": list(self.failed)}


