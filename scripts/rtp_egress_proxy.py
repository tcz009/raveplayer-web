#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal HTTP CONNECT egress proxy for RaveTapePacks.

Runs on the homelab VPN host, whose default route is the VPN tunnel.  The web
app and the Kodi add-on use it as a per-host proxy: only RaveTapePacks
hostnames are allowed, and only clients on the home LAN may connect.  It is
deliberately tiny (stdlib only, CONNECT only) and logs no URLs, query strings
or credentials.

Usage on the VPN host::

    python3 rtp_egress_proxy.py --listen 0.0.0.0:8888

Install as the ``pi`` user (no root needed for a high port): see
``docs/RTP-EGRESS.md``.  Stop it with SIGTERM.
"""

import argparse
import ipaddress
import os
import select
import signal
import socket
import socketserver
import sys
import threading
import time

DEFAULT_HOSTS = ("ravetapepacks.com",)
# Private ranges only: this must never be reachable from the WAN.  Narrow it
# further with --allow-client when the proxy host has other networks attached.
DEFAULT_CLIENTS = ("127.0.0.1/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
MAX_HEADER_BYTES = 16 * 1024
CONNECT_TIMEOUT = 10.0
CLIENT_TIMEOUT = 20.0
IDLE_TIMEOUT = 180.0
BUFFER_BYTES = 64 * 1024
ALLOWED_PORTS = (443, 80)


def _host_allowed(host, allowed):
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for suffix in allowed:
        suffix = suffix.strip().lower().lstrip(".")
        if not suffix:
            continue
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _client_allowed(address, networks):
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in networks)


def _parse_authority(raw):
    """Split ``host:port`` (including ``[v6]:port``) or return (None, None)."""
    text = raw.decode("ascii", "replace").strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end + 1:].startswith(":"):
            return None, None
        host, port = text[1:end], text[end + 2:]
    else:
        host, sep, port = text.rpartition(":")
        if not sep:
            return None, None
    if not host or not port.isdigit():
        return None, None
    try:
        number = int(port)
    except ValueError:
        return None, None
    if number < 1 or number > 65535:
        return None, None
    return host, number


class _Handler(socketserver.BaseRequestHandler):
    verbose = False
    allowed_hosts = DEFAULT_HOSTS
    allowed_clients = tuple(
        ipaddress.ip_network(net) for net in DEFAULT_CLIENTS
    )

    def handle(self):
        client = self.client_address[0]
        if not _client_allowed(client, self.allowed_clients):
            self._deny(403, "client not allowed")
            return
        try:
            request = self._read_request()
        except (socket.timeout, OSError):
            return
        if not request:
            return
        line = request.split(b"\r\n", 1)[0]
        parts = line.split()
        if len(parts) != 3:
            self._deny(400, "bad request")
            return
        method, target, version = parts
        if version not in (b"HTTP/1.0", b"HTTP/1.1"):
            self._deny(400, "bad version")
            return
        if method.upper() != b"CONNECT":
            self._deny(405, "CONNECT only")
            return
        host, port = _parse_authority(target)
        if not host:
            self._deny(400, "bad authority")
            return
        if port not in ALLOWED_PORTS:
            self._deny(403, "port not allowed")
            return
        if not _host_allowed(host, self.allowed_hosts):
            self._deny(403, "host not allowed")
            return
        if self.verbose:
            print("egress %s:%d" % (host.lower(), port), file=sys.stderr, flush=True)
        try:
            upstream = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            # The hostname is safe to log; never log the tunneled URL/path.
            print("connect failed for %s:%d (%s)" % (host.lower(), port, exc),
                  file=sys.stderr, flush=True)
            self._deny(502, "upstream connect failed")
            return
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _read_request(self):
        self.request.settimeout(CLIENT_TIMEOUT)
        chunks, total = [], 0
        while b"\r\n\r\n" not in b"".join(chunks):
            data = self.request.recv(4096)
            if not data:
                return b""
            chunks.append(data)
            total += len(data)
            if total > MAX_HEADER_BYTES:
                return b""
        return b"".join(chunks).split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"

    def _deny(self, code, reason):
        try:
            self.request.sendall(
                ("HTTP/1.1 %d %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                 % (code, reason)).encode("ascii", "replace"))
        except OSError:
            pass

    def _relay(self, upstream):
        self.request.setblocking(False)
        upstream.setblocking(False)
        peers = {self.request: upstream, upstream: self.request}
        try:
            while True:
                readable, _writable, errored = select.select(
                    list(peers), [], list(peers), IDLE_TIMEOUT)
                if errored or not readable:
                    return
                for source in readable:
                    try:
                        data = source.recv(BUFFER_BYTES)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    destination = peers[source]
                    try:
                        destination.sendall(data)
                    except (BlockingIOError, InterruptedError):
                        # Very unlikely for TLS-sized frames; drop the tunnel
                        # rather than buffer unbounded data.
                        return
                    except OSError:
                        return
        finally:
            try:
                self.request.setblocking(True)
            except OSError:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _parse_networks(values):
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise SystemExit("bad --allow-client network: %r" % value)
    return tuple(networks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0:8888",
                        help="bind address (default 0.0.0.0:8888)")
    parser.add_argument("--allow-host", action="append", default=None,
                        help="allowed host suffix (repeatable)")
    parser.add_argument("--allow-client", action="append", default=None,
                        help="allowed client CIDR (repeatable)")
    parser.add_argument("--verbose", action="store_true",
                        help="log one line per allowed tunnel (host only)")
    args = parser.parse_args(argv)

    try:
        listen_host, listen_port = args.listen.rsplit(":", 1)
        listen_port = int(listen_port)
    except ValueError:
        raise SystemExit("bad --listen address: %r" % args.listen)

    _Handler.verbose = bool(args.verbose)
    _Handler.allowed_hosts = tuple(args.allow_host or DEFAULT_HOSTS)
    _Handler.allowed_clients = _parse_networks(
        args.allow_client or list(DEFAULT_CLIENTS))

    server = _Server((listen_host, listen_port), _Handler)
    server.timeout = 1.0

    def _stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass

    print("rtp-egress proxy listening on %s:%d (hosts: %s)"
          % (listen_host, listen_port, ",".join(_Handler.allowed_hosts)),
          file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
