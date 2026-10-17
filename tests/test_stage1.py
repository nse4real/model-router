import unittest
from api.contracts import Request, RouterError
from classifier.scorer import score
from routing.config import Config
from routing.policy import ordered_models
from fallback.providers import MockProvider


class ContractTests(unittest.TestCase):
    def test_defaults(self):
        r = Request.parse({"messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual((r.max_tokens, r.deadline_ms), (512, 10000))
        self.assertEqual(r.temperature, 1)
        self.assertEqual(Request(messages=r.messages).temperature, 1)
        self.assertTrue(r.request_id)

    def test_invalid(self):
        for data in ({}, {"messages": []}, {"messages": [{"role": "bad", "content": "x"}]},
                     {"messages": [{"role": [], "content": "x"}]},
                     {"messages": [{"role": "user", "content": "x" * 32769}]}):
            with self.subTest(data=str(data)[:80]), self.assertRaises(RouterError) as e:
                Request.parse(data)
            self.assertEqual(e.exception.status, 400)

    def test_limits(self):
        for name, value in (("max_tokens", 2049), ("max_tokens", True),
                            ("deadline_ms", 60001), ("max_cost_usd", -1),
                            ("max_cost_usd", "NaN"), ("temperature", float("nan")),
                            ("temperature", -0.1), ("temperature", 2.1)):
            with self.subTest(name=name, value=value), self.assertRaises(RouterError):
                Request.parse({"messages": [{"role": "user", "content": "x"}], name: value})

    def test_temperature_range_endpoints(self):
        for temperature in (0, 2):
            with self.subTest(temperature=temperature):
                r = Request.parse({"messages": [{"role": "user", "content": "x"}],
                                   "temperature": temperature})
                self.assertEqual(r.temperature, temperature)


class ClassifierTests(unittest.TestCase):
    def request(self, text):
        return Request.parse({"messages": [{"role": "user", "content": text}]})

    def test_deterministic(self):
        r = self.request("prove this algorithm step by step")
        self.assertEqual(score(r), score(r))

    def test_features(self):
        self.assertEqual(score(self.request("x" * 500))[2]["length_score"], 1)
        self.assertGreater(score(self.request("reason code"))[2]["keyword_density"], .1)
        self.assertEqual(score(self.request("calculate"))[2]["task_type"], "maths")
        self.assertTrue(score(self.request("```python"))[2]["code_block"])

    def test_boundaries(self):
        for text, tier in (("hello", "simple"), ("code", "medium"),
                           ("reason code compare filler", "medium"),
                           ("code reason ```", "complex")):
            self.assertEqual(score(self.request(text))[0], tier)
        for length, tier in ((499, "simple"), (500, "medium"), (999, "medium"),
                             (1000, "complex")):
            self.assertEqual(score(self.request("x" * length), (1, 2))[0], tier)

    def test_config_and_policy(self):
        c = Config()
        self.assertEqual(ordered_models(c, "simple")[0], "cheap")
        self.assertEqual(ordered_models(c, "simple", True)[0], "mid")
        with self.assertRaises(RouterError) as e:
            c.model("missing")
        self.assertEqual(e.exception.status, 422)
        self.assertGreater(c.reserve_cost("cheap", self.request("hello")), 0)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock(self):
        p = MockProvider()
        result = await p.complete(Config().model("cheap"),
                                  Request.parse({"messages": [{"role": "user", "content": "hi"}]}), 1)
        self.assertIn("[mock]", result.content)
        self.assertEqual(len(p.calls), 1)
