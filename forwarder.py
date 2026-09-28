# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. This
# software aids surveillance; it does not guarantee it, and no liability is
# accepted for any failure to detect, record, retain or report an event.
# See the NOTICE file for the full disclaimer.

"""Publish devices the board is next to, on board ports.

An iptables DNAT is the obvious way to publish something on a segment the
client cannot route to, and camera_ui.sh does exactly that for the camera. It
works there by luck. A DNAT rewrites the IP header and leaves the HTTP payload
alone, so the Host the browser wrote arrives intact -- and while the Foscam
ignores Host entirely, the 4G router validates it (ordinary DNS-rebinding
protection) and answers anything else with a redirect to its own address:

    Host: 192.168.8.1     -> 200, the real UI
    Host: 10.8.2.6:8082   -> 307 http://192.168.8.1/html/index.html?origin=xxx

A browser follows that redirect to an address it cannot reach, so the page
never loads. Forwarding packets is not enough; the request has to be rewritten,
which means a proxy rather than a NAT rule. socat cannot do it either -- it
moves bytes and does not parse HTTP.

So this is a small reverse proxy: it rewrites Host on the way in and rewrites
absolute Location headers back on the way out, so the browser stays on the
proxy instead of being sent to an address only the board can reach.

Which devices are published is configured in the panel, not here, and every
entry is checked against the networks the board is actually attached to before
it is stored -- see Settings.parse_forwards. A forward exposed as "tunnel"
binds to the tunnel address, so nothing outside the VPN can open a socket to
it; that is the right default for the router UI, which holds the SIM, the wifi
password and the firewall, and a weaker need for the camera.

One process serves them all and reconciles itself when the settings file
changes, so adding a forward does not need a restart, a root password, or an
iptables rule that outlives the device it pointed at.
"""

import argparse
import http.client
import ipaddress
import logging
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("forwarder")

# Set per RFC 7230; a proxy must not pass these along.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade"}

DEFAULT_PORT = 8082
BODY_LIMIT = 32 * 1024 * 1024


def default_gateway():
    """The router's address, discovered rather than hard-coded."""
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def tunnel_address(iface="tun0"):
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rknn-routerui"
    sys_version = ""

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def _upstream_headers(self):
        out = []
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "host":
                continue
            # The board is not the origin; a forwarded chain would only
            # confuse a device that has never heard of one.
            if lk in ("x-forwarded-for", "x-forwarded-host", "x-real-ip"):
                continue
            out.append((k, v))
        # The whole point: the router only answers to its own name.
        out.append(("Host", self.server.upstream_host))
        return out

    def _rewrite_location(self, value):
        """Keep the browser on the proxy.

        With the Host header right the router should not redirect at all, but
        a device that redirects to itself the moment something is off is
        exactly the device that produced this file.
        """
        for scheme in ("http://", "https://"):
            # The port-qualified form FIRST: "http://h" is also a prefix of
            # "http://h:80", so the bare check would strip the host and leave
            # the port glued to the path -- ":80/html/index.html".
            prefix_port = "%s%s:%d" % (scheme, self.server.upstream_host,
                                       self.server.upstream_port)
            if value.startswith(prefix_port):
                return self.server.public_base + value[len(prefix_port):]
            prefix = scheme + self.server.upstream_host
            if value.startswith(prefix):
                rest = value[len(prefix):]
                # Only a real boundary counts; "http://192.168.8.10/x" must not
                # be rewritten because it starts with "http://192.168.8.1".
                if rest == "" or rest[0] in "/?#":
                    return self.server.public_base + rest
        return value

    def _relay(self):
        length = self.headers.get("Content-Length")
        body = None
        if length:
            try:
                n = int(length)
            except ValueError:
                self.send_error(400, "bad Content-Length")
                return
            if n > BODY_LIMIT:
                self.send_error(413, "body too large")
                return
            body = self.rfile.read(n)

        conn = http.client.HTTPConnection(self.server.upstream_host,
                                          self.server.upstream_port,
                                          timeout=30)
        try:
            conn.putrequest(self.command, self.path, skip_host=True,
                            skip_accept_encoding=True)
            for k, v in self._upstream_headers():
                conn.putheader(k, v)
            conn.endheaders(body)
            resp = conn.getresponse()
            payload = resp.read()
        except (OSError, http.client.HTTPException) as e:
            log.warning("upstream %s:%s failed: %s", self.server.upstream_host,
                        self.server.upstream_port, e)
            self.send_error(502, "the router did not answer")
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "content-length":
                continue
            if lk == "location":
                v = self._rewrite_location(v)
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    # ------------------------------------------------------------- methods
    def do_GET(self):
        self._relay()

    do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_GET


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build(bind, port, upstream, upstream_port, public_host=None):
    srv = ProxyServer((bind, port), Proxy)
    srv.upstream_host = upstream
    srv.upstream_port = upstream_port
    srv.public_base = "http://%s:%d" % (public_host or bind, port)
    return srv


