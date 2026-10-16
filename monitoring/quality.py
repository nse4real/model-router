"""Bounded asynchronous judge; quality cannot trigger a repeat inference."""
import asyncio
from collections import defaultdict, deque
import json
import logging
from pathlib import Path
import random
from statistics import mean
from uuid import uuid4
from api.contracts import Request
from benchmark.statistics import bootstrap_ci, spearman, correlation_ci
from monitoring.ledger import StorageUnavailable
from monitoring.security import redact

LOG = logging.getLogger("router.quality")


def validate_calibration(pairs, seed=13, resamples=10000):
    """Each pair contains human_a, human_b, judge_a, judge_b, tier and task_type.
    25 matched pairs supply the required 50 individually human-rated outputs.
    """
    if len(pairs) < 25:
        return dict(validated=False, reason="Need at least 50 individually rated outputs")
    for p in pairs:
        if any(type(p.get(k)) is not int or not 1 <= p[k] <= 5
               for k in ("human_a", "human_b", "judge_a", "judge_b")):
            return dict(validated=False, reason="Invalid or missing ratings")
        if not p.get("tier") or not p.get("task_type"):
            return dict(validated=False, reason="Missing stratification")

    def gate(rows):
        agreement = mean(abs(r["human_" + side] - r["judge_" + side]) <= 1
                         for r in rows for side in ("a", "b"))
        h = [r["human_b"] - r["human_a"] for r in rows]
        j = [r["judge_b"] - r["judge_a"] for r in rows]
        bias = [a - b for a, b in zip(j, h)]
        ci = bootstrap_ci(bias, seed, resamples)
        rho = spearman(h, j)
        rho_ci = correlation_ci(h, j, seed, resamples)
        passed = (agreement >= .8 and -.2 <= mean(bias) <= .2 and ci[0] >= -.2 and ci[1] <= .2
                  and rho is not None and rho > 0 and rho_ci[0] is not None and rho_ci[0] > 0)
        return dict(passed=passed, n=len(rows), within_one=agreement, bias=mean(bias),
                    bias_ci=ci, spearman=rho, spearman_ci=rho_ci)

    breakdown = {"overall": gate(pairs)}
    for field in ("tier", "task_type"):
        for value in sorted({p[field] for p in pairs}):
            breakdown[field + ":" + value] = gate([p for p in pairs if p[field] == value])
    # Require all configured strata, rather than passing on an accidentally narrow sample.
    strata = ({p["tier"] for p in pairs} >= {"simple", "medium", "complex"} and
              {p["task_type"] for p in pairs} >= {"general", "reasoning", "code", "maths"})
    return dict(validated=strata and all(g["passed"] for g in breakdown.values()),
                breakdown=breakdown, seed=seed, resamples=resamples)


class Quality:
    def __init__(self, ledger, config, provider, validated=False, enabled=False,
                 benchmark=False, queue_size=100, sample_rate=.1, seed=13, timeout=5):
        self.ledger, self.config, self.provider = ledger, config, provider
        self.validated, self.enabled, self.benchmark = validated, enabled, benchmark
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.rng = random.Random(seed)
        self.sample_rate, self.timeout = sample_rate, timeout
        self.windows = defaultdict(lambda: deque(maxlen=30))
        self.alerts = set()
        self.recovery = defaultdict(list)

    def raised(self, route):
        return self.enabled and self.validated and not self.benchmark and route in self.alerts

    def observe(self, route, value):
        if route in self.alerts:
            self.recovery[route].append(value)
            if len(self.recovery[route]) == 30:
                if mean(self.recovery[route]) >= 3.5:
                    self.alerts.discard(route)
                    self.windows[route].clear()
                self.recovery[route].clear()
        else:
            window = self.windows[route]
            window.append(value)
            if len(window) == 30 and mean(window) < 3.5:
                self.alerts.add(route)
                self.recovery[route].clear()

    def unknown(self, job, detail):
        LOG.warning("quality_unknown %s", redact({"attempt_id": job[0], "reason": str(detail)}))
        try:
            self.ledger.job(job[0], job[1], "quality_unknown")
        except StorageUnavailable:
            LOG.warning("quality_unknown storage_unavailable")

    def submit(self, parent, route, request, output):
        if self.rng.random() >= self.sample_rate:
            return None
        job = (str(uuid4()), parent, route, request, output)
        try:
            self.ledger.job(job[0], parent)
            self.queue.put_nowait(job)
        except (asyncio.QueueFull, StorageUnavailable) as exc:
            self.unknown(job, exc)
        return job[0]

    async def process(self, job):
        attempt, parent, route, request, output = job
        alias = self.config.data["judge"]
        model = self.config.model(alias)
        try:
            if not self.ledger.claim_job(attempt):
                return
            rubric = Path(__file__).with_name("judge_prompt_v1.md").read_text()
            judge_request = Request.parse(dict(messages=[dict(role="system", content=rubric),
                 dict(role="user", content=json.dumps(dict(messages=list(request.messages),
                                                           answer=output.content)))], max_tokens=32))
            worst = self.config.reserve_cost(alias, judge_request)
            self.ledger.reserve(attempt, parent, model["id"], model["provider"], "evaluation",
                                self.config.version, worst)
            self.ledger.dispatch(attempt)
            result = await asyncio.wait_for(self.provider.complete(model, judge_request, self.timeout),
                                            timeout=self.timeout)
            actual = self.config.cost(alias, result.input_tokens, result.output_tokens)
            self.ledger.settle(attempt, actual, result.input_tokens, result.output_tokens)
            parsed = json.loads(result.content)
            value = parsed["score"]
            if type(value) is not int or not 1 <= value <= 5:
                raise ValueError("Malformed judge score")
            if self.ledger.job(attempt, parent, "completed", value):
                self.observe(route, value)
        except Exception as exc:
            self.unknown(job, exc)

    async def drain(self):
        while not self.queue.empty():
            job = await self.queue.get()
            try:
                await self.process(job)
            finally:
                self.queue.task_done()

    async def worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self.process(job)
            finally:
                self.queue.task_done()
