import unittest
try:
    import fastapi
    import httpx
except ImportError:
    fastapi = None


@unittest.skipUnless(fastapi is not None, "FastAPI/httpx not installed; dependency install unavailable")
class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_transport(self):
        from api.app import create_app
        from monitoring.ledger import SQLiteLedger
        from routing.service import Router
        app = create_app(Router(SQLiteLedger()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/route", json={"messages": [{"role": "user", "content": "hello"}]})
            self.assertEqual(r.status_code, 200)
            self.assertTrue({"model", "tier", "content", "usage", "cost", "cache_hit",
                             "fallback_count", "request_id"} <= set(r.json()))
            for body in ({}, {"messages": [{"role": "user", "content": "x" * 33000}]}):
                r = await c.post("/v1/route", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("request_id", r.json())
            self.assertEqual((await c.post("/v1/route", content="bad")).status_code, 400)
