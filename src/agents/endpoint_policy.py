"""AUD-03: which AI endpoint addresses are allowed.

Rule (decision D2):
  - the scheme must be http or https;
  - http only for loopback and private-LAN hosts, by IP literal or the name
    "localhost" (127.0.0.0/8, ::1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16);
    no DNS lookup is ever made, so any other host name over http is refused;
  - a key is never sent over http, not even to loopback or the LAN.

Messages name the endpoint (or its host) and never contain the key.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_HTTP_OK_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))

_NO_KEY = ("", "not-needed")


class EndpointPolicyError(ValueError):
    """An endpoint address or key use that the rule does not allow."""


def has_key(api_key) -> bool:
    """True for a real key; the empty and "not-needed" placeholders are not."""
    return bool(api_key) and str(api_key).strip() not in _NO_KEY


def _parts(url: str):
    try:
        u = urlsplit(str(url or "").strip())
        return (u.scheme or "").lower(), (u.hostname or "")
    except ValueError:
        return "", ""


def is_local_or_lan_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False          # a host name: never resolved, never trusted over http
    return any(ip in net for net in _HTTP_OK_NETS if ip.version == net.version)


def _label(url: str, name: str) -> str:
    if name:
        return f"endpoint '{name}'"
    host = _parts(url)[1]
    return f"the endpoint at {host}" if host else "this endpoint"


def check(url: str, api_key=None, name: str = "") -> str:
    """The one rule, used by both Settings save and every send site: "" when
    the address and key use are allowed, else a plain-words reason that says
    what to change. Never contains the key or any user:pass part of the URL."""
    scheme, host = _parts(url)
    who = _label(url, name)
    if scheme not in ("http", "https"):
        return f"{who}: the address must start with http:// or https://. Change it in Settings."
    if not host:
        return f"{who}: the address has no host name. Change it in Settings."
    if scheme == "http" and not is_local_or_lan_host(host):
        return (f"{who}: plain http is allowed only for this computer (localhost, 127.x) "
                f"and private-network addresses (10.x, 172.16-31.x, 192.168.x). "
                f"Change the address in Settings to https:// or to a local or "
                f"private-network address.")
    if scheme == "http" and has_key(api_key):
        return (f"{who}: an API key is never sent over plain http, even to this "
                f"computer or the local network. Change the address in Settings "
                f"to https://, or remove the key.")
    return ""


def check_save(url: str, api_key, name: str = "") -> str:
    """Save-time check: same rule as check_send."""
    return check(url, api_key, name)


def check_send(url: str, api_key, name: str = "") -> None:
    """Send-time guard: the same rule as check_save. Raises EndpointPolicyError
    and nothing is sent. Catches configs saved before the save-time check."""
    msg = check(url, api_key, name)
    if msg:
        raise EndpointPolicyError(f"{msg} Nothing was sent.")
