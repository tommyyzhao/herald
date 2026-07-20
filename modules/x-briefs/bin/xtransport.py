"""
httpx transport that routes twikit's requests through curl_cffi with a real
Chrome TLS/HTTP2 fingerprint.

X's edge rejects plain-httpx requests by their network fingerprint (HTTP 400,
empty body) even with valid cookies; curl_cffi's browser impersonation gets
through. We keep httpx for everything it's good at (cookie jar, redirects,
request building, twikit's whole GraphQL layer) and only swap the wire transport.
"""
import httpx
from curl_cffi.requests import AsyncSession

# Request headers curl_cffi must own itself (forwarding them causes conflicts).
_DROP_REQ = {"host", "content-length", "connection", "accept-encoding"}
# Response headers to drop: curl_cffi already decoded the body, so a leftover
# content-encoding/length would make httpx try to decode again.
_DROP_RESP = {"content-encoding", "content-length", "transfer-encoding", "connection"}


class CurlCffiTransport(httpx.AsyncBaseTransport):
    def __init__(self, impersonate: str = "chrome"):
        self._impersonate = impersonate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        req_headers = [
            (k, v) for k, v in request.headers.items() if k.lower() not in _DROP_REQ
        ]
        async with AsyncSession() as s:
            r = await s.request(
                request.method,
                str(request.url),
                headers=req_headers,
                data=body if body else None,
                impersonate=self._impersonate,
                allow_redirects=False,
                timeout=60,
            )
        try:
            items = r.headers.multi_items()
        except AttributeError:
            items = list(r.headers.items())
        resp_headers = [(k, v) for k, v in items if k.lower() not in _DROP_RESP]
        return httpx.Response(
            status_code=r.status_code,
            headers=resp_headers,
            content=r.content,
            request=request,
        )
