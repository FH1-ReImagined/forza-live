import asyncio
import tempfile
import unittest
from pathlib import Path

import forza_plugins


class Server:
    pass


class PluginTests(unittest.TestCase):
    def test_loads_hooks_and_http_route(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "example.py").write_text('''
def register(server):
    def route(server, url, headers, body, peer):
        return 200, "text/plain", b"plugin response"

    server.plugins.on_start(lambda server: setattr(server, "started", True))
    server.plugins.on_tick(lambda server: setattr(server, "ticks", getattr(server, "ticks", 0) + 1))
    server.plugins.route("GET", "/plugin-test", route)
''')
            server = Server()
            server.plugins = forza_plugins.PluginManager(directory)
            server.plugins.load(server)

            asyncio.run(server.plugins.start(server))
            asyncio.run(server.plugins.tick(server))
            reply = asyncio.run(server.plugins.dispatch_http(
                server, "GET", "/plugin-test", {}, b"", ("127.0.0.1", 1)))

            self.assertEqual(["example"], server.plugins.plugins)
            self.assertEqual([], server.plugins.failed)
            self.assertTrue(server.started)
            self.assertEqual(1, server.ticks)
            self.assertEqual((200, "text/plain", b"plugin response"), reply)

    def test_skips_plugins_without_register_function(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "broken.py").write_text("value = 1\n")
            server = Server()
            server.plugins = forza_plugins.PluginManager(directory)
            server.plugins.load(server)

            self.assertEqual([], server.plugins.plugins)
            self.assertEqual("broken", server.plugins.failed[0]["name"])


if __name__ == "__main__":
    unittest.main()

