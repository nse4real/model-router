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
from fallback.providers import MockJudgeProvider
from monitoring.quality import Quality, validate_calibration
from routing.config import Config
from routing.latency import Health, RedisHealthStore


def build_router():
    mode = os.environ.get("PROVIDER_MODE", "mock")
    if mode != "mock":
        raise RuntimeError("This entry point permits mock providers only")
    if os.environ.get("STORAGE_MODE") == "test-double":
        return Router(SQLiteLedger())
    import redis
    config = Config()
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
    quality = Quality(ledger, config, MockJudgeProvider(), validated=validated,
                      enabled=os.environ.get("QUALITY_ROUTING") == "true")
    return Router(ledger, config=config, cache=RedisCache(client), rate=RedisRate(client),
                  health=Health(RedisHealthStore(client), config.data["slo_ms"]), quality=quality)


def create_app(router=None):
    @asynccontextmanager
    async def lifespan(app):
        if app.state.router is None:
            app.state.router = build_router()
        service = app.state.router

        async def recovery():
            while True:
                try:
                    await asyncio.to_thread(service.ledger.recover)
                except Exception:
                    pass
                await asyncio.sleep(5)
        worker = asyncio.create_task(recovery())
        quality_worker = asyncio.create_task(service.quality.worker()) if service.quality else None
        yield
        worker.cancel()
        if quality_worker:
            quality_worker.cancel()
        await asyncio.gather(worker, *([quality_worker] if quality_worker else []), return_exceptions=True)

    app = FastAPI(title="Model Router (mock)", lifespan=lifespan)
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
        return {"status": "ok", "providers": "mock"}

    @app.get("/metrics")
    async def metrics():
        from monitoring.metrics import prometheus
        return Response(prometheus(app.state.router.ledger, os.environ.get("MATCHED_RESULTS_FILE")), media_type="text/plain; version=0.0.4")

    return app


app = create_app()
