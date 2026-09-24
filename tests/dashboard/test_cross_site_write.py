#!/usr/bin/env python3
"""Write endpoints refuse requests another web page could forge.

The browser keeps sending cached Basic Auth credentials, so without these
checks any page could submit a text/plain form to /api/send and type a
command into a pane.

Run: python3 -m pytest test_cross_site_write.py -q
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import server


def api_post(path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    old_authorized = server.Handler.authorized
    old_log_message = server.Handler.log_message
    server.Handler.authorized = lambda _handler: True
    server.Handler.log_message = lambda _handler, _fmt, *_args: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=2)
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return status, payload
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        server.Handler.authorized = old_authorized
        server.Handler.log_message = old_log_message


class CrossSiteWriteTest(unittest.TestCase):
    def test_forged_form_post_never_reaches_the_pane(self) -> None:
        body = b'{"pane":"%1","text":"touch /tmp/pwned","enter":true}'
        with mock.patch.object(server, "send_message_with_receipt") as send:
            status, payload = api_post("/api/send", body, {"Content-Type": "text/plain"})
            self.assertEqual((status, payload["error"]), (403, "write requests must be application/json"))
            status, _payload = api_post(
                "/api/send", body, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}
            )
            self.assertEqual(status, 403)
        send.assert_not_called()

    def test_own_page_and_local_clients_are_allowed(self) -> None:
        refusal = server.cross_site_write_refusal
        self.assertEqual(refusal({"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}, "/api/send"), "")
        self.assertEqual(refusal({"Content-Type": "application/json; charset=utf-8"}, "/api/prefs/pane"), "")
        self.assertEqual(refusal({"Content-Type": "image/png", "X-Cards-Upload": "1"}, "/api/files/upload"), "")
        self.assertNotEqual(refusal({"Content-Type": "text/plain"}, "/api/files/upload"), "")
        self.assertNotEqual(
            refusal({"Content-Type": "image/png", "X-Cards-Upload": "1", "Sec-Fetch-Site": "same-site"}, "/api/files/upload"),
            "",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
