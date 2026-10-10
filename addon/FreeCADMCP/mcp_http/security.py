"""Request admission for the HTTP MCP endpoint.

Reuses the policy the XML-RPC endpoint already applies -- IP allowlist, bearer
token, and the browser guard -- but re-expressed for JSON-over-HTTP, since the
XML-RPC guard is hard-coded to ``text/xml``.

Free of FreeCAD imports: the XML-RPC guard logs through ``FreeCAD.Console``,
this one reports back to its caller so the HTTP handler can log however it
likes.
"""

import base64
import hmac
import ipaddress
from typing import Iterable

# A wildcard bind is not a name any client's Host header carries, so it cannot
# seed a default allowlist. See check_bind_host.
WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::"})

# Cap the request body. A negative Content-Length would make a blocking read
# consume to EOF and pin a worker thread until the socket timeout, answering
# nothing at all; an oversized one would buffer the whole body in memory.
MAX_REQUEST_BODY = 10 * 1024 * 1024

JSON_MEDIA_TYPES = frozenset({"application/json"})

ALLOWED = "ok"
DENIED = "denied"           # 403: this caller may not come here at all.
UNAUTHORIZED = "unauthorized"  # 401: maybe legitimate, but no valid credential.


class BindHostError(OSError):
    """Raised when a bind address makes the Host policy unusable."""


def check_bind_host(host: str) -> None:
    """Refuse a wildcard bind unless the caller supplies its own allowlist.

    Binding 0.0.0.0 serves every interface, but no client's Host header ever
    names 0.0.0.0, so a Host allowlist derived from the bind address is empty
    and every request -- including the LAN clients the bind was meant to serve
    -- would be refused with 403. Better to say so now than to serve a server
    that rejects all of its own clients.
    """
    if host.lower() in WILDCARD_BIND_HOSTS:
        raise BindHostError(
            f"MCP_HTTP_HOST={host} binds every interface, but no client's Host "
            f"header ever names a wildcard address, so every LAN client would "
            f"get a 403. Set MCP_HTTP_HOST to the concrete address clients will "
            f"dial (e.g. your LAN IP), or pass allowed_hosts explicitly to opt "
            f"into a wider policy."
        )


def hostname_of(host_header: str | None) -> str | None:
    """The host part of a Host header, without port or IPv6 brackets."""
    if not host_header:
        return None
    name = host_header.strip()
    if name.startswith("["):
        closing = name.find("]")
        return name[: closing + 1] if closing != -1 else name
    colon = name.rfind(":")
    if colon != -1 and name.count(":") == 1:
        name = name[:colon]
    return name.lower()


def _is_loopback_name(name: str | None) -> bool:
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def default_allowed_hosts(host: str, loopback_only: bool) -> tuple[str, ...]:
    """Host allowlist implied by the bind address."""
    if loopback_only:
        return ("localhost", "127.0.0.1", "::1")
    return (hostname_of(host) or host.lower(),)


def parse_allowed_ips(allowed: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse an allowlist string into networks. Bad entries are ignored.

    Mirrors the XML-RPC endpoint's behaviour: an unparseable entry must not
    take the server down, it just does not match anything.
    """
    networks = []
    for raw in (allowed or "").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def ip_allowed(client_ip: str, allowed: str) -> bool:
    """Whether ``client_ip`` falls inside the comma-separated allowlist."""
    networks = parse_allowed_ips(allowed)
    if not networks:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


def media_type_of(content_type: str | None) -> str:
    """The bare media type of a Content-Type header, lowercased."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def bearer_token_of(authorization: str | None) -> str:
    """The token in an ``Authorization: Bearer <token>`` header, else "".

    Returns "" for a missing header or any other scheme. The caller compares
    that "" against the real token in constant time, so a wrong scheme is
    handled the same as a wrong token.
    """
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def basic_token_of(authorization: str | None) -> str | None:
    """The password field of HTTP Basic auth, where the token may also sit.

    Accepted so stdlib clients can use ``http://:token@host:port`` URIs, the
    same affordance the XML-RPC endpoint offers. Returns None when the header
    is not Basic or does not decode.
    """
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except Exception:
        return None
    _, _, password = decoded.partition(":")
    return password


def presented_token(authorization: str | None) -> str:
    """The credential a request presents, whichever scheme it used."""
    token = bearer_token_of(authorization)
    if token:
        return token
    basic = basic_token_of(authorization)
    return basic if basic is not None else ""


def token_matches(authorization: str | None, expected: str) -> bool:
    """Constant-time comparison of the presented credential against ``expected``.

    hmac.compare_digest raises TypeError when a str argument contains a
    non-ASCII character. HTTP headers are decoded latin-1, so any byte >= 0x80
    in Authorization produces exactly that: without this guard an
    unauthenticated caller could crash the handler thread on every request and
    receive no response at all.
    """
    try:
        return hmac.compare_digest(presented_token(authorization), expected)
    except TypeError:
        return False


def classify(
    *,
    client_ip: str,
    host_header: str | None,
    origin_header: str | None,
    authorization: str | None,
    allowed_ips: str,
    allowed_hosts: Iterable[str],
    allowed_origins: Iterable[str] = (),
    auth_token: str | None = None,
    loopback_only: bool = True,
) -> tuple[str, str]:
    """Decide whether a request may proceed, and why not when it may not.

    Returns ``(verdict, reason)`` where verdict is one of ALLOWED, DENIED or
    UNAUTHORIZED. The three are kept distinct because they mean different
    things to a client: DENIED is a policy refusal the client cannot retry its
    way out of, UNAUTHORIZED says a credential would help, ALLOWED says go
    ahead. Collapsing DENIED into UNAUTHORIZED invites pointless retry loops
    against a server that will never accept them.
    """
    if not ip_allowed(client_ip, allowed_ips):
        return DENIED, f"client address {client_ip} is not in the allowed list"

    host = hostname_of(host_header)
    if host not in {name.lower() for name in allowed_hosts}:
        return DENIED, f"Host header {host_header!r} is not allowed"

    # Browsers attach Origin to every cross-origin request; native MCP clients
    # send none. Rejecting a present Origin blocks a web page driving the
    # endpoint through fetch(). Note this is not access control: anything
    # running on this machine can omit Origin entirely. That gap is what the
    # token below closes.
    if origin_header is not None and origin_header not in set(allowed_origins):
        return DENIED, "requests carrying an Origin header are not accepted"

    if auth_token and not token_matches(authorization, auth_token):
        return UNAUTHORIZED, "a valid bearer token is required"

    return ALLOWED, ""


def check_content_type(content_type: str | None) -> tuple[int, str] | None:
    """A ``(status, reason)`` refusal for a body we will not parse, or None."""
    if media_type_of(content_type) not in JSON_MEDIA_TYPES:
        return 415, "this endpoint only accepts Content-Type application/json"
    return None


def check_body_length(content_length: str | None) -> tuple[int, str] | None:
    """A ``(status, reason)`` refusal for an unusable Content-Length, or None.

    A missing length is accepted: the caller falls back to reading the body
    with the cap applied. A negative one is refused rather than trusted,
    because it would turn the read into an unbounded block on EOF.
    """
    if content_length is None or content_length == "":
        return None
    try:
        length = int(content_length)
    except ValueError:
        return 400, "Content-Length is not a number"
    if length < 0:
        return 400, "Content-Length must not be negative"
    if length > MAX_REQUEST_BODY:
        return 413, f"request body exceeds {MAX_REQUEST_BODY} bytes"
    return None