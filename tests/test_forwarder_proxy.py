# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
"""The proxy half of the forwarder.

An iptables DNAT cannot publish the Huawei's web UI: it rewrites the IP header
and leaves the HTTP payload alone, so the Host the browser wrote survives, and
the router answers anything but its own address with a redirect to that
address -- which the client cannot route to. Measured on the club board:

    Host: 192.168.8.1     -> 200
    Host: 10.8.2.6:8082   -> 307 http://192.168.8.1/html/index.html?origin=xxx

The same test against the Foscam returns 200 for every Host, which is why
camera_ui.sh gets away with a plain forward. These pin the two rewrites that
make the difference.
"""

import http.client
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forwarder as router_ui  # noqa: E402

SEEN = []


class FakeRouter(BaseHTTPRequestHandler):
    """Behaves like the Huawei: only answers to its own name."""

    protocol_version = "HTTP/1.1"
    expect_host = None

    def log_message(self, *a):
        pass

    def _handle(self):
        SEEN.append((self.command, self.path, self.headers.get("Host"),
                     self.headers.get("X-Probe")))
        if (self.headers.get("Host") != self.server.expect_host
                or self.path.startswith("/redirect")):
            body = b"nope"
            self.send_response(307)
            self.send_header("Location",
                             "http://%s/html/index.html?origin=xxx"
                             % self.server.expect_host)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"<html><title>router</title></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "sid=abc; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_HEAD = _handle


class Base(unittest.TestCase):
    def setUp(self):
        SEEN.clear()
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
        self.up.daemon_threads = True
        self.up_port = self.up.server_address[1]
        # The name the fake router insists on, exactly as the Huawei does.
        self.up.expect_host = "127.0.0.1"
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.addCleanup(self.up.shutdown)

        self.px = router_ui.build("127.0.0.1", 0, "127.0.0.1", self.up_port)
        self.px_port = self.px.server_address[1]
        self.px.public_base = "http://127.0.0.1:%d" % self.px_port
        threading.Thread(target=self.px.serve_forever, daemon=True).start()
        self.addCleanup(self.px.shutdown)

    def get(self, path="/", method="GET", headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.px_port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        out = (r.status, dict(r.getheaders()), data)
        c.close()
        return out


class TestHostRewrite(Base):
    def test_the_router_sees_its_own_name_not_the_proxys(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200, "the proxy did not rewrite Host")
        self.assertIn(b"router", body)
        self.assertEqual(SEEN[0][2], "127.0.0.1")

    def test_without_the_rewrite_this_would_be_a_307(self):
        """Proves the fake router really is picky, so the test above means
        something -- a DNAT would land exactly here."""
        c = http.client.HTTPConnection("127.0.0.1", self.up_port, timeout=10)
        c.request("GET", "/", headers={"Host": "10.8.2.6:8082"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 307)
        c.close()

    def test_a_post_body_survives(self):
        status, _, _ = self.get("/api/x", method="POST", body=b"a=1",
                                headers={"Content-Length": "3"})
        self.assertEqual(status, 200)
        self.assertEqual(SEEN[0][0], "POST")

    def test_other_headers_are_passed_through(self):
        self.get("/", headers={"X-Probe": "keep-me"})
        self.assertEqual(SEEN[0][3], "keep-me")

    def test_response_headers_come_back(self):
        _, headers, _ = self.get("/")
        self.assertEqual(headers.get("Set-Cookie"), "sid=abc; Path=/")


class TestLocationRewrite(Base):
    def test_an_absolute_redirect_is_pointed_back_at_the_proxy(self):
        """Otherwise the browser follows it to an address only the board can
        reach, and the page dies there."""
        status, headers, _ = self.get("/redirect")
        self.assertEqual(status, 307)
        self.assertEqual(headers["Location"],
                         "http://127.0.0.1:%d/html/index.html?origin=xxx"
                         % self.px_port)

    def test_a_similar_looking_host_is_left_alone(self):
        """http://192.168.8.10/x starts with http://192.168.8.1 -- rewriting
        it would send the browser to the wrong machine entirely."""
        self.px.upstream_host = "192.168.8.1"
        self.px.upstream_port = 80
        h = Proxy = self.px.RequestHandlerClass
        rewrite = h._rewrite_location
        class _S:
            server = self.px
        self.assertEqual(rewrite(_S(), "http://192.168.8.10/x"),
                         "http://192.168.8.10/x")
        self.assertEqual(rewrite(_S(), "http://192.168.8.1/x"),
                         self.px.public_base + "/x")


class TestHopByHop(Base):
    def test_hop_by_hop_headers_are_not_forwarded(self):
        self.get("/", headers={"Connection": "keep-alive", "TE": "trailers"})
        c = http.client.HTTPConnection("127.0.0.1", self.up_port, timeout=10)
        c.close()
        # The upstream saw the request at all, which means the proxy did not
        # choke on them; a forwarded Transfer-Encoding would have desynced it.
        self.assertEqual(len(SEEN), 1)


# What used to be TestDiscovery lived on router_ui.main()'s --upstream/--tun
# flags. The forwarder takes its targets from the panel instead, so those
# behaviours moved: a target off every attached network is refused in
# test_forwarding.TestTheFence, and a tunnel forward with no tunnel address
# waits rather than binding elsewhere in TestForwarder.
