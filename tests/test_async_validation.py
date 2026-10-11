import asyncio

from app.foundation.validation import (
    AsyncResponseValidator,
    GenerationGuard,
    MockMiniLLMValidator,
    ValidationDecision,
)


def run(coro):
    return asyncio.run(coro)


def test_regex_fast_path_and_block():
    validator = AsyncResponseValidator()
    assert run(validator.validate("Hello, friend.")).allowed
    blocked = run(validator.validate("I will give you a diagnosis and medicine."))
    assert blocked.blocked
    assert blocked.source == "regex"


def test_async_mini_llm_gate_allows_safe_question():
    validator = AsyncResponseValidator(mini=MockMiniLLMValidator())
    decision = run(validator.validate("What is a zebra and where does it live?"))
    assert decision.allowed
    assert decision.source == "mock_mini_llm"
    assert decision.latency_ms >= 0


def test_low_confidence_or_timeout_fails_closed():
    class SlowValidator:
        async def validate(self, text, context=()):
            await asyncio.sleep(0.2)
            return ValidationDecision("allow", True, "late", 1.0, "slow")

    validator = AsyncResponseValidator(mini=SlowValidator(), timeout_ms=10)
    decision = run(validator.validate("Why is the sky blue?"))
    assert decision.blocked
    assert "timeout" in decision.reason


def test_parallel_validation_does_not_serialize_chunks():
    async def scenario():
        validator = AsyncResponseValidator(mini=MockMiniLLMValidator(delay_s=0.02))
        started = asyncio.get_running_loop().time()
        results = await asyncio.gather(*(validator.validate("Why is the sky blue?") for _ in range(20)))
        elapsed = asyncio.get_running_loop().time() - started
        return results, elapsed

    results, elapsed = run(scenario())
    assert all(item.allowed for item in results)
    assert elapsed < 0.12


def test_generation_guard_rejects_stale_output_and_clears_queue():
    async def scenario():
        guard = GenerationGuard()
        generation = await guard.start()
        queue = asyncio.Queue()
        for value in range(5):
            await queue.put(value)
        cleared = await GenerationGuard.clear_queue(queue)
        await guard.cancel()
        return generation, cleared, guard.current(generation), queue.empty()

    generation, cleared, current, empty = run(scenario())
    assert generation == 1
    assert cleared == 5
    assert not current
    assert empty
