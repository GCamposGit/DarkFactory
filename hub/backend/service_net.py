"""SSRF guard for Hub health probes and redirects (DF-08)."""

import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from typing import Tuple


BLOCKED_SSRF_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / Cloud Metadata (AWS, GCP, Azure, etc.)
    ipaddress.ip_network("0.0.0.0/8"),          # Current network
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved
    ipaddress.ip_network("255.255.255.255/32"), # Broadcast
]

BLOCKED_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


class DisallowedDestinationError(urllib.error.URLError):
    """Raised when a probe or redirect targets a forbidden IP/host or scheme."""
    pass


def is_destination_allowed(url: str) -> Tuple[bool, str]:
    """
    Evaluates whether a target URL is permitted for health checks and network probing (DF-08).
    Strictly blocks cloud metadata endpoints, link-local addresses, and non-HTTP schemes.
    Deliberately permits legitimate local cluster services (localhost, 127.0.0.1, ::1).
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid"

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception as exc:
        return False, f"Malformed URL: {exc}"

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False, f"Protocol '{scheme}' not permitted for health probes (only HTTP/HTTPS allowed)"

    host = parsed.hostname
    if not host:
        return False, "Missing hostname in target URL"

    host_lower = host.strip("[]").lower()

    # Explicitly permitted loopback endpoints for local services.
    if host_lower in ("localhost", "127.0.0.1", "::1", "testserver"):
        return True, "Permitted local loopback service"

    # Check for metadata hostnames
    if host_lower in BLOCKED_METADATA_HOSTS:
        return False, f"Cloud metadata host '{host_lower}' is strictly blocked"

    # Check if host is an IP address literal
    try:
        ip_obj = ipaddress.ip_address(host_lower)
        for net in BLOCKED_SSRF_NETWORKS:
            if ip_obj in net:
                return False, f"Destination IP {host_lower} is blocked by SSRF policy (cloud metadata/link-local)"

        if ip_obj.is_private and not ip_obj.is_loopback:
            return False, f"Private network address {host_lower} is blocked by default SSRF policy"

        if ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_link_local:
            return False, f"Address {host_lower} is blocked by SSRF policy"
    except ValueError:
        # Host is a domain name (not raw IP)
        pass

    return True, "Permitted external destination"


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Inspects HTTP redirect Location headers to prevent SSRF bypass via 3xx redirects (DF-08).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed, reason = is_destination_allowed(newurl)
        if not allowed:
            raise DisallowedDestinationError(
                f"SSRF Redirect Blocked: redirect to '{newurl}' is disallowed ({reason})"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)
