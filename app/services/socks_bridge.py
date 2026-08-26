"""Playwright / Chromium 不支持带账密的 SOCKS5。

本机 OAuth 脚本已经用 C# Team48SocksBridge 绕过；
服务器上的自动注册 / 自动授权在这里起一座等价的本地无认证桥。
"""
from __future__ import annotations

import logging
import socket
import threading
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlparse

from app.services.sms import chrome_proxy_config

logger = logging.getLogger(__name__)

LOOPBACK_BYPASS = "localhost,127.0.0.1,::1"
_CMD_UNSUP = bytes([5, 7, 0, 1, 0, 0, 0, 0, 0, 0])
_OK_LOOPBACK = bytes([5, 0, 0, 1, 127, 0, 0, 1, 0, 0])


def needs_socks_auth_bridge(config: dict[str, str]) -> bool:
    server = str(config.get("server") or "").lower()
    if not server.startswith("socks5"):
        return False
    return bool(config.get("username") or config.get("password"))


class SocksAuthBridge:
    """127.0.0.1 上的无认证 SOCKS5，连上游时补上用户名密码。"""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        user = (username or "").encode("utf-8")
        passwd = (password or "").encode("utf-8")
        if len(user) > 255 or len(passwd) > 255:
            raise ValueError("proxy auth too long")
        if not host or not port:
            raise ValueError("浏览器代理缺少 host/port")
        self._up_host = host
        self._up_port = int(port)
        self._user = user
        self._pass = passwd
        self._stop = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._listener.settimeout(0.5)
        self._port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, name="team48-socks-bridge", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._port

    def close(self) -> None:
        self._stop = True
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop:
                    break
                continue
            if self._stop:
                _close_sock(client)
                break
            worker = threading.Thread(target=self._serve, args=(client,), daemon=True)
            worker.start()

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(30)
            if _recv_exact(client, 1) != b"\x05":
                return
            n_methods = _recv_exact(client, 1)[0]
            if n_methods < 1:
                return
            _recv_exact(client, n_methods)
            client.sendall(b"\x05\x00")

            head = _recv_exact(client, 4)
            if head[0] != 5 or head[1] != 1:
                client.sendall(_CMD_UNSUP)
                return
            addr = _read_addr(client, head[3])
            if addr is None:
                return
            portb = _recv_exact(client, 2)
            dest_port = (portb[0] << 8) | portb[1]
            if _is_loopback(head[3], addr):
                upstream = socket.create_connection(("127.0.0.1", dest_port), timeout=30)
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.sendall(_OK_LOOPBACK)
                _pump_both(client, upstream)
                return

            upstream = socket.create_connection((self._up_host, self._up_port), timeout=30)
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream.settimeout(30)
            _auth_upstream(upstream, self._user, self._pass)
            upstream.sendall(head + addr + portb)
            reply_head = _recv_exact(upstream, 4)
            reply_addr = _read_addr(upstream, reply_head[3])
            if reply_addr is None:
                return
            reply_port = _recv_exact(upstream, 2)
            client.sendall(reply_head + reply_addr + reply_port)
            _pump_both(client, upstream)
        except Exception:  # noqa: BLE001
            logger.debug("socks bridge session failed", exc_info=True)
        finally:
            _close_sock(client)
            _close_sock(upstream)


def _auth_upstream(upstream: socket.socket, user: bytes, password: bytes) -> None:
    upstream.sendall(b"\x05\x01\x02")
    greet = _recv_exact(upstream, 2)
    if greet[0] != 5 or greet[1] != 2:
        raise RuntimeError("upstream socks needs user/pass")
    auth = bytes([1, len(user)]) + user + bytes([len(password)]) + password
    upstream.sendall(auth)
    reply = _recv_exact(upstream, 2)
    if reply[1] != 0:
        raise RuntimeError("upstream socks auth failed")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf.extend(chunk)
    return bytes(buf)


def _read_addr(sock: socket.socket, atyp: int) -> bytes | None:
    if atyp == 1:
        return _recv_exact(sock, 4)
    if atyp == 3:
        length = _recv_exact(sock, 1)[0]
        if length < 1:
            return None
        return bytes([length]) + _recv_exact(sock, length)
    if atyp == 4:
        return _recv_exact(sock, 16)
    return None


def _is_loopback(atyp: int, addr: bytes) -> bool:
    if atyp == 1:
        return len(addr) >= 4 and addr[0] == 127
    if atyp == 4:
        return len(addr) >= 16 and addr[:15] == b"\x00" * 15 and addr[15] == 1
    if atyp == 3 and len(addr) > 1:
        name = addr[1 : 1 + addr[0]].decode("ascii", errors="ignore").strip().lower()
        return name in {"localhost", "127.0.0.1", "::1"}
    return False


def _pump_both(left: socket.socket, right: socket.socket) -> None:
    left.settimeout(None)
    right.settimeout(None)
    worker = threading.Thread(target=_pump, args=(left, right), daemon=True)
    worker.start()
    _pump(right, left)


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(8192)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass


def _close_sock(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


@contextmanager
def chrome_proxy_launch(proxy: str) -> Iterator[dict[str, str]]:
    """给 Playwright 用的代理配置。带账密的 SOCKS5 会先转成本地无认证端口。"""
    config = dict(chrome_proxy_config(proxy))
    config["bypass"] = LOOPBACK_BYPASS
    if not needs_socks_auth_bridge(config):
        yield config
        return

    parsed = urlparse(config["server"])
    host = parsed.hostname or ""
    port = parsed.port
    if not host or not port:
        raise ValueError("浏览器代理缺少 host/port")
    bridge = SocksAuthBridge(host, port, config.get("username") or "", config.get("password") or "")
    try:
        logger.info("SOCKS5 账密已改走本地桥 127.0.0.1:%s", bridge.port)
        yield {"server": f"socks5://127.0.0.1:{bridge.port}", "bypass": LOOPBACK_BYPASS}
    finally:
        bridge.close()