def tunnel_address(iface="tun0"):
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def _key(fwd, bind):
    """What makes a running listener the same as a wanted one."""
    return (bind, fwd["port"], fwd["host"], fwd["target_port"])


class Forwarder:
    """Owns one listener per enabled forward, and keeps them matching.

    Reconciled rather than restarted: a forward the operator did not touch
    keeps its socket, so adding the router does not interrupt somebody looking
    at the camera.
    """

    def __init__(self, settings, tun="tun0,tailscale0", poll_s=5.0):
        self.settings = settings
        self.tun = tun
        self.tun_ifaces = [t.strip() for t in str(tun).split(",") if t.strip()]
        self.poll_s = float(poll_s)
        self._running = {}          # key -> (server, thread, name)
        self._stop = threading.Event()
        self._last_warn = None

    def binds_for(self, fwd):
        """Where a forward should listen. A list, because "the tunnel" is no
        longer one thing.

        There are two private ways in now -- OpenVPN for development and
        Tailscale for a phone -- and "expose: tunnel" means both of them, not
        whichever happens to be checked first. Listening on one and not the
        other produced a forward that was allowed by the ACL and still
        refused the connection, which is a confusing way to spend an evening.

        It deliberately does NOT fall back to 0.0.0.0 when no tunnel has an
        address. A forward that quietly appears on the club wifi because the
        VPN was down is the opposite of what "tunnel only" asked for.
        """
        if fwd["expose"] == "local":
            return ["0.0.0.0"]
        out = []
        for iface in self.tun_ifaces:
            addr = tunnel_address(iface)
            if addr and addr not in out:
                out.append(addr)
        return out

    def desired(self):
        out = {}
        for fwd in self.settings.forwards:
            if not fwd.get("enabled", True):
                continue
            binds = self.binds_for(fwd)
            if not binds:
                self._warn("%s wants a tunnel, and none has an address yet"
                           % fwd["name"])
                continue
            for bind in binds:
                out[_key(fwd, bind)] = fwd
        return out

    def _warn(self, msg):
        # The tunnel flaps; saying so once per state change is enough.
        if self._last_warn != msg:
            self._last_warn = msg
            log.warning("%s", msg)

    def reconcile(self):
        want = self.desired()
        for key in list(self._running):
            if key not in want:
                srv, thread, name = self._running.pop(key)
                log.info("stopping %s on :%d", name, key[1])
                srv.shutdown()
                srv.server_close()
        for key, fwd in want.items():
            if key in self._running:
                continue
            bind, port, host, target_port = key
            try:
                srv = build(bind, port, host, target_port)
            except OSError as e:
                self._warn("%s: cannot listen on %s:%d (%s)"
                           % (fwd["name"], bind, port, e))
                continue
            t = threading.Thread(target=srv.serve_forever, daemon=True,
                                 name="fwd-%d" % port)
            t.start()
            self._running[key] = (srv, t, fwd["name"])
            log.info("%s: http://%s:%d/  ->  %s:%d (Host rewritten)",
                     fwd["name"], bind, port, host, target_port)

    def run(self):
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("could not reconcile the forwards")
            self._stop.wait(self.poll_s)
        self.stop()

    def stop(self):
        self._stop.set()
        for key, (srv, _t, name) in list(self._running.items()):
            log.info("stopping %s", name)
            srv.shutdown()
            srv.server_close()
            self._running.pop(key, None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tun", default="tun0,tailscale0",
                    help="comma-separated; a forward exposed to \"tunnel\" listens on each one that has an address")
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--config", default=None, help="config.yaml to read")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    import config as config_mod
    from settings import Settings
    try:
        cfg = config_mod.load(args.config, require_password=False)
    except Exception as e:
        log.error("could not read the config: %s", e)
        return 2
    # The same file the panel writes, so a forward added there is live within
    # a poll -- no restart, no root, no iptables rule to remember.
    path = cfg.events_root.parent / "settings.json"
    settings = Settings(path)

    fw = Forwarder(settings, tun=args.tun, poll_s=args.poll)
    log.info("watching %s for forwards", path)
    try:
        fw.run()
    except KeyboardInterrupt:
        fw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
