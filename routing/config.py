from decimal import Decimal
from pathlib import Path
import json
import hashlib
from copy import deepcopy
from api.contracts import RouterError

ROOT = Path(__file__).resolve().parents[1]


class Config:
    def __init__(self, path=None, data=None):
        self.data = deepcopy(data) if data is not None else json.loads(
            Path(path or ROOT / "config/models.json").read_text())
        self.models = self.data["models"]
        self.chains = self.data["chains"]
        self.provider_version = self.data.get("provider_config_version", "1")
        if type(self.benchmark_deadline_ms) is not int or not 1 <= self.benchmark_deadline_ms <= 60000:
            raise ValueError("benchmark_deadline_ms must be an integer in 1..60000")
        if type(self.benchmark_max_tokens) is not int or not 1 <= self.benchmark_max_tokens <= 2048:
            raise ValueError("benchmark_max_tokens must be an integer in 1..2048")
        if type(self.judge_calibration_timeout_s) is not int or not 1 <= self.judge_calibration_timeout_s <= 60:
            raise ValueError("judge_calibration_timeout_s must be an integer in 1..60")
        for chain in self.chains.values():
            for alias in chain:
                self.model(alias)

    @property
    def slo_ms(self):
        return self.data.get("slo_ms", 20000)

    @property
    def benchmark_deadline_ms(self):
        return self.data.get("benchmark_deadline_ms", 60000)

    @property
    def benchmark_max_tokens(self):
        return self.data.get("benchmark_max_tokens", 1024)

    @property
    def judge_calibration_timeout_s(self):
        return self.data.get("judge_calibration_timeout_s", 30)

    @property
    def version(self):
        # Include prices so a missed manual bump cannot reuse cached costs.
        prices = {}
        for alias in self.models:
            model = self.model(alias)
            prices[alias] = {field: model[field] for field in
                             ("input_per_million", "output_per_million", "tiers") if field in model}
        digest = hashlib.sha256(json.dumps(prices, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        return f"{self.data['price_version']}:{digest}"

    def model(self, alias):
        if alias not in self.models:
            raise RouterError(422, "Unknown model")
        model = self.models[alias]
        try:
            if "tiers" in model:
                tiers = model["tiers"]
                if not isinstance(tiers, list) or not tiers:
                    raise ValueError
                previous = 0
                for index, tier in enumerate(tiers):
                    if not isinstance(tier, dict):
                        raise ValueError
                    maximum = tier["max_input_tokens"]
                    if maximum is None:
                        if index != len(tiers) - 1:
                            raise ValueError
                    elif type(maximum) is not int or maximum <= previous:
                        raise ValueError
                    else:
                        previous = maximum
                    self._validate_prices(tier)
                if tiers[-1]["max_input_tokens"] is not None:
                    raise ValueError
            else:
                self._validate_prices(model)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise RouterError(422, "Missing or invalid model price") from None
        return model

    @staticmethod
    def _validate_prices(prices):
        for field in ("input_per_million", "output_per_million"):
            if isinstance(prices[field], bool):
                raise ValueError
            value = Decimal(prices[field])
            if not value.is_finite() or value < 0:
                raise ValueError

    @staticmethod
    def _price(model, input_tokens):
        if "tiers" not in model:
            return model
        return next(tier for tier in model["tiers"]
                    if tier["max_input_tokens"] is None or input_tokens <= tier["max_input_tokens"])

    @staticmethod
    def _cost(price, input_tokens, output_tokens):
        return (Decimal(input_tokens) * Decimal(price["input_per_million"]) +
                Decimal(output_tokens) * Decimal(price["output_per_million"])) / 1000000

    def cost(self, alias, input_tokens, output_tokens):
        model = self.model(alias)
        # One tier prices the entire request, including output, using actual input usage.
        return self._cost(self._price(model, input_tokens), input_tokens, output_tokens)

    def reserve_cost(self, alias, request):
        # UTF-8 byte count plus framing is a conservative bound for supported tokenizers.
        tokens = sum(len(m["content"].encode()) + len(m["role"]) + 16 for m in request.messages) + 64
        model = self.model(alias)
        price = self._price(model, tokens)
        worst = self._cost(price, tokens, request.max_tokens)
        if "tiers" in model:
            for index, tier in enumerate(model["tiers"][:-1]):
                if tier is price and tokens * 10 >= tier["max_input_tokens"] * 9:
                    # At/within 10% below a boundary, reserve the next tier's rates.
                    worst = max(worst, self._cost(model["tiers"][index + 1],
                                                  tokens, request.max_tokens))
                    break
        return worst
