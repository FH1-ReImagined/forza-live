"""The visitor's address: X-Real-IP when the request comes from the website's nginx (a trusted
proxy, FORZA_ACCOUNTS_PROXY), else the peer itself. Used for rate limits and the admin's allow
list; never stored."""

import ipaddress

from django.conf import settings


def _in(ip, nets):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip(request):
    peer = request.META.get("REMOTE_ADDR", "")
    real = request.headers.get("X-Real-IP", "").strip()
    if real and _in(peer, settings.TRUSTED_PROXIES):
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return peer


def admin_allowed(request):
    return _in(client_ip(request), settings.ADMIN_ALLOW)
