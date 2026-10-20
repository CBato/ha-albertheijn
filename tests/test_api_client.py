"""API client: token retry keeps extra headers, HTTP status on the error, 404 barcode -> None."""

import asyncio

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

from albert_heijn import api as A

class Resp:
    def __init__(s, status, body=None): s.status, s.body, s.content_type = status, body, "application/json"
    async def json(s): return s.body
    async def text(s): return "err"
    async def __aenter__(s): return s
    async def __aexit__(s, *a): pass

class Session:
    def __init__(s, script): s.script, s.calls = list(script), []
    def request(s, method, url, headers=None, json=None):
        s.calls.append(dict(headers)); return s.script.pop(0)
    def post(s, url, headers=None, json=None):
        return Resp(200, {"access_token": "new", "refresh_token": "r2"})

async def main():
    s = Session([Resp(401), Resp(200, {})])
    a = A.AlbertHeijnApi(s, "old", "r")
    await a.add_to_order([{"productId": 1, "quantity": 2}], order_id=42)
    assert s.calls[1]["Appie-Current-Order-Id"] == "42" and s.calls[1]["Authorization"] == "Bearer new", s.calls
    s = Session([Resp(404)])
    assert await A.AlbertHeijnApi(s, "t", "r").lookup_gtin("8710412046863") is None
    s = Session([Resp(400)])
    try:
        await A.AlbertHeijnApi(s, "t", "r").add_to_order([], order_id=1); assert False
    except A.AlbertHeijnApiError as e:
        assert e.status == 400
    print("API OK")
asyncio.run(main())
