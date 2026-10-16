from decimal import Decimal
from pathlib import Path
import json
from api.contracts import RouterError

ROOT = Path(__file__).resolve().parents[1]


class Config:
    def __init__(self, path=None):
        self.data = json.loads(Path(path or ROOT / "config/models.json").read_text())
        self.models = self.data["models"]
        self.chains = self.data["chains"]
        self.version = self.data["price_version"]
        for chain in self.chains.values():
            for alias in chain:
                self.model(alias)

    def model(self, alias):
        if alias not in self.models:
            raise RouterError(422, "Unknown model")
        model = self.models[alias]
        for price in ("input_per_million", "output_per_million"):
            try:
                value = Decimal(model[price])
                if not value.is_finite() or value < 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError, ArithmeticError):
                raise RouterError(422, "Missing or invalid model price") from None
        return model

    def cost(self, alias, input_tokens, output_tokens):
        model = self.model(alias)
        return (Decimal(input_tokens) * Decimal(model["input_per_million"]) +
                Decimal(output_tokens) * Decimal(model["output_per_million"])) / 1000000

    def reserve_cost(self, alias, request):
        # UTF-8 byte count plus framing is a conservative upper bound for supported tokenizers.
        tokens = sum(len(m["content"].encode()) + len(m["role"]) + 16 for m in request.messages)
        return self.cost(alias, tokens + 64, request.max_tokens)
