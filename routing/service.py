import asyncio
import logging
import time
from decimal import Decimal
from uuid import uuid4
from api.contracts import Request, RouterError
from cache.exact import key, MemoryCache, MemoryRate
from classifier.scorer import score
from fallback.providers import MockProvider, ProviderError, Output
from monitoring.ledger import SpendExceeded, StorageUnavailable
from monitoring.security import redact
from routing.config import Config
from routing.policy import ordered_models

LOG = logging.getLogger("router")


class Router:
    def __init__(self, ledger, config=None, provider=None, cache=None, rate=None,
                 clock=time.monotonic, health=None, quality=None, benchmark=False):
        self.ledger = ledger
        self.config = config or Config()
        self.provider = provider or MockProvider()
        self.cache = cache if cache is not None else MemoryCache()
        self.rate = rate or MemoryRate()
        self.clock, self.health, self.quality = clock, health, quality
        self.benchmark = benchmark

    async def handle(self, data, **kwargs):
        started = kwargs.pop("started", self.clock())
        try:
            request = Request.parse(data)
        except RouterError as exc:
            try:
                self.ledger.begin(exc.request_id, kwargs.get("run_id"))
                self.ledger.finish(exc.request_id, outcome=str(exc.status))
            except StorageUnavailable:
                pass
            raise
        return await self.route(request, started=started, **kwargs)

    async def route(self, request, use_cache=True, force_tier=None, run_id=None, started=None, force_model=None):
        started = self.clock() if started is None else started
        end = started + request.deadline_ms / 1000
        request_id = request.request_id
        attempts = 0
        spent = Decimal(0)
        tier = force_tier or score(request, self.config.data["thresholds"])[0]
        raised = bool(self.quality and not self.benchmark and self.quality.raised(tier))
        chain = [force_model] if force_model else ordered_models(self.config, tier, raised)

        def remaining():
            left = end - self.clock()
            if left <= 0:
                raise RouterError(504, "Deadline exhausted", request_id)
            return left

        def finish(outcome, **fields):
            self.ledger.finish(request_id, outcome=str(outcome), tier=tier,
                               latency_ms=(self.clock() - started) * 1000,
                               fallback_count=max(0, attempts - 1), **fields)

        try:
            # Cache may still serve while spend storage is unavailable.
            if use_cache:
                for alias in chain:
                    remaining()
                    model = self.config.model(alias)
                    try:
                        cached = self.cache.get(key(request, model["id"], self.config.version))
                    except Exception:
                        LOG.warning("cache_unavailable")
                        break
                    remaining()
                    if cached:
                        response = dict(cached, cost=0.0, cache_hit=True, fallback_count=0, tier=tier,
                                        request_id=request_id)
                        try:
                            self.ledger.begin(request_id, run_id)
                            finish(200, cache_hit=1, final_model=model["id"])
                        except StorageUnavailable:
                            LOG.warning("cache hit ledger unavailable request_id=%s", request_id)
                        if self.quality:
                            self.quality.submit(request_id, tier, request,
                                Output(response["content"], response["usage"]["input_tokens"],
                                       response["usage"]["output_tokens"]))
                        remaining()
                        return response
            self.ledger.begin(request_id, run_id)
            self.ledger.ping()  # fail closed before considering paid models
            self.ledger.check(min(self.config.reserve_cost(a, request) for a in chain))
            budget_eligible = False
            for alias in chain:
                model = self.config.model(alias)
                for retry in range(2):
                    remaining()
                    worst = self.config.reserve_cost(alias, request)
                    if request.max_cost_usd is not None and spent + worst > request.max_cost_usd:
                        break
                    budget_eligible = True
                    permit = None
                    if self.health:
                        permit = self.health.acquire(model["id"])
                        if permit is None:
                            break
                    if not self.rate.allow(model["id"], model.get("rpm", 300)):
                        if self.health:
                            self.health.release(model["id"], permit)
                        break
                    attempt_id = str(uuid4())
                    try:
                        self.ledger.reserve(attempt_id, request_id, model["id"], model["provider"],
                                            "inference", self.config.version, worst)
                        self.ledger.dispatch(attempt_id)
                    except BaseException:
                        if self.health:
                            self.health.release(model["id"], permit)
                        raise
                    attempts += 1
                    spent += worst
                    call_start = self.clock()
                    result = None
                    error = None
                    try:
                        timeout = min(remaining(), 30)
                        call = self.provider.complete(model, request, timeout)
                        result = await asyncio.wait_for(
                            self.health.run_probe(model["id"], permit, call) if self.health else call,
                            timeout=timeout)
                    except (asyncio.TimeoutError, TimeoutError):
                        error = ProviderError(408, "provider timeout")
                    except ProviderError as exc:
                        error = exc
                    except (ConnectionError, OSError) as exc:
                        error = ProviderError(0, str(exc))
                    except Exception as exc:
                        error = ProviderError(500, str(exc))
                    if result is not None and (not result.content or result.stop_reason == "content_filter"):
                        error = ProviderError(400, "Empty or content-policy response",
                                              usage=(result.input_tokens, result.output_tokens))
                        result = None
                    latency = (self.clock() - call_start) * 1000
                    if self.health:
                        accepted_probe = self.health.observe(model["id"], permit, result is not None, latency)
                        if not accepted_probe:
                            result = None
                            error = ProviderError(503, "Probe lease lost; result abandoned")
                    if error:
                        self.ledger.error(attempt_id, {"status": error.status, "raw": str(error)})
                        if error.usage is not None:
                            actual = self.config.cost(alias, *error.usage)
                            self.ledger.settle(attempt_id, actual, *error.usage,
                                               error={"status": error.status, "raw": str(error)})
                            spent += actual - worst
                        remaining()
                        if error.retryable and retry == 0:
                            await asyncio.sleep(min(.01, remaining()))
                            continue
                        break
                    actual = self.config.cost(alias, result.input_tokens, result.output_tokens)
                    try:
                        self.ledger.settle(attempt_id, actual, result.input_tokens, result.output_tokens)
                    except Exception:
                        LOG.error("settlement_failed %s", redact({"attempt_id": attempt_id,
                                                                  "content": result.content}))
                        raise StorageUnavailable("Settlement write failed") from None
                    spent += actual - worst
                    remaining()
                    response = dict(model=model["id"], tier=tier, content=result.content,
                                    usage=dict(input_tokens=result.input_tokens,
                                               output_tokens=result.output_tokens),
                                    cost=float(spent), cache_hit=False,
                                    fallback_count=attempts - 1, request_id=request_id,
                                    stop_reason=result.stop_reason, data_source=self.provider.mode)
                    finish(200, final_model=model["id"])
                    if use_cache and result.stop_reason == "stop":
                        try:
                            self.cache.put(key(request, model["id"], self.config.version), response)
                        except Exception:
                            LOG.warning("cache_unavailable")
                    if self.quality:
                        self.quality.submit(request_id, tier, request, result)
                    return response
            affordable = any(request.max_cost_usd is None or
                             spent + self.config.reserve_cost(a, request) <= request.max_cost_usd
                             for a in chain)
            status = 503 if budget_eligible and affordable else 402
            raise RouterError(status, "No eligible model or all attempts failed", request_id)
        except (StorageUnavailable, SpendExceeded) as exc:
            try:
                finish(503)
            except StorageUnavailable:
                pass
            raise RouterError(503, str(exc), request_id) from None
        except RouterError as exc:
            exc.request_id = request_id
            try:
                finish(exc.status)
            except StorageUnavailable:
                pass
            raise
