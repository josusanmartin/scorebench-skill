"""Per-connection transport policy shared by OpenRouter inference and lookups."""
from functools import partial
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import os
import socket
import urllib.request

IP_FAMILY_ENV = "SCOREBENCH_OPENROUTER_IP_FAMILY"


def configured_ip_family(env=None):
    value = (os.environ if env is None else env).get(IP_FAMILY_ENV, "auto").strip().lower()
    if value not in {"auto", "4", "6"}:
        raise ValueError(f"{IP_FAMILY_ENV} must be auto, 4 or 6")
    return value


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, family):
    # Resolve only the requested family; never patch process-wide DNS or replace
    # the HTTPS hostname, which the stdlib still uses for SNI and verification.
    last_error = None
    for af, socktype, proto, _, sockaddr in socket.getaddrinfo(*address, family, socket.SOCK_STREAM):
        connection = None
        try:
            connection = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("OpenRouter DNS returned no addresses in the selected family")


class UnsentRequestError(OSError):
    """Connection establishment failed before any inference request bytes."""

    def __init__(self, cause):
        super().__init__("upstream connection establishment failed")
        self.cause = cause


class _ConnectionPhase:
    def __init__(self, *args, ip_family="auto", **kwargs):
        super().__init__(*args, **kwargs)
        selected = configured_ip_family({IP_FAMILY_ENV: ip_family})
        if selected != "auto":
            self._create_connection = partial(_create_connection,
                family=socket.AF_INET if selected == "4" else socket.AF_INET6)

    def connect(self):
        try:
            super().connect()
        except (OSError, HTTPException) as exc:
            raise UnsentRequestError(exc) from exc


class _HTTPConnection(_ConnectionPhase, HTTPConnection):
    pass


class _HTTPSConnection(_ConnectionPhase, HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, ip_family):
        super().__init__()
        self.ip_family = ip_family

    def http_open(self, request):
        return self.do_open(partial(_HTTPConnection, ip_family=self.ip_family), request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ip_family):
        super().__init__()
        self.ip_family = ip_family

    def https_open(self, request):
        return self.do_open(partial(_HTTPSConnection, ip_family=self.ip_family), request, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # A redirected connection failure cannot prove the first POST was unsent.
        return None


def openrouter_opener():
    family = configured_ip_family()
    return urllib.request.build_opener(_HTTPHandler(family), _HTTPSHandler(family), _NoRedirect())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate the OpenRouter transport setting without network requests")
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    try:
        family = configured_ip_family()
    except ValueError as exc:
        parser.error(str(exc))
    print(f"ScoreBench OpenRouter transport: IP family {family}; TLS verification enabled")
