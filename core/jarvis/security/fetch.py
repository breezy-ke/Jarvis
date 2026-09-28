"""Reading the web safely: feeds, articles and research pages.

Everything Jarvis fetches from the web on its own goes through `SafeFetcher`:

* **Public addresses only**, over http or https. A link planted in a feed or a
  search result can't make Jarvis reach its own services on the internal
  Docker network (the database, the speech server, Ollama), your router, your
  Tailscale devices or a cloud metadata address.
* **One lookup.** The name is resolved once, every address it has is checked,
  and the connection goes to the checked address, so a second lookup can't be
  answered differently. HTTPS still verifies the certificate for the real name.
* **Redirects** are followed by hand, at most five, each checked the same way.
* **Limits:** answers are capped in size and time.
* **robots.txt** is honoured for pages (`RobotsCache`); feeds and APIs are
  published to be read by programs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin

import httpx

log = logging.getLogger("jarvis.fetch")

USER_AGENT = "JarvisPersonalAssistant/0.2 (+a personal assistant reading on its owner's behalf)"
ROBOTS_AGENT = "JarvisPersonalAssistant"
MAX_REDIRECTS = 5

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(RuntimeError):
    """The page couldn't be read (network, status, size or time)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FetchRefused(FetchError):
    """Jarvis won't read this address (not http(s), or not a public address)."""


@dataclass(frozen=True)
class Fetched:
    url: str  # after redirects
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def text(self) -> str:
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip("\"' ")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public(address: str) -> bool:
    """A globally routable unicast address: not private, loopback, link-local,
    shared (CGNAT and Tailscale), multicast or reserved."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class SafeFetcher:
    """GET public web pages within limits. See the module docstring for the rules.

    `allow_private` exists for the test suite's fake web server on 127.0.0.1;
    the settings refuse it in production.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
        allow_private: bool = False,
        timeout: float = 15.0,
        per_host_interval: float = 1.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # a proxy would resolve names itself, defeating the check
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={"User-Agent": USER_AGENT},
        )
        self._resolve = resolver
        self._allow_private = allow_private
        self._timeout = timeout
        self._interval = per_host_interval
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_at: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(
        self,
        url: str,
        *,
        max_bytes: int = 2_000_000,
        headers: dict[str, str] | None = None,
        accept: str = "*/*",
        pace: float | None = None,
    ) -> Fetched:
        """Fetch `url`, following checked redirects. 304 comes back as `not_modified`.

        `pace` is the gap between requests to the same site (APIs may go faster
        than the polite default for pages).
        """
        current = httpx.URL(url)
        extra = {"Accept": accept, **(headers or {})}
        try:
            async with asyncio.timeout(self._timeout * 2):
                for _ in range(MAX_REDIRECTS + 1):
                    response = await self._one(current, extra, max_bytes, pace)
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(
                                "A redirect without a destination", status=response.status
                            )
                        current = httpx.URL(urljoin(str(current), location))
                        extra.pop("If-None-Match", None)  # validators belong to the first URL
                        extra.pop("If-Modified-Since", None)
                        continue
                    if response.status == 304 or 200 <= response.status < 300:
                        return response
                    raise FetchError(
                        f"{current.host} answered {response.status}", status=response.status
                    )
        except TimeoutError as exc:
            raise FetchError(f"{current.host} took too long") from exc
        raise FetchError(f"{url} redirects too many times")

    async def _checked_address(self, url: httpx.URL) -> str:
        if url.scheme not in ("http", "https") or not url.host:
            raise FetchRefused(f"Jarvis only reads http(s) addresses, not {url.scheme or '?'}:")
        if url.username or url.password:
            raise FetchRefused("Jarvis doesn't read addresses with a user name in them")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = await self._resolve(url.host, port)
        except OSError as exc:
            raise FetchError(f"Couldn't find {url.host}") from exc
        if not addresses:
            raise FetchError(f"Couldn't find {url.host}")
        if not self._allow_private and not all(is_public(a) for a in addresses):
            raise FetchRefused(f"{url.host} isn't a public address")
        return addresses[0]

    async def _one(
        self, url: httpx.URL, headers: dict[str, str], max_bytes: int, pace: float | None
    ) -> Fetched:
        address = await self._checked_address(url)
        lock = self._host_locks.setdefault(url.host, asyncio.Lock())
        async with lock:  # one request at a time per site, a polite gap between them
            gap = self._interval if pace is None else pace
            wait = self._last_at.get(url.host, 0.0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._send(url, address, headers, max_bytes)
            finally:
                self._last_at[url.host] = time.monotonic()

    async def _send(
        self, url: httpx.URL, address: str, headers: dict[str, str], max_bytes: int
    ) -> Fetched:
        pinned = url.copy_with(host=address)
        host_header = url.host if url.port is None else f"{url.host}:{url.port}"
        extensions = {"sni_hostname": url.host} if url.scheme == "https" else {}
        request = self._http.build_request(
            "GET", pinned, headers={**headers, "Host": host_header}, extensions=extensions
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise FetchError(f"Couldn't reach {url.host}: {type(exc).__name__}") from exc
        try:
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise FetchError(f"{url.host} sent more than {max_bytes} bytes")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FetchError(f"{url.host} sent more than {max_bytes} bytes")
        except httpx.HTTPError as exc:
            raise FetchError(f"Lost the connection to {url.host}") from exc
        finally:
            await response.aclose()
        return Fetched(
            url=str(url),
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=bytes(body),
            headers={k.lower(): v for k, v in response.headers.items()},
        )


class RobotsCache:
    """robots.txt for each site, fetched through the same checks and kept a day."""

    def __init__(self, fetcher: SafeFetcher, *, max_age: float = 86_400.0) -> None:
        self._fetcher = fetcher
        self._max_age = max_age
        self._rules: dict[str, tuple[float, robotparser.RobotFileParser | None]] = {}

    async def allowed(self, url: str) -> bool:
        parsed = httpx.URL(url)
        origin = f"{parsed.scheme}://{parsed.netloc.decode()}"
        cached = self._rules.get(origin)
        if cached is None or time.monotonic() - cached[0] > self._max_age:
            cached = (time.monotonic(), await self._load(origin))
            self._rules[origin] = cached
        rules = cached[1]
        return True if rules is None else rules.can_fetch(ROBOTS_AGENT, url)

    async def _load(self, origin: str) -> robotparser.RobotFileParser | None:
        rules = robotparser.RobotFileParser()
        try:
            fetched = await self._fetcher.get(f"{origin}/robots.txt", max_bytes=500_000)
        except FetchError as exc:
            if exc.status in (401, 403):  # the site says keep out
                rules.parse(["User-agent: *", "Disallow: /"])
                return rules
            return None  # no robots.txt (404) or unreachable: nothing forbids it
        rules.parse(fetched.text().splitlines())
        return rules
