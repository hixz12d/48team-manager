import socket
import threading
import unittest

from app.services.sms import chrome_proxy_config
from app.services.socks_bridge import (
    LOOPBACK_BYPASS,
    SocksAuthBridge,
    chrome_proxy_launch,
    needs_socks_auth_bridge,
)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf.extend(chunk)
    return bytes(buf)


class _FakeSocksAuthServer:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.auths: list[tuple[str, str]] = []
        self.connects: list[tuple[str, int]] = []
        self._stop = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            if _recv_exact(client, 1) != b"\x05":
                return
            n_methods = _recv_exact(client, 1)[0]
            _recv_exact(client, n_methods)
            client.sendall(b"\x05\x02")
            auth = _recv_exact(client, 2)
            user = _recv_exact(client, auth[1]).decode("utf-8")
            plen = _recv_exact(client, 1)[0]
            password = _recv_exact(client, plen).decode("utf-8")
            self.auths.append((user, password))
            if user != self.username or password != self.password:
                client.sendall(b"\x01\x01")
                return
            client.sendall(b"\x01\x00")
            head = _recv_exact(client, 4)
            if head[3] == 3:
                length = _recv_exact(client, 1)[0]
                host = _recv_exact(client, length).decode("ascii")
            elif head[3] == 1:
                raw = _recv_exact(client, 4)
                host = socket.inet_ntoa(raw)
            else:
                return
            dest_port = int.from_bytes(_recv_exact(client, 2), "big")
            self.connects.append((host, dest_port))
            client.sendall(bytes([5, 0, 0, 1, 127, 0, 0, 1, 0, 0]))
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                client.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass


class _EchoServer:
    def __init__(self) -> None:
        self._stop = False
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                client.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass


def _socks_noauth_connect(bridge_port: int, host: str, port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", bridge_port), timeout=5)
    try:
        sock.settimeout(5)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise AssertionError("bridge should accept no-auth clients")
        req = b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode("ascii") + port.to_bytes(2, "big")
        sock.sendall(req)
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise ConnectionError(f"connect failed: {reply[1]}")
        if reply[3] == 1:
            _recv_exact(sock, 6)
        elif reply[3] == 3:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length + 2)
        elif reply[3] == 4:
            _recv_exact(sock, 18)
        return sock
    except Exception:
        sock.close()
        raise


class ChromeProxyLaunchTests(unittest.TestCase):
    def test_http_auth_stays_native(self):
        with chrome_proxy_launch("http://user:secret@10.0.0.8:8000") as config:
            self.assertEqual(config["server"], "http://10.0.0.8:8000")
            self.assertEqual(config["username"], "user")
            self.assertEqual(config["password"], "secret")
            self.assertEqual(config["bypass"], LOOPBACK_BYPASS)
        self.assertFalse(needs_socks_auth_bridge(chrome_proxy_config("http://user:secret@10.0.0.8:8000")))

    def test_socks_without_auth_does_not_bridge(self):
        with chrome_proxy_launch("socks5h://127.0.0.1:1080") as config:
            self.assertEqual(config["server"], "socks5://127.0.0.1:1080")
            self.assertNotIn("username", config)
            self.assertEqual(config["bypass"], LOOPBACK_BYPASS)

    def test_socks_with_auth_uses_local_bridge(self):
        with chrome_proxy_launch("socks5h://user:secret@10.0.0.8:1080") as config:
            self.assertTrue(config["server"].startswith("socks5://127.0.0.1:"))
            self.assertNotIn("username", config)
            self.assertNotIn("password", config)
            self.assertEqual(config["bypass"], LOOPBACK_BYPASS)
            port = int(config["server"].rsplit(":", 1)[1])
            self.assertGreater(port, 0)


class SocksAuthBridgeTests(unittest.TestCase):
    def test_forwards_after_upstream_auth(self):
        upstream = _FakeSocksAuthServer("user", "secret")
        bridge = SocksAuthBridge("127.0.0.1", upstream.port, "user", "secret")
        try:
            sock = _socks_noauth_connect(bridge.port, "chatgpt.com", 443)
            try:
                sock.sendall(b"ping")
                self.assertEqual(sock.recv(16), b"ping")
            finally:
                sock.close()
            self.assertEqual(upstream.auths, [("user", "secret")])
            self.assertEqual(upstream.connects, [("chatgpt.com", 443)])
        finally:
            bridge.close()
            upstream.close()

    def test_loopback_bypasses_upstream(self):
        echo = _EchoServer()
        upstream = _FakeSocksAuthServer("user", "secret")
        bridge = SocksAuthBridge("127.0.0.1", upstream.port, "user", "secret")
        try:
            sock = _socks_noauth_connect(bridge.port, "127.0.0.1", echo.port)
            try:
                sock.sendall(b"local")
                self.assertEqual(sock.recv(16), b"local")
            finally:
                sock.close()
            self.assertEqual(upstream.auths, [])
            self.assertEqual(upstream.connects, [])
        finally:
            bridge.close()
            upstream.close()
            echo.close()

    def test_bad_password_does_not_open_tunnel(self):
        upstream = _FakeSocksAuthServer("user", "secret")
        bridge = SocksAuthBridge("127.0.0.1", upstream.port, "user", "wrong")
        try:
            with self.assertRaises(ConnectionError):
                _socks_noauth_connect(bridge.port, "chatgpt.com", 443)
            self.assertEqual(upstream.auths, [("user", "wrong")])
        finally:
            bridge.close()
            upstream.close()


if __name__ == "__main__":
    unittest.main()
