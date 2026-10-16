"""Transport-independent API validation. No provider credentials are read."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from uuid import uuid4
import json


class RouterError(Exception):
    def __init__(self, status, message, request_id=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.request_id = request_id or str(uuid4())


@dataclass(frozen=True)
class Request:
    messages: tuple
    max_tokens: int = 512
    max_cost_usd: Decimal | None = None
    deadline_ms: int = 10000
    temperature: float = 0
    request_id: str = field(default_factory=lambda: str(uuid4()))

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            raise RouterError(400, "JSON object required")
        allowed = {"messages", "max_tokens", "max_cost_usd", "deadline_ms", "temperature"}
        if set(data) - allowed:
            raise RouterError(400, "Unknown request field")
        try:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise RouterError(400, "Invalid JSON values") from None
        if len(encoded) > 32768:
            raise RouterError(400, "Input exceeds 32 KB")
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise RouterError(400, "messages must be a nonempty role/content list")
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or not isinstance(message["role"], str)
                    or message["role"] not in {"system", "user", "assistant"}
                    or not isinstance(message["content"], str) or not message["content"].strip()):
                raise RouterError(400, "Invalid message")
        max_tokens = data.get("max_tokens", 512)
        deadline = data.get("deadline_ms", 10000)
        temperature = data.get("temperature", 0)
        if type(max_tokens) is not int or not 1 <= max_tokens <= 2048:
            raise RouterError(400, "max_tokens must be 1..2048")
        if type(deadline) is not int or not 1 <= deadline <= 60000:
            raise RouterError(400, "deadline_ms must be 1..60000")
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
            raise RouterError(400, "temperature must be 0..2")
        budget = data.get("max_cost_usd")
        if budget is not None:
            try:
                if isinstance(budget, bool):
                    raise InvalidOperation
                budget = Decimal(str(budget))
                if not budget.is_finite() or budget < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise RouterError(400, "max_cost_usd must be finite and nonnegative") from None
        return cls(tuple(dict(m) for m in messages), max_tokens, budget, deadline, temperature)
