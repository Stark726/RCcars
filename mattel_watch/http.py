"""Small JSON HTTP transport: bounded reads, no POST retries, no secret URLs in errors."""
import http.cookiejar
import json
import urllib.error
import urllib.request


class TransportError(Exception):
    def __init__(self, status=None, ambiguous=False):
        self.status = status
        self.ambiguous = ambiguous
        super().__init__(f"HTTP {status}" if status else "Network request failed")


class JsonHTTP:
    def __init__(self, timeout=25):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, url, payload=None):
        headers = {"Accept": "application/json", "User-Agent": "MattelWatch/0.1 (personal product monitor)"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise TransportError(ambiguous=payload is not None)
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise TransportError(status, ambiguous=payload is not None and status >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TransportError(ambiguous=payload is not None) from None
