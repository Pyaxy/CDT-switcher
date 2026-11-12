import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from cdt_switcher import make_webhook_handler


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.submit = Mock(return_value=True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_webhook_handler({'i-known': 'A'}, self.submit))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def post(self, body, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request('POST', '/', body, headers or {})
            response = conn.getresponse()
            status = response.status
            try:
                response.read()
            except ConnectionResetError:
                if status != 413:
                    raise
            return status
        finally:
            conn.close()

    def test_known_events_and_backpressure(self):
        for body in ({'instance_id': 'i-known'}, {'detail': {'instance_id': 'i-known'}}):
            self.assertEqual(self.post(json.dumps(body)), 200)
        self.assertEqual(self.submit.call_count, 2)
        self.submit.assert_called_with('SPOT_WARN', account='A')
        self.submit.return_value = False
        self.assertEqual(self.post('{"instance_id":"i-known"}'), 503)

    def test_unknown_events_are_ignored(self):
        for body in ({}, {'detail': {}}, {'instance_id': 'other'}):
            self.assertEqual(self.post(json.dumps(body)), 200)
        self.submit.assert_not_called()

    def test_invalid_payloads_return_400(self):
        for body in ('', '{', '[]', 'null', '{"detail":null}', '{"detail":[]}',
                     '{"instance_id":[]}', '{"instance_id":""}',
                     '{"detail":{"instance_id":1}}', b'\xff'):
            with self.subTest(body=body):
                self.assertEqual(self.post(body), 400)
        self.submit.assert_not_called()

    def test_body_limit(self):
        self.assertEqual(self.post(b'x' * (64 * 1024 + 1)), 413)
        self.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
