"""Fixed mock workload. Default transport is the loopback FastAPI endpoint."""
import argparse
import asyncio
import json
import os
import platform
from pathlib import Path
import time
from urllib.parse import urlsplit
from api.contracts import RouterError
from benchmark.statistics import percentile
from monitoring.ledger import SQLiteLedger
from routing.service import Router


async def http_json(base_url, method, path, body=None):
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Load tests only allow loopback HTTP mock endpoints")
    reader, writer = await asyncio.open_connection(url.hostname, url.port or 80)
    raw = json.dumps(body).encode() if body is not None else b""
    message = (f"{method} {path} HTTP/1.1\r\nHost: {url.hostname}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
               "Connection: close\r\n\r\n").encode() + raw
    writer.write(message)
    await writer.drain()
    headers = await reader.readuntil(b"\r\n\r\n")
    status = int(headers.split(b" ")[1])
    content = await reader.read()
    writer.close()
    await writer.wait_closed()
    return status, json.loads(content)


async def workload(n, duration):
    start = time.monotonic()
    for index in range(n):
        await asyncio.sleep(max(0, start + index * duration / max(n,1) - time.monotonic()))
        yield index


async def execute(n=100, concurrency=10, duration=5, threshold_ms=50,
                  transport="http", url="http://127.0.0.1:8000"):
    if n < 1 or concurrency < 1 or duration < 0:
        raise ValueError("Invalid workload")
    router = Router(SQLiteLedger()) if transport == "in-process" else None
    if transport == "http":
        _, health = await asyncio.wait_for(http_json(url, "GET", "/health"), timeout=5)
        if health.get("providers") != "mock":
            raise ValueError("Load test requires a mock-provider API")
    semaphore = asyncio.Semaphore(concurrency)
    latencies, failures = [], []
    async def send(index):
        async with semaphore:
            start = time.perf_counter()
            body = dict(messages=[dict(role="user", content=f"hello load request {index}")])
            try:
                if router:
                    result = await router.handle(body)
                    status = 200
                else:
                    status, result = await asyncio.wait_for(http_json(url, "POST", "/v1/route", body), 10)
                failures.append(status != 200 or result.get("data_source") != "mock")
            except (Exception, RouterError):
                failures.append(True)
            latencies.append((time.perf_counter()-start)*1000)
    start = time.monotonic()
    tasks = []
    async for index in workload(n, duration):
        tasks.append(asyncio.create_task(send(index)))
    await asyncio.gather(*tasks)
    p95 = percentile(latencies, .95)
    return dict(data_source="mock", transport=transport, N=n, C=concurrency, D=duration,
                elapsed=time.monotonic()-start, errors=sum(failures), error_rate=sum(failures)/n,
                local_overhead_p95_ms=p95, threshold_ms=threshold_ms,
                passed=sum(failures)/n < .01 and p95 < threshold_ms,
                scope="local router overhead; no real-provider latency",
                hardware=dict(platform=platform.platform(), processor=platform.processor(),
                              machine=platform.machine(), logical_cpus=os.cpu_count(),
                              python=platform.python_version()))


async def main(args):
    result = await execute(args.n, args.concurrency, args.duration, args.threshold_ms,
                           args.transport, args.url)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=5)
    parser.add_argument("--threshold-ms", type=float, default=50)
    parser.add_argument("--transport", choices=["http","in-process"], default="http")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="results/load-test-mock.json")
    asyncio.run(main(parser.parse_args()))
