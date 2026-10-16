"""Mock by default. The opt-in LiteLLM adapter is intentionally unverified."""
import asyncio
from dataclasses import dataclass


class ProviderError(Exception):
    def __init__(self, status=500, detail="provider failure", usage=None):
        super().__init__(detail)
        self.status = status
        self.usage = usage

    @property
    def retryable(self):
        return self.status in (408, 429) or self.status >= 500 or self.status == 0


@dataclass
class Output:
    content: str
    input_tokens: int
    output_tokens: int
    stop_reason: str = "stop"


class MockProvider:
    mode = "mock"

    def __init__(self, actions=None):
        self.actions = actions or {}
        self.calls = []

    async def complete(self, model, request, timeout):
        self.calls.append(model["id"])
        actions = self.actions.get(model["id"], [])
        action = actions.pop(0) if actions else None
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, (float, int)):
            await asyncio.sleep(action)
        if isinstance(action, Output):
            return action
        content = "[mock] " + request.messages[-1]["content"][:160]
        tokens = min(request.max_tokens, max(1, len(content.encode()) // 4))
        return Output(content[:tokens * 4], sum(len(m["content"].encode()) // 4 + 1
                                               for m in request.messages), tokens)


class LiteLLMProvider:
    mode = "real-unverified"

    def __init__(self, enabled=False):
        if not enabled:
            raise ValueError("Real providers require explicit opt-in")

    async def complete(self, model, request, timeout):
        import litellm
        try:
            response = await litellm.acompletion(
                model=model["id"], messages=list(request.messages),
                max_tokens=request.max_tokens, temperature=request.temperature,
                timeout=timeout, num_retries=0, **model.get("litellm", {}))
        except Exception as exc:
            raise ProviderError(getattr(exc, "status_code", 0), str(exc)) from None
        choice = response.choices[0]
        return Output(choice.message.content or "", response.usage.prompt_tokens,
                      response.usage.completion_tokens, choice.finish_reason)


class MockJudgeProvider(MockProvider):
    """Fixed diagnostic fixture; never evidence of real answer quality."""
    async def complete(self, model, request, timeout):
        self.calls.append(model["id"])
        return Output('{"score":3}', sum(len(m["content"].encode()) // 4 + 1
                                         for m in request.messages), 5)
