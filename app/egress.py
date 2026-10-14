"""Connection-pinned HTTP proxy for tenant capture and webhook traffic.

The browser never resolves target hosts or opens target sockets itself. Each
proxy connection validates every DNS answer, then connects to a numeric address
from that validated set. Private-network grants come only from server settings.
Configured OCR/embedding services use their own operator-owned clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from typing import Iterable, Any
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.settings import Settings
import httpcore


class EgressDenied(ValueError):
    """A safe, destination-free reason for refusing an outbound connection."""


@dataclass(frozen=True)
class NetworkPolicy:
    private_cidrs: tuple[str, ...] = ()

    def allows(self, address: str) -> bool:
        if "%" in address:
            return False
        ip = ipaddress.ip_address(address)
        # Metadata, scoped/link-local and transition addresses are never granted.
        if (
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or str(ip) in {"168.63.129.16", "100.100.100.200", "fd00:ec2::254"}
        ):
            return False
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip.ipv4_mapped is not None
            or ip.sixtofour is not None
            or ip.teredo is not None
            or ip in ipaddress.ip_network("64:ff9b::/96")
            or ip in ipaddress.ip_network("64:ff9b:1::/48")
        ):
            return False
        if ip.is_global:
            return True
        return any(ip in ipaddress.ip_network(cidr) for cidr in self.private_cidrs)

    def summary(self) -> dict[str, object]:
        return {
            "mode": "public-plus-server-grants" if self.private_cidrs else "public-only",
            "grant_count": len(self.private_cidrs),
            "policy_sha256": hashlib.sha256(json.dumps(self.private_cidrs).encode()).hexdigest(),
        }


def network_policy(settings: Settings, *, owner: str | None) -> NetworkPolicy:
    trusted = (
        owner in settings.NETWORK_TRUSTED_OWNERS
        if owner
        else (not settings.REQUIRE_API_KEY and settings.NETWORK_TRUST_LOCAL_OPERATOR)
    )
    return NetworkPolicy(settings.NETWORK_PRIVATE_CIDRS if trusted else ())


def validate_network_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https", "ws", "wss"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "%" in parts.hostname
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
    ):
        raise EgressDenied("invalid-url-or-embedded-credentials")
    if parts.port is not None and not 0 < parts.port < 65536:
        raise EgressDenied("invalid-port")


async def resolve_addresses(host: str, port: int, policy: NetworkPolicy) -> list[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise EgressDenied("non-public-destination-without-server-grant")
    addresses = []
    for family, _, _, _, destination in answers:
        address = destination[0]
        if family not in {socket.AF_INET, socket.AF_INET6} or not isinstance(address, str):
            raise EgressDenied("unsupported-address-family")
        if not policy.allows(address):
            raise EgressDenied("non-public-destination-without-server-grant")
        if address not in addresses:
            addresses.append(address)
    return addresses


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """HTTPcore backend preserving original HTTP Host/TLS SNI with pinned TCP.

    This uses HTTPcore's public network-backend interface directly. Webhooks
    never depend on a proxy client's serialization of IPv6 authorities.
    """

    def __init__(self, policy: NetworkPolicy):
        self.policy = policy
        backend = httpcore.AnyIOBackend()
        if not isinstance(backend, httpcore.AsyncNetworkBackend):
            raise RuntimeError("The declared HTTPcore asyncio backend is unavailable")
        self.backend: httpcore.AsyncNetworkBackend = backend
        self.opened = 0
        self.denials: dict[str, int] = {}

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout if timeout is not None else 15):
                addresses = await resolve_addresses(host, port, self.policy)
                for address in addresses:
                    try:
                        stream = await self.backend.connect_tcp(
                            address,
                            port,
                            timeout=timeout,
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except httpcore.ConnectError:
                        continue
                    peer = stream.get_extra_info("server_addr")
                    if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(address):
                        await stream.aclose()
                        raise EgressDenied("connection-peer-mismatch")
                    self.opened += 1
                    return stream
                raise httpcore.ConnectError("No validated destination accepted the connection")
        except EgressDenied as error:
            self.denials[str(error)] = self.denials.get(str(error), 0) + 1
            raise

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def summary(self) -> dict[str, object]:
        return {
            **self.policy.summary(),
            "connections_opened": self.opened,
            "denials": dict(self.denials),
        }


class EgressProxy:
    """Ephemeral fixed proxy; no DIRECT fallback, DNS cache or tenant grant input."""

    def __init__(self, policy: NetworkPolicy):
        self.policy = policy
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.opened = 0
        self.denials: dict[str, int] = {}
        self.closing = False

    async def __aenter__(self) -> EgressProxy:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=65536)
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closing = True
        if self.server is not None:
            self.server.close()
        # Python3.13 wait_closed waits for client transports as well. Close
        # their handlers BEFORE waiting, or open relay/header reads deadlock
        # shutdown against the very cancellation that would release them.
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        if self.server is None or not self.server.is_serving():
            raise RuntimeError("Egress proxy is not running")
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def summary(self) -> dict[str, object]:
        return {
            **self.policy.summary(),
            "connections_opened": self.opened,
            "denials": dict(self.denials),
        }

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closing or len(self.tasks) >= 64:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        # All answers are checked BEFORE any connection. A mixed answer set is
        # denied as a whole, even when the first/public answer would connect.
        addresses = await resolve_addresses(host, port, self.policy)
        for address in addresses:
            family = (
                socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            )
            connection = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            connection.setblocking(False)
            try:
                # Numeric destination prevents a second resolution/rebinding.
                await loop.sock_connect(connection, (address, port))
                if ipaddress.ip_address(connection.getpeername()[0]) != ipaddress.ip_address(
                    address
                ):
                    raise EgressDenied("connection-peer-mismatch")
                streams = await asyncio.open_connection(sock=connection)
                self.opened += 1
                return streams
            except BaseException as error:
                connection.close()
                if not isinstance(error, OSError):
                    raise
        raise OSError("No validated destination accepted the connection")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        established = False
        try:
            async with asyncio.timeout(15):
                header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                lines = header.split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"} or not re.fullmatch(r"[A-Z]+", method):
                    raise EgressDenied("invalid-proxy-request")
                tunnel = method == "CONNECT"
                url = f"https://{target}" if tunnel else target
                validate_network_url(url)
                parts = urlsplit(url)
                if (
                    parts.hostname is None
                    or parts.fragment
                    or (tunnel and (parts.path or parts.query or parts.port is None))
                ):
                    raise EgressDenied("invalid-proxy-target")
                port = parts.port or (443 if parts.scheme in {"https", "wss"} else 80)
                headers = []
                hosts = []
                upgrade = any(line.lower().startswith("upgrade:") for line in lines[1:-2])
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name):
                        raise EgressDenied("invalid-proxy-header")
                    if name.lower() == "host":
                        hosts.append(value.strip())
                    if name.lower() not in {"proxy-authorization", "proxy-connection"} and (
                        upgrade or name.lower() != "connection"
                    ):
                        headers.append(line)
                if hosts != [parts.netloc]:
                    raise EgressDenied("proxy-host-mismatch")
                upstream_reader, upstream = await self._connect(parts.hostname, port)
                if tunnel:
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    if not upgrade:
                        headers.append("Connection: close")
                    path = parts.path or "/"
                    if parts.query:
                        path += "?" + parts.query
                    upstream.write(
                        (
                            f"{method} {path} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n"
                        ).encode("ascii")
                    )
                    await upstream.drain()
                established = True
            async with asyncio.timeout(120):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._pipe(reader, upstream))
                    tasks.create_task(self._pipe(upstream_reader, writer))
        except (
            ValueError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as error:
            reason = (
                str(error) if isinstance(error, EgressDenied) else "invalid-or-failed-connection"
            )
            self.denials[reason] = self.denials.get(reason, 0) + 1
            if not established:
                body = b"Network policy denied."
                writer.write(
                    (
                        f"HTTP/1.1 403 Forbidden\r\nX-MDWB-Egress-Denied: blocked-connection\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + body
                )
                with suppress(OSError):
                    await writer.drain()
        except ExceptionGroup as error:
            if not all(isinstance(item, OSError) for item in error.exceptions):
                raise
            reason = "relay-connection-failed"
            self.denials[reason] = self.denials.get(reason, 0) + 1
        finally:
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
