"""Desktop launch orchestration; no executable or browser is started by tests."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


TEMP = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'SUIJIAN_DATA': str(Path(TEMP.name) / 'data')}):
    import desktop


@contextlib.contextmanager
def local_health(payload, status=200, location=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            if location:
                self.send_header('Location', location)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join(timeout=2)


class DesktopLaunch(unittest.TestCase):
    def test_existing_studio_reopens_without_second_server(self):
        with patch.dict(os.environ, {'SUIJIAN_PORT': '8876'}), \
             patch.object(desktop, 'port_in_use', return_value=True), \
             patch.object(desktop, 'is_suijian_running', return_value=True) as health, \
             patch.object(desktop, 'open_window') as window, \
             patch.object(desktop.uvicorn, 'run') as run, \
             patch.object(desktop.threading, 'Thread') as thread:
            desktop.main()
        health.assert_called_once_with('http://127.0.0.1:8876')
        window.assert_called_once_with('http://127.0.0.1:8876')
        run.assert_not_called()
        thread.assert_not_called()

    def test_unrelated_service_has_clear_error_and_is_never_opened(self):
        with patch.dict(os.environ, {'SUIJIAN_PORT': '8891'}), \
             patch.object(desktop, 'port_in_use', return_value=True), \
             patch.object(desktop, 'is_suijian_running', return_value=False), \
             patch.object(desktop, 'open_window') as window, \
             patch.object(desktop.uvicorn, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '8891.*其它程序.*SUIJIAN_PORT'):
                desktop.main()
        window.assert_not_called()
        run.assert_not_called()

    def test_new_server_binds_only_loopback_and_opens_after_readiness(self):
        with patch.dict(os.environ, {'SUIJIAN_PORT': '8892'}), \
             patch.object(desktop, 'port_in_use', return_value=False), \
             patch.object(desktop, 'open_window') as window, \
             patch.object(desktop.uvicorn, 'run') as run, \
             patch.object(desktop.threading, 'Thread') as thread:
            desktop.main()
        thread.assert_called_once_with(target=desktop.open_when_ready,
                                       args=('http://127.0.0.1:8892',), daemon=True)
        thread.return_value.start.assert_called_once()
        window.assert_not_called()
        run.assert_called_once_with(desktop.app, host='127.0.0.1', port=8892,
                                    access_log=False, log_config=None)

    def test_invalid_port_fails_before_network_or_browser(self):
        for value in ('not-a-port', '0', '65536', '-1'):
            with self.subTest(value=value), \
                 patch.dict(os.environ, {'SUIJIAN_PORT': value}), \
                 patch.object(desktop, 'port_in_use') as network:
                with self.assertRaisesRegex(RuntimeError, 'SUIJIAN_PORT.*1—65535'):
                    desktop.main()
                network.assert_not_called()

    def test_readiness_wait_does_not_open_a_blank_page(self):
        with patch.object(desktop, 'is_suijian_running', side_effect=[False, False, True]), \
             patch.object(desktop.time, 'sleep') as sleep, \
             patch.object(desktop, 'open_window') as window:
            desktop.open_when_ready('http://127.0.0.1:8876')
        self.assertEqual(sleep.call_count, 2)
        window.assert_called_once_with('http://127.0.0.1:8876')

    def test_readiness_timeout_is_visible_in_service_log(self):
        messages = io.StringIO()
        with patch.object(desktop, 'is_suijian_running', return_value=False), \
             patch.object(desktop.time, 'monotonic', side_effect=[0, 0, 21]), \
             patch.object(desktop.time, 'sleep'), \
             patch.object(desktop, 'open_window') as window, \
             contextlib.redirect_stderr(messages):
            desktop.open_when_ready('http://127.0.0.1:8876')
        window.assert_not_called()
        self.assertIn('启动等待超时', messages.getvalue())

    def test_real_http_health_identifies_only_the_named_studio(self):
        for payload, expected in [({'app': 'suijian-studio', 'version': '0.5.0'}, True),
                                  ({'app': 'another-app'}, False),
                                  (['suijian-studio'], False)]:
            with self.subTest(payload=payload), local_health(payload) as url:
                self.assertTrue(desktop.port_in_use(int(url.rsplit(':', 1)[1])))
                self.assertEqual(desktop.is_suijian_running(url), expected)

    def test_health_redirect_is_not_followed(self):
        with local_health({'app': 'suijian-studio'}) as target, \
             local_health({}, status=302, location=target) as redirect:
            self.assertFalse(desktop.is_suijian_running(redirect))


if __name__ == '__main__':
    unittest.main()
