"""Seed loopback mock API so a freshly provisioned dashboard has data."""
import asyncio
from benchmark.load_test import http_json


async def main():
    status, health = await http_json("http://127.0.0.1:8000", "GET", "/health")
    if status != 200 or health.get("providers") != "mock":
        raise SystemExit("Expected a local mock API")
    for prompt in ("hello", "calculate 2 plus 2", "code reason prove a step", "hello"):
        status, _ = await http_json("http://127.0.0.1:8000", "POST", "/v1/route",
                                   {"messages":[{"role":"user","content":prompt}]})
        if status != 200:
            raise SystemExit("Mock seed request failed")
    print("Seeded four mock API requests")


if __name__ == "__main__":
    asyncio.run(main())
