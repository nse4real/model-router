"""Model-level P95 circuit with distributed, renewable single-probe leases."""
import asyncio
from collections import deque
from dataclasses import dataclass
import math
import threading
import time
from uuid import uuid4
from monitoring.ledger import measure_full_completion_latency


@dataclass
class Permit:
    probe: bool = False
    token: str = ""
    valid: bool = True
    local_fallback: bool = False
    latency_ms: float | None = None


class MemoryHealthStore:
    """Share this object between test workers to model the Redis atomic contract."""
    def __init__(self, clock=time.monotonic, ttl_seconds=3600):
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self.states = {}
        self.locks = {}
        self.mutex = threading.RLock()
        self.renew_ok = True

    def _state(self, model):
        state = self.states.get(model)
        if state is None or state["expires"] <= self.clock():
            state = dict(samples=deque(maxlen=50), opened=False, until=0,
                         expires=self.clock() + self.ttl_seconds)
            self.states[model] = state
        return state

    def acquire(self, model):
        with self.mutex:
            state = self._state(model)
            now = self.clock()
            if not state["opened"]:
                return Permit()
            if state["until"] > now:
                return None
            if model in self.locks and self.locks[model][1] > now:
                return None
            token = str(uuid4())
            self.locks[model] = (token, now + 45)
            return Permit(True, token)

    def renew(self, model, token):
        with self.mutex:
            current = self.locks.get(model)
            if not self.renew_ok or not current or current[0] != token or current[1] <= self.clock():
                return False
            self.locks[model] = (token, self.clock() + 45)
            return True

    def release(self, model, token):
        with self.mutex:
            if self.locks.get(model, (None,))[0] == token:
                del self.locks[model]

    def observe(self, model, permit, success, latency, slo):
        with self.mutex:
            state = self._state(model)
            if permit.probe:
                current = self.locks.get(model)
                if not permit.valid or not current or current[0] != permit.token or current[1] <= self.clock():
                    self.release(model, permit.token)
                    return False
                state["opened"] = not (success and latency <= slo)
                state["until"] = self.clock() + 60 if state["opened"] else 0
                state["samples"].clear()
                self.release(model, permit.token)
            else:
                state["samples"].append(latency)
                values = sorted(state["samples"])
                if len(values) >= 20 and values[math.ceil(.95 * len(values)) - 1] > slo:
                    state["opened"], state["until"] = True, self.clock() + 60
            state["expires"] = self.clock() + self.ttl_seconds
            return True


class RedisHealthStore:
    ACQUIRE = """
    local state = cjson.decode(redis.call('GET', KEYS[1]) or '{"opened":false,"until":0,"samples":[]}')
    if not state.opened then return 0 end
    local now = tonumber(redis.call('TIME')[1])
    if state['until'] > now then return -1 end
    if redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', 45) then return 1 end
    return -1
    """
    RENEW = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      redis.call('EXPIRE', KEYS[1], 45); return 1
    end
    return 0
    """
    RELEASE = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
    return 0
    """
    OBSERVE = """
    local state = cjson.decode(redis.call('GET', KEYS[1]) or '{"opened":false,"until":0,"samples":[]}')
    local now = tonumber(redis.call('TIME')[1])
    local latency, slo = tonumber(ARGV[4]), tonumber(ARGV[5])
    if ARGV[1] == '1' then
      if ARGV[6] ~= '1' or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
      state.opened = not (ARGV[3] == '1' and latency <= slo)
      state['until'] = state.opened and (now + 60) or 0
      state.samples = {}
      redis.call('DEL', KEYS[2])
    else
      table.insert(state.samples, latency)
      if #state.samples > 50 then table.remove(state.samples,1) end
      local sorted = {}; for i,v in ipairs(state.samples) do sorted[i]=v end
      table.sort(sorted)
      if #sorted >= 20 and sorted[math.ceil(.95 * #sorted)] > slo then
        state.opened=true; state['until']=now+60
      end
    end
    redis.call('SET', KEYS[1], cjson.encode(state), 'EX', ARGV[7]); return 1
    """

    def __init__(self, client, namespace="", ttl_seconds=3600):
        self.client = client
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds

    def keys(self, model):
        suffix = (self.namespace + ":" if self.namespace else "") + model
        return ("router:health:" + suffix, "router:probe:" + suffix)

    def acquire(self, model):
        token = str(uuid4())
        value = self.client.eval(self.ACQUIRE, 2, *self.keys(model), token)
        return None if value == -1 else Permit(value == 1, token)

    def renew(self, model, token):
        return bool(self.client.eval(self.RENEW, 1, self.keys(model)[1], token))

    def release(self, model, token):
        self.client.eval(self.RELEASE, 1, self.keys(model)[1], token)

    def observe(self, model, permit, success, latency, slo):
        return bool(self.client.eval(self.OBSERVE, 2, *self.keys(model), int(permit.probe),
                                    permit.token, int(success), latency, slo, int(permit.valid),
                                    self.ttl_seconds))


class Health:
    def __init__(self, store=None, slo_ms=20000, renewal_seconds=10):
        self.store = store or MemoryHealthStore()
        self.slo = slo_ms
        self.renewal_seconds = renewal_seconds
        self.local = MemoryHealthStore()
        self.known_open = set()

    def for_benchmark(self, run_id):
        """Isolate all models for a fresh run, preserving the full-completion SLO."""
        if isinstance(self.store, RedisHealthStore):
            store = RedisHealthStore(self.store.client, "benchmark:" + run_id,
                                     self.store.ttl_seconds)
        else:
            clock = self.store.clock if isinstance(self.store, MemoryHealthStore) else self.local.clock
            ttl = self.store.ttl_seconds if isinstance(self.store, MemoryHealthStore) else 3600
            store = MemoryHealthStore(clock, ttl)
        return Health(store, self.slo, self.renewal_seconds)

    def acquire(self, model):
        try:
            permit = self.store.acquire(model)
            if permit is None or permit.probe:
                self.known_open.add(model)
            else:
                self.known_open.discard(model)
            return permit
        except Exception:
            # Cache/rate outages must not stop known-healthy models. Never issue
            # a recovery probe without the distributed lease. Unknown remote
            # health cannot be enforced while Redis is unreachable.
            if model in self.known_open or self.local._state(model)["opened"]:
                return None
            return Permit(local_fallback=True)

    def release(self, model, permit):
        if permit and permit.probe:
            try:
                self.store.release(model, permit.token)
            except Exception:
                permit.valid = False

    def observe(self, model, permit, success, latency):
        if permit.latency_ms is not None:
            latency = permit.latency_ms
        if permit.local_fallback:
            return self.local.observe(model, permit, success, latency, self.slo)
        try:
            result = self.store.observe(model, permit, success, latency, self.slo)
            if permit.probe and result and success and latency <= self.slo:
                self.known_open.discard(model)
                self.local.states.pop(model, None)
            return result
        except Exception:
            return not permit.probe

    async def run_probe(self, model, permit, call, clock=time.monotonic):
        async def measured_call():
            measurement = []
            try:
                with measure_full_completion_latency(clock) as measurement:
                    return await call
            finally:
                if measurement:
                    permit.latency_ms = measurement[0]

        if not permit.probe:
            return await measured_call()

        async def renew():
            while True:
                await asyncio.sleep(self.renewal_seconds)
                try:
                    good = self.store.renew(model, permit.token)
                except Exception:
                    good = False
                if not good:
                    permit.valid = False
                    return
        task = asyncio.create_task(renew())
        try:
            return await asyncio.wait_for(measured_call(), timeout=30)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
