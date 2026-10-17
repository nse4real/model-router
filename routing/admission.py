"""One model health/rate admission contract for inference and evaluation."""
from routing.latency import Health
from cache.exact import MemoryRate


class Admission:
    def __init__(self, health=None, rate=None):
        self.health = health or Health()
        self.rate = rate or MemoryRate()

    def acquire(self, model):
        permit = self.health.acquire(model['id'])
        if permit is None:
            return None
        if not self.rate.allow(model['id'], model.get('rpm', 300)):
            self.health.release(model['id'], permit)
            return None
        return permit


def shared_admission(slo_ms=20000):
    """Production entry points share the API's Redis model state and RPM keys."""
    import os
    import redis
    from cache.exact import RedisRate
    from routing.latency import RedisHealthStore
    client = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                                  socket_timeout=.2, socket_connect_timeout=.2, decode_responses=True)
    return Admission(Health(RedisHealthStore(client), slo_ms), RedisRate(client))
