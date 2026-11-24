"""Local relay for proxies with a login and password.

Chromium takes only ``--proxy-server=scheme://host:port``: credentials in the address are ignored and
the login dialog would stop the agent. For an authenticated proxy the agent starts this relay on
127.0.0.1 with a random port; Chromium uses it as a plain HTTP proxy and the relay forwards every
connection to the upstream proxy with the credentials:

* HTTP/HTTPS upstream — ``CONNECT`` and absolute-URI requests get a ``Proxy-Authorization: Basic`` header;
* SOCKS5 upstream — username/password authentication (RFC 1929), the target hostname is resolved
  by the proxy, so DNS requests do not leak past it.

The relay listens on the loopback interface only and lives as long as the browser of one phrase.
"""
import base64
import ipaddress
import logging
import select
import socket
import socketserver
import ssl
import struct
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
HEADER_LIMIT = 64 * 1024
CONNECT_TIMEOUT = 20
HOP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


class RelayError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def read_head(sock):
    """Bytes up to and including the blank line that ends HTTP headers, and the rest that was read."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RelayError(502, "соединение закрыто до конца заголовков")
        data += chunk
        if len(data) > HEADER_LIMIT:
            raise RelayError(431, "слишком длинные заголовки")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("iso-8859-1"), rest


def pipe(left, right):
    """Copy bytes both ways until either side closes."""
    sockets = [left, right]
    try:
        while True:
            # TLS sockets can hold decrypted bytes that select() does not see.
            readable = [sock for sock in sockets if isinstance(sock, ssl.SSLSocket) and sock.pending()]
            if not readable:
                readable, _, failed = select.select(sockets, [], sockets, 300)
                if failed or not readable:
                    return
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (right if sock is left else left).sendall(data)
    except OSError:
        return


def basic_auth(proxy):
    token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode("ascii")
    return f"Basic {token}"


def split_target(target, default_port):
    """host, port of «host:port» or «[v6]:port»."""
    parts = urlsplit(f"//{target}")
    try:
        port = parts.port or default_port
    except ValueError:
        raise RelayError(400, "некорректный порт") from None
    if not parts.hostname:
        raise RelayError(400, "не указан хост")
    return parts.hostname, port


def open_upstream(proxy):
    sock = socket.create_connection((proxy.host, proxy.port), timeout=CONNECT_TIMEOUT)
    if proxy.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=proxy.host)
    return sock


def socks5_connect(proxy, host, port):
    """TCP connection to host:port through a SOCKS5 proxy with username/password authentication."""
    sock = open_upstream(proxy)
    try:
        methods = b"\x02" if proxy.username else b"\x00"
        sock.sendall(b"\x05\x01" + methods)
        reply = recv_exact(sock, 2)
        if reply[0] != 5 or reply[1] == 0xFF:
            raise RelayError(502, "SOCKS5-прокси не принял способ авторизации")
        if reply[1] == 2:
            user, password = proxy.username.encode(), proxy.password.encode()
            sock.sendall(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            if recv_exact(sock, 2)[1] != 0:
                raise RelayError(407, "SOCKS5-прокси отклонил логин или пароль")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            name = host.encode("idna")
            request = b"\x03" + bytes([len(name)]) + name
        else:
            request = (b"\x01" if address.version == 4 else b"\x04") + address.packed
        sock.sendall(b"\x05\x01\x00" + request + struct.pack("!H", port))
        reply = recv_exact(sock, 4)
        if reply[1] != 0:
            raise RelayError(502, f"SOCKS5-прокси не открыл соединение (код {reply[1]})")
        bound = {1: 4, 4: 16}.get(reply[3])
        if bound is None:
            bound = recv_exact(sock, 1)[0]
        recv_exact(sock, bound + 2)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RelayError(502, "прокси закрыл соединение")
        data += chunk
    return data


def http_connect(proxy, host, port):
    """Tunnel to host:port through an HTTP(S) proxy with Basic authentication."""
    sock = open_upstream(proxy)
    try:
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        sock.sendall((
            f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
            f"Proxy-Authorization: {basic_auth(proxy)}\r\n\r\n"
        ).encode("iso-8859-1"))
        head, rest = read_head(sock)
        status = head.split(" ", 2)
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 502
        if code != 200:
            raise RelayError(code, f"прокси ответил {code}")
        if rest:
            raise RelayError(502, "прокси прислал данные до установки туннеля")
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(CONNECT_TIMEOUT)
        proxy = self.server.upstream
        try:
            head, rest = read_head(client)
            request_line, *header_lines = head.split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = split_target(target, 443)
                upstream = self.tunnel(proxy, host, port)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                upstream = self.forward(proxy, method, target, version, header_lines, rest)
        except (RelayError, OSError, ValueError) as error:
            status = getattr(error, "status", 502)
            log.debug("Прокси-релей: %s", error)
            try:
                client.sendall(f"HTTP/1.1 {status} Proxy Relay Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            except OSError:
                pass
            return
        client.settimeout(None)
        with upstream:
            pipe(client, upstream)

    @staticmethod
    def tunnel(proxy, host, port):
        if proxy.scheme == "socks5":
            return socks5_connect(proxy, host, port)
        return http_connect(proxy, host, port)

    @staticmethod
    def forward(proxy, method, target, version, header_lines, rest):
        """Plain-HTTP request: one request per connection, so that every request carries the credentials."""
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:
            raise RelayError(400, "ожидается абсолютный http-адрес")
        headers = [line for line in header_lines if line.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
        headers.append("Connection: close")
        if proxy.scheme == "socks5":
            upstream = socks5_connect(proxy, url.hostname, url.port or 80)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            line = f"{method} {path} {version}"
        else:
            upstream = open_upstream(proxy)
            upstream.settimeout(None)
            headers.append(f"Proxy-Authorization: {basic_auth(proxy)}")
            line = f"{method} {target} {version}"
        upstream.sendall(("\r\n".join([line, *headers]) + "\r\n\r\n").encode("iso-8859-1") + rest)
        return upstream


class RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyRelay:
    """Loopback HTTP proxy that forwards to an authenticated upstream proxy."""

    def __init__(self, upstream):
        self.server = RelayServer(("127.0.0.1", 0), RelayHandler)
        self.server.upstream = upstream
        self.thread = threading.Thread(target=self.server.serve_forever, name="proxy-relay", daemon=True)

    @property
    def address(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
