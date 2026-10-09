"""Standard-library JSON transport; error text never includes response bodies."""
import json
import logging
import re
import socket
import time
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .protocol import ProtocolError, require, validate_body

ERROR_STATUSES = {"invalid_request": 400, "invalid_decision": 400,
                  "authentication_required": 401, "competition_not_found": 404,
                  "participation_not_found": 404, "competition_not_active": 409,
                  "registration_required": 409, "insufficient_bankroll": 409,
                  "stale_action_request": 409, "stale_table": 409,
                  "idempotency_conflict": 409, "internal_error": 500,
                  "temporarily_unavailable": 503}


def redact(text):
    text = re.sub(r"(?i)authorization\s*[:=]\s*[^\r\n]+", "Authorization: [REDACTED]", str(text))
    return re.sub(r"sk_[^\s\"'<>]+", "[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class APIError(Exception):
    def __init__(self, code, retry_after=1.0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Competition API: {code}")


class NetworkError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self, base_url, key, timeout=10.0, max_clock_skew=5.0):
        parts = urlsplit(base_url)
        require(parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}), "HTTPS required except loopback tests")
        require(parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment and parts.path in {"", "/"}, "Invalid service URL")
        self.base_url = base_url.rstrip("/")
        self.key, self.timeout, self.max_clock_skew = key, timeout, max_clock_skew
        self.opener = build_opener(NoRedirect())

    def request(self, command, body=None, timeout=None):
        if command == "discovery":
            require(body is None, "Discovery has no body")
            path, headers, payload = "/api/competitions", {}, None
        else:
            validate_body(command, body)
            path = f"/api/competitions/{command}"
            headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        req = Request(self.base_url + path, data=payload, headers=headers)
        started_wall, started_mono = time.time(), time.monotonic()
        try:
            response = self.opener.open(req, timeout=min(self.timeout, timeout) if timeout is not None else self.timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise NetworkError("Network result unknown") from None
        try:
            with response:
                raw = response.read(4 * 1024 * 1024 + 1)
                require(len(raw) <= 4 * 1024 * 1024, "HTTP response too large")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ProtocolError("Non-JSON API response") from None
                status = response.code
                if status != 200:
                    error = data.get("error") if isinstance(data, dict) else None
                    code = error.get("code") if isinstance(error, dict) else None
                    require(code in ERROR_STATUSES and ERROR_STATUSES[code] == status, "Unknown API error/status")
                    retry = response.headers.get("Retry-After", "1")
                    try:
                        delay = float(retry)
                        require(0 <= delay < float("inf"), "Invalid Retry-After")
                    except ValueError:
                        try:
                            delay = max(0, parsedate_to_datetime(retry).timestamp()-time.time())
                        except (ValueError, TypeError, OverflowError):
                            raise ProtocolError("Invalid Retry-After") from None
                    raise APIError(code, delay)
                require(isinstance(data, dict) and "error" not in data, "Invalid success envelope")
                if command == "discovery":
                    date = response.headers.get("Date")
                    require(date is not None, "Discovery missing server Date for clock check")
                    try:
                        server_time = parsedate_to_datetime(date).timestamp()
                    except (ValueError, TypeError, OverflowError):
                        raise ProtocolError("Invalid server Date") from None
                    elapsed = time.monotonic() - started_mono
                    require(started_wall-self.max_clock_skew-1 <= server_time <= started_wall+elapsed+self.max_clock_skew+1,
                            "System clock differs from server; synchronize before running")
                return data
        except (TimeoutError, socket.timeout, OSError, HTTPException):
            raise NetworkError("Network result unknown") from None
