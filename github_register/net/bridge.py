"""Local auth proxy bridge — a plain local HTTP proxy that relays to an
authenticated upstream (HTTP Basic or SOCKS5 user/pass).

Why this exists:
- Firefox does not support authenticated SOCKS5 proxies ("Browser does not
  support socks5 proxy authentication").
- Many gateways reject locally resolved DNS (plain socks5://), so CONNECT
  must use the hostname form (ATYP=0x03) to resolve DNS at the gateway.

The bridge listens on 127.0.0.1 with NO auth (browsers love that) and
injects the upstream credentials itself.

This module is browser- and site-agnostic: nothing here knows about GitHub
or Camoufox, so it can be shared across regkit-style projects.
"""
from __future__ import annotations

import base64
import os
import socket
import socketserver
import threading
from typing import Optional
from urllib.parse import urlsplit


class ProxyBridgeError(RuntimeError):
    pass


class _AuthBridgeHandler(socketserver.BaseRequestHandler):
    # populated by LocalAuthProxyBridge.start() via a bound subclass
    upstream: dict = {}

    def _relay(self, src: socket.socket, dst: socket.socket, timeout: float = 180.0) -> None:
        """Bidirectional relay using two pump threads (blocking one-way relay
        deadlocks TLS: the handshake needs simultaneous both-direction I/O).

        Each pump only shuts down ITS OWN direction when it sees EOF, so a
        closed write-side never kills the other direction mid-flight (a
        browser that half-closes its request side still receives the
        response). Full close happens once both directions are drained.
        """
        src.settimeout(timeout)
        dst.settimeout(timeout)
        done = threading.Event()

        def pump(a: socket.socket, b: socket.socket) -> None:
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    a.shutdown(socket.SHUT_RD)
                    b.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                if done.is_set():
                    # both directions drained — close everything for real
                    for sock in (src, dst):
                        try:
                            sock.close()
                        except OSError:
                            pass
                done.set()

        t1 = threading.Thread(target=pump, args=(src, dst), daemon=True)
        t2 = threading.Thread(target=pump, args=(dst, src), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def _connect_upstream(self) -> socket.socket:
        s = socket.create_connection((self.upstream["host"], self.upstream["port"]), timeout=20)
        if self.upstream["socks"]:
            # minimal SOCKS5 handshake with remote DNS (ATYP=0x03 hostname)
            s.sendall(b"\x05\x01\x02")  # greet: support user/pass auth
            resp = s.recv(2)
            if len(resp) < 2 or resp[0] != 5:
                raise OSError("socks5: bad greeting")
            if resp[1] == 0x02:
                user = self.upstream["user"].encode()
                pwd = self.upstream["pass"].encode()
                s.sendall(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
                resp = s.recv(2)
                if len(resp) < 2 or resp[1] != 0:
                    raise OSError("socks5: auth rejected")
            elif resp[1] != 0x00:
                raise OSError("socks5: no acceptable auth method")
        return s

    def _socks5_connect_remote(self, s: socket.socket, host: str, port: int) -> None:
        """SOCKS5 CONNECT with hostname (ATYP=0x03) so DNS resolves at the gateway."""
        h = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + port.to_bytes(2, "big"))
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            raise OSError(f"socks5: connect failed code={resp[1] if len(resp) > 1 else '?'}")

    def _inject_auth_header(self, data: bytes) -> bytes:
        """Rewrite/add 'Proxy-Authorization: Basic ...' on the first request."""
        if not self.upstream.get("user"):
            return data
        token = base64.b64encode(
            f"{self.upstream['user']}:{self.upstream['pass']}".encode()
        ).decode()
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep:
            return data
        lines = head.split(b"\r\n")
        out = [lines[0]]
        for ln in lines[1:]:
            if ln.lower().startswith(b"proxy-authorization:"):
                continue  # drop existing
            out.append(ln)
        out.append(f"Proxy-Authorization: Basic {token}".encode())
        return b"\r\n".join(out) + b"\r\n\r\n" + rest

    def handle(self) -> None:
        try:
            self.request.settimeout(20)
            first = self.request.recv(65536)
            if not first:
                return
            if first[:7] == b"CONNECT":
                # --- HTTPS tunnel ---
                line = first.split(b"\r\n", 1)[0]
                hostport = line.split()[1].decode()
                host, _, port_s = hostport.rpartition(":")
                port = int(port_s or "443")
                upstream = self._connect_upstream()
                if self.upstream["socks"]:
                    # SOCKS5 CONNECT with remote DNS, then tell the browser the
                    # tunnel is up — do NOT wait for upstream data (deadlock:
                    # upstream waits for the browser's TLS ClientHello).
                    self._socks5_connect_remote(upstream, host, port)
                    self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:
                    # HTTP upstream: forward CONNECT with auth injected, relay
                    # the gateway's own 2xx reply to the browser
                    authed = self._inject_auth_header(first)
                    upstream.sendall(authed)
                    reply = self._wait_http_connect_reply(upstream)
                    self.request.sendall(reply)
                self._relay(self.request, upstream)
            else:
                # --- plain HTTP: forward the full request with auth injected ---
                upstream = self._connect_upstream()
                upstream.sendall(self._inject_auth_header(first))
                self._relay(self.request, upstream)
        except OSError:
            pass
        finally:
            try:
                self.request.close()
            except OSError:
                pass

    @staticmethod
    def _wait_http_connect_reply(upstream: socket.socket, timeout: float = 20.0) -> bytes:
        """Read the upstream HTTP proxy's CONNECT reply (up to the blank line)."""
        upstream.settimeout(timeout)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = upstream.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf or b"HTTP/1.1 502 Bad Gateway\r\n\r\n"


class LocalAuthProxyBridge:
    """Run a local no-auth HTTP proxy that forwards to an authed upstream.

    Use for SOCKS5-with-auth upstreams (Firefox can't authenticate to SOCKS5)
    or HTTP upstreams behind DataDome-style rulesets. DNS for CONNECT is
    resolved at the gateway (hostname-based SOCKS5 ATYP=0x03).
    """

    def __init__(self, proxy_url: str):
        p = urlsplit(proxy_url.strip())
        scheme = (p.scheme or "http").lower()
        if scheme in ("socks", "socks5", "socks5h"):
            scheme = "socks5"
        if not p.hostname:
            raise ProxyBridgeError(f"invalid proxy url for bridge: {proxy_url}")
        self._upstream = {
            "host": p.hostname,
            "port": p.port or (1080 if scheme == "socks5" else 8080),
            "user": p.username or "",
            "pass": p.password or "",
            "socks": scheme == "socks5",
        }
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self.port: Optional[int] = None

    def start(self) -> int:
        # bind the upstream config onto the handler CLASS so every accepted
        # connection sees it without per-connection plumbing
        handler = type(
            "BoundBridgeHandler", (_AuthBridgeHandler,), {"upstream": self._upstream}
        )
        for attempt in range(20):
            candidate = 20000 + (os.getpid() % 10000) + attempt * 7
            try:
                self._server = socketserver.ThreadingTCPServer(
                    ("127.0.0.1", candidate), handler
                )
                self._server.daemon_threads = True
                self.port = candidate
                threading.Thread(target=self._server.serve_forever, daemon=True).start()
                return candidate
            except OSError:
                continue
        raise ProxyBridgeError("local auth proxy bridge: no free port found")

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def browser_proxy(self) -> dict:
        """Playwright proxy dict pointing at the local bridge (no auth)."""
        return {"server": f"http://127.0.0.1:{self.port}"}
