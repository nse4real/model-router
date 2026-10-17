import hashlib
import json
import time


def key(request, model_id, price_version, provider_parameters=None, provider_version="1"):
    canonical = dict(messages=list(request.messages), model=model_id, price_version=price_version,
                     max_tokens=request.max_tokens, temperature=request.temperature,
                     provider_parameters=provider_parameters or {}, provider_version=provider_version)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class MemoryCache:
    def __init__(self):
        self.values = {}
        self.available = True

    def get(self, name):
        if not self.available:
            raise ConnectionError("cache_unavailable")
        return self.values.get(name)

    def put(self, name, value):
        if not self.available:
            raise ConnectionError("cache_unavailable")
        self.values[name] = value

    def clear(self):
        self.values.clear()


class MemoryRate:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.counts = {}
        self.available = True

    def allow(self, model, limit):
        if not self.available:
            return True
        name = (model, int(self.clock() // 60))
        self.counts[name] = self.counts.get(name, 0) + 1
        return self.counts[name] <= limit


class RedisCache:
    def __init__(self, client, namespace="router:cache:", ttl=3600):
        self.client, self.namespace, self.ttl = client, namespace, ttl

    def get(self, name):
        value = self.client.get(self.namespace + name)
        return json.loads(value) if value else None

    def put(self, name, value):
        self.client.set(self.namespace + name, json.dumps(value), ex=self.ttl)

    def clear(self):
        names = list(self.client.scan_iter(match=self.namespace + "*"))
        if names:
            self.client.delete(*names)


class RedisRate:
    SCRIPT = """
    local n = redis.call('INCR', KEYS[1])
    if n == 1 then redis.call('EXPIRE', KEYS[1], 70) end
    return n
    """

    def __init__(self, client, clock=time.time):
        self.client, self.clock = client, clock

    def allow(self, model, limit):
        try:
            name = f"router:rate:{model}:{int(self.clock() // 60)}"
            return self.client.eval(self.SCRIPT, 1, name) <= limit
        except Exception:
            return True  # Redis fails open; durable spend admission is still mandatory.
