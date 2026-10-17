"""FastAPI adapter. Production compose always uses Postgres admission."""
from contextlib import asynccontextmanager
import asyncio
import json
import os
from fastapi import FastAPI, Request as HTTPRequest
from fastapi.responses import JSONResponse, Response
from api.contracts import RouterError
from routing.service import Router
from monitoring.ledger import PostgresLedger, SQLiteLedger
from cache.exact import RedisCache, RedisRate
from fallback.providers import MockJudgeProvider, MockProvider, real_provider
from monitoring.quality import Quality, validate_calibration
from routing.config import Config
from routing.latency import Health, RedisHealthStore


def build_router(real=False):
    mode = os.environ.get("PROVIDER_MODE", "mock")
    if mode not in ("mock", "real"):
        raise ValueError("Unknown PROVIDER_MODE")
    if mode == "real" and not real:
        raise ValueError("Real mode requires the --real entry point flag")
    provider = real_provider(real) if real else MockProvider()
    config = Config(os.environ.get("ROUTER_CONFIG"))
    if os.environ.get("STORAGE_MODE") == "test-double":
        if real:
            raise ValueError("Real entry points require shared Postgres spend admission")
        return Router(SQLiteLedger(cap=config.data["global_cap_usd"],
                                   evaluation_cap=config.data["evaluation_cap_usd"]),
                      config=config, provider=provider)
    import redis
    ledger = PostgresLedger(os.environ.get("DATABASE_URL",
                            "postgresql://router:router@localhost:5432/router"),
                            cap=config.data["global_cap_usd"],
                            evaluation_cap=config.data["evaluation_cap_usd"])
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                                  socket_timeout=.2, socket_connect_timeout=.2, decode_responses=True)
    validated = False
    ratings_path = os.environ.get("CALIBRATION_RATINGS_FILE")
    if ratings_path:
        from pathlib import Path
        ratings = json.loads(Path(ratings_path).read_text())
        validated = validate_calibration(ratings)["validated"]
    quality = Quality(ledger, config, provider if real else MockJudgeProvider(), validated=validated,
                      enabled=os.environ.get("QUALITY_ROUTING") == "true")
    return Router(ledger, config=config, provider=provider, cache=RedisCache(client), rate=RedisRate(client),
                  health=Health(RedisHealthStore(client), config.slo_ms), quality=quality)


def create_app(router=None):
    @asynccontextmanager
    async def lifespan(app):
        if app.state.router is None:
            app.state.router = build_router()
        service = app.state.router

        async def recovery():
            while True:
                try:
                    await asyncio.to_thread(service.settlement_journal.recover, service.ledger)
                    await asyncio.to_thread(service.ledger.recover)
                except Exception:
                    pass
                await asyncio.sleep(5)
        worker = asyncio.create_task(recovery())
        quality_worker = asyncio.create_task(service.quality.worker()) if service.quality else None
        yield
        await service.drain_background()
        if service.quality:
            await service.quality.queue.join()
        worker.cancel()
        if quality_worker:
            quality_worker.cancel()
        await asyncio.gather(worker, *([quality_worker] if quality_worker else []), return_exceptions=True)

    app = FastAPI(title="Model Router", lifespan=lifespan)
    app.state.router = router

    @app.exception_handler(RouterError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.message, "request_id": exc.request_id})

    @app.post("/v1/route")
    async def route(request: HTTPRequest):
        # Stream bounds prevent an unbounded body allocation; do not log headers/body.
        started = app.state.router.clock()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32768:
                return await invalid(400, "Input exceeds 32 KB")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return await invalid(400, "Invalid JSON")
        return await app.state.router.handle(data, started=started)

    async def invalid(status, message):
        exc = RouterError(status, message)
        try:
            app.state.router.ledger.begin(exc.request_id)
            app.state.router.ledger.finish(exc.request_id, outcome=str(status))
        except Exception:
            pass
        return JSONResponse(status_code=status, content={"error": message, "request_id": exc.request_id})

    @app.get("/health")
    async def health():
        return {"status": "ok", "providers": app.state.router.provider.mode}

    @app.get("/metrics")
    async def metrics():
        from monitoring.metrics import prometheus
        return Response(prometheus(app.state.router.ledger, os.environ.get("MATCHED_RESULTS_FILE"),
                                   data_source=app.state.router.provider.mode), media_type="text/plain; version=0.0.4")

    return app


app = create_app()


if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Start the router; mock providers by default")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(build_router(real=args.real)), host=args.host, port=args.port)
