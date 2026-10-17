import asyncio
import logging
import time
from decimal import Decimal
from uuid import uuid4
from api.contracts import Request, RouterError
from cache.exact import key, MemoryCache
from classifier.scorer import score
from fallback.providers import MockProvider, ProviderError, Output
from monitoring.ledger import SpendExceeded, StorageUnavailable
from monitoring.settlement import SettlementJournal, settle_response
from routing.admission import Admission
from routing.config import Config
from routing.latency import Health
from routing.policy import ordered_models

LOG = logging.getLogger('router')


class Router:
    def __init__(self, ledger, config=None, provider=None, cache=None, rate=None,
                 clock=time.monotonic, health=None, quality=None, benchmark=False,
                 background_size=100, settlement_journal=None):
        self.ledger = ledger
        self.settlement_journal = settlement_journal or SettlementJournal()
        self.config = config or Config()
        self.provider = provider or MockProvider()
        self.cache = cache if cache is not None else MemoryCache()
        health = health or Health(slo_ms=self.config.slo_ms)
        if benchmark:
            health = health.for_benchmark(str(uuid4()))
        self.admission = Admission(health, rate)
        self.rate, self.health = self.admission.rate, self.admission.health
        self.clock, self.quality = clock, quality
        if quality and hasattr(quality, 'admission'):
            quality.admission = self.admission
        self.benchmark = benchmark
        if background_size < 1:
            raise ValueError("Background queue must be bounded and nonempty")
        self.background = asyncio.Queue(maxsize=background_size)
        self._background_task = None

    def enqueue_background(self, operation):
        try:
            self.background.put_nowait(operation)
        except asyncio.QueueFull:
            LOG.warning('response_background_queue_full')
            return False
        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_worker())
        return True

    async def _background_worker(self):
        while not self.background.empty():
            operation = self.background.get_nowait()
            try:
                # Background persistence has its own bounded storage timeout;
                # it must not inherit an already-expired request deadline.
                with self.ledger.deadline(float('inf')):
                    await operation()
            except Exception:
                LOG.warning('response_background_failed')
            finally:
                self.background.task_done()

    async def drain_background(self):
        await self.background.join()
        if self._background_task:
            await self._background_task

    def cache_key(self, request, model):
        return key(request, model['id'], self.config.version, model.get('litellm', {}),
                   self.config.provider_version)

    def _defer(self, request_id, tier, request, result, cache_key=None, response=None):
        # Capture immutable values now; all I/O runs after the response coroutine.
        async def work():
            if cache_key is not None:
                try:
                    await asyncio.to_thread(self.cache.put, cache_key, response)
                except Exception:
                    LOG.warning('cache_unavailable')
            if self.quality:
                if hasattr(self.quality, 'async_submit'):
                    await self.quality.async_submit(request_id, tier, request, result)
                else:
                    await asyncio.to_thread(self.quality.submit, request_id, tier, request, result)
        if cache_key is not None or self.quality:
            self.enqueue_background(work)

    async def handle(self, data, **kwargs):
        started = kwargs.pop('started', self.clock())
        try:
            request = Request.parse(data)
        except RouterError as exc:
            try:
                self.ledger.begin(exc.request_id, kwargs.get('run_id'))
                self.ledger.finish(exc.request_id, outcome=str(exc.status))
            except StorageUnavailable:
                pass
            raise
        return await self.route(request, started=started, **kwargs)

    async def route(self, request, use_cache=True, force_tier=None, run_id=None,
                    started=None, force_model=None):
        started = self.clock() if started is None else started
        with self.ledger.deadline(started + request.deadline_ms / 1000, self.clock):
            return await self._route(request, use_cache, force_tier, run_id, started, force_model)

    async def _route(self, request, use_cache, force_tier, run_id, started, force_model):
        end = started + request.deadline_ms / 1000
        request_id = request.request_id
        attempts = 0
        spent = Decimal(0)
        tier = force_tier or score(request, self.config.data['thresholds'])[0]
        raised = bool(self.quality and not self.benchmark and self.quality.raised(tier))
        chain = [force_model] if force_model else ordered_models(self.config, tier, raised)

        def remaining():
            left = end - self.clock()
            if left <= 0:
                raise RouterError(504, 'Deadline exhausted', request_id)
            return left

        def finish(outcome, **fields):
            self.ledger.finish(request_id, outcome=str(outcome), tier=tier,
                               latency_ms=(self.clock() - started) * 1000,
                               fallback_count=max(0, attempts - 1), **fields)

        try:
            if use_cache:
                for alias in chain:
                    remaining()
                    model = self.config.model(alias)
                    try:
                        cached = await asyncio.wait_for(
                            asyncio.to_thread(self.cache.get, self.cache_key(request, model)),
                            timeout=remaining())
                    except (asyncio.TimeoutError, TimeoutError):
                        raise RouterError(504, 'Deadline exhausted', request_id) from None
                    except Exception:
                        LOG.warning('cache_unavailable')
                        remaining()
                        break
                    remaining()
                    if cached:
                        response = dict(cached, cost=0.0, cache_hit=True, fallback_count=0, tier=tier,
                                        request_id=request_id)
                        try:
                            self.ledger.retry_admission(self.ledger.begin, request_id, run_id)
                            remaining()
                            finish(200, cache_hit=1, final_model=model['id'])
                        except StorageUnavailable:
                            LOG.warning('cache hit ledger unavailable request_id=%s', request_id)
                        remaining()
                        self._defer(request_id, tier, request,
                                    Output(response['content'], response['usage']['input_tokens'],
                                           response['usage']['output_tokens']))
                        remaining()
                        return response
            self.ledger.retry_admission(self.ledger.begin, request_id, run_id)
            remaining()
            self.ledger.retry_admission(self.ledger.ping)
            remaining()
            self.ledger.retry_admission(self.ledger.check, min(self.config.reserve_cost(a, request) for a in chain))
            remaining()
            budget_eligible = False
            for alias in chain:
                model = self.config.model(alias)
                for retry in range(2):
                    remaining()
                    worst = self.config.reserve_cost(alias, request)
                    if request.max_cost_usd is not None and spent + worst > request.max_cost_usd:
                        break
                    budget_eligible = True
                    permit = self.admission.acquire(model)
                    if permit is None:
                        remaining()
                        break
                    attempt_id = str(uuid4())
                    try:
                        remaining()
                        self.ledger.retry_admission(self.ledger.reserve, attempt_id, request_id, model['id'], model['provider'],
                                            'inference', self.config.version, worst)
                        remaining()
                        self.ledger.retry_admission(self.ledger.dispatch, attempt_id)
                        remaining()
                    except BaseException:
                        self.health.release(model['id'], permit)
                        raise
                    attempts += 1
                    spent += worst
                    call_start = self.clock()
                    result = None
                    error = None
                    try:
                        timeout = min(remaining(), 30)
                        result = await asyncio.wait_for(
                            self.health.run_probe(model['id'], permit,
                                self.provider.complete(model, request, timeout), clock=self.clock), timeout=timeout)
                        # Measured usage must settle even if the deadline just passed.
                    except (asyncio.TimeoutError, TimeoutError):
                        error = ProviderError(408, 'provider timeout')
                    except ProviderError as exc:
                        error = exc
                    except (ConnectionError, OSError) as exc:
                        error = ProviderError(0, str(exc))
                    except Exception as exc:
                        error = ProviderError(500, str(exc))
                    # Only a provider HTTP 400 proves rejection without spend.
                    # A successful response rejected below still carries usage.
                    definitive_rejection = error is not None and error.status == 400
                    paid_result = result
                    latency = (self.clock() - call_start) * 1000
                    if result is not None and (not result.content or result.stop_reason == 'content_filter'):
                        error = ProviderError(400, 'Empty or content-policy response',
                                              usage=(result.input_tokens, result.output_tokens))
                        result = None
                    accepted = self.health.observe(model['id'], permit, result is not None, latency)
                    lost_probe = not accepted
                    if lost_probe and not definitive_rejection:
                        usage = ((result.input_tokens, result.output_tokens) if result else
                                 error.usage if error else None)
                        error = ProviderError(503, 'Probe lease lost; result abandoned', usage=usage)
                        result = None
                    if error:
                        if paid_result is None:
                            self.ledger.error(attempt_id, {'status': error.status, 'raw': str(error)})
                        if definitive_rejection:
                            self.ledger.settle(attempt_id, Decimal(0), 0, 0, rejected=True,
                                               error={'status': error.status, 'raw': str(error)})
                            spent -= worst
                        elif error.usage is not None:
                            actual = self.config.cost(alias, *error.usage)
                            if paid_result is not None:
                                _, charged = await settle_response(
                                    self.ledger, self.settlement_journal, attempt_id, paid_result,
                                    actual, worst, error={'status': error.status, 'raw': str(error)})
                            else:
                                self.ledger.settle(attempt_id, actual, *error.usage,
                                                   error={'status': error.status, 'raw': str(error)})
                                charged = actual
                            spent += charged - worst
                        remaining()
                        if lost_probe:
                            raise RouterError(503, str(error), request_id)
                        if error.retryable and retry == 0:
                            await asyncio.sleep(min(.01, remaining()))
                            remaining()
                            continue
                        break
                    actual = self.config.cost(alias, result.input_tokens, result.output_tokens)
                    response = dict(model=model['id'], resolved_model=result.resolved_model,
                                    tier=tier, content=result.content,
                                    usage=dict(input_tokens=result.input_tokens,
                                               output_tokens=result.output_tokens),
                                    cost=float(spent), cache_hit=False,
                                    fallback_count=attempts - 1, request_id=request_id,
                                    stop_reason=result.stop_reason, data_source=self.provider.mode)
                    settlement_status, charged = await settle_response(
                        self.ledger, self.settlement_journal, attempt_id, result, actual, worst,
                        response=response)
                    spent += charged - worst
                    response.update(cost=float(spent), settlement_status=settlement_status,
                                    attempt_id=attempt_id)
                    remaining()
                    try:
                        finish(200, final_model=model['id'])
                    except StorageUnavailable:
                        # A paid answer must survive a later request-summary failure too.
                        payload = dict(resolved_model=result.resolved_model,
                                       input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                       actual=str(actual), reserved=str(worst), error=None, rejected=False,
                                       response=response, content=result.content, stop_reason=result.stop_reason)
                        try:
                            self.settlement_journal.save(attempt_id, payload, settlement_status)
                        except Exception:
                            response['recovery_persisted'] = False
                            LOG.critical('response_journal_unavailable attempt_id=%s', attempt_id)
                        LOG.warning('response_summary_pending attempt_id=%s', attempt_id)
                    remaining()
                    self._defer(request_id, tier, request, result,
                                self.cache_key(request, model) if use_cache and result.stop_reason == 'stop'
                                and settlement_status != 'unknown' else None,
                                response)
                    remaining()
                    return response
            remaining()
            affordable = any(request.max_cost_usd is None or
                             spent + self.config.reserve_cost(a, request) <= request.max_cost_usd
                             for a in chain)
            status = 503 if budget_eligible and affordable else 402
            raise RouterError(status, 'No eligible model or all attempts failed', request_id)
        except (StorageUnavailable, SpendExceeded) as exc:
            status = 504 if self.clock() >= end else 503
            try:
                finish(status)
            except StorageUnavailable:
                pass
            raise RouterError(status, 'Deadline exhausted' if status == 504 else str(exc), request_id) from None
        except RouterError as exc:
            exc.request_id = request_id
            try:
                finish(exc.status)
            except StorageUnavailable:
                pass
            raise
