"""Mock by default. The opt-in LiteLLM adapter is intentionally unverified."""
import asyncio
import os
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
    resolved_model: str | None = None


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
                                               for m in request.messages), tokens,
                      resolved_model=model["id"])


class LiteLLMProvider:
    mode = "real-unverified"

    def __init__(self, enabled=False):
        if not enabled or os.environ.get("ROUTER_CONFIRM_REAL_SPEND") != "YES":
            raise ValueError("Real providers require --real and ROUTER_CONFIRM_REAL_SPEND=YES")
        self.enabled = enabled

    async def complete(self, model, request, timeout):
        if not self.enabled or os.environ.get("ROUTER_CONFIRM_REAL_SPEND") != "YES":
            raise ValueError("Real provider confirmation is no longer present")
        import litellm
        parameters = dict(model.get("litellm", {}))
        # Configuration cannot override retry ownership, timeout or request bounds.
        parameters.update(model=model["id"], messages=list(request.messages),
                          max_tokens=request.max_tokens, temperature=request.temperature,
                          timeout=timeout, num_retries=0)
        try:
            # LiteLLM creates its provider coroutine in an executor thread.
            # Cancellation during that handoff can orphan the returned coroutine.
            # Keep its owner alive and drain it before propagating cancellation.
            # The SDK timeout and disabled retries above still apply.
            call = asyncio.create_task(litellm.acompletion(**parameters))
            try:
                response = await asyncio.shield(call)
            except asyncio.CancelledError:
                while not call.done():
                    try:
                        await asyncio.shield(call)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not call.cancelled():
                    call.exception()  # Retrieve late failures as well as successes.
                raise
        except Exception as exc:
            raise ProviderError(getattr(exc, "status_code", 0), str(exc)) from None
        choice = response.choices[0]
        return Output(choice.message.content or "", response.usage.prompt_tokens,
                      response.usage.completion_tokens, choice.finish_reason,
                      resolved_model=getattr(response, "model", None))


class MockJudgeProvider(MockProvider):
    """Fixed diagnostic fixture; never evidence of real answer quality."""
    async def complete(self, model, request, timeout):
        self.calls.append(model["id"])
        return Output('{"score":3}', sum(len(m["content"].encode()) // 4 + 1
                                         for m in request.messages), 5,
                      resolved_model=model["id"])


def real_provider(real=False):
    """The only factory used by explicitly opted-in API/benchmark entry points."""
    return LiteLLMProvider(enabled=real)
