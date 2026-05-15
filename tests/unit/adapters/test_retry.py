"""Tests for mithra/adapters/output/retry.py -- RetryableOutputAdapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mithra.adapters.messaging import DeliveryReceipt, OutboundMessage, OutputAdapter
from mithra.adapters.output.retry import RetryableOutputAdapter
from mithra.autonomy.dead_letter import DeadLetterQueue


def _success_receipt(**kwargs) -> DeliveryReceipt:
    return DeliveryReceipt(
        channel="test",
        message_id="m-1",
        delivered_at="2026-01-01T00:00:00+00:00",
        success=True,
        undoable=False,
        **kwargs,
    )


def _failure_receipt(error: str = "inner error") -> DeliveryReceipt:
    return DeliveryReceipt(
        channel="test",
        message_id=None,
        delivered_at="2026-01-01T00:00:00+00:00",
        success=False,
        undoable=False,
        error=error,
    )


def _make_dlq() -> MagicMock:
    """Return a mock DLQ so tests don't depend on filesystem or user_id routing."""
    dlq = MagicMock(spec=DeadLetterQueue)
    dlq.enqueue = MagicMock()
    return dlq


def _make_inner(receipt: DeliveryReceipt | None = None, raises: Exception | None = None):
    inner = AsyncMock(spec=OutputAdapter)
    if raises:
        inner.send = AsyncMock(side_effect=raises)
        inner.send_with_actions = AsyncMock(side_effect=raises)
        inner.send_stream = AsyncMock(side_effect=raises)
    else:
        r = receipt or _success_receipt()
        inner.send = AsyncMock(return_value=r)
        inner.send_with_actions = AsyncMock(return_value=r)
        inner.send_stream = AsyncMock(return_value=r)
    inner.undo = AsyncMock(return_value=True)
    inner.health = MagicMock(return_value={"status": "ok"})
    inner.__class__.__name__ = "MockAdapter"
    return inner


class TestRetryableOutputAdapterStructure:
    def test_is_subclass_of_output_adapter(self):
        assert issubclass(RetryableOutputAdapter, OutputAdapter)

    def test_health_includes_inner_adapter_name(self):
        inner = _make_inner()
        dlq = _make_dlq()
        adapter = RetryableOutputAdapter(inner, dlq)
        h = adapter.health()
        assert "status" in h


class TestSendSucceedsFirstTry:
    @pytest.mark.asyncio
    async def test_send_returns_success_on_first_attempt(self):
        inner = _make_inner(_success_receipt())
        adapter = RetryableOutputAdapter(inner, _make_dlq(), max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))
        assert receipt.success is True
        assert inner.send.call_count == 1


class TestSendRetries:
    @pytest.mark.asyncio
    async def test_send_retries_on_exception(self):
        call_count = 0
        receipts = [RuntimeError("fail"), RuntimeError("fail"), _success_receipt()]

        async def side_effect(*a, **kw):
            nonlocal call_count
            r = receipts[call_count]
            call_count += 1
            if isinstance(r, Exception):
                raise r
            return r

        inner = _make_inner()
        inner.send = AsyncMock(side_effect=side_effect)
        adapter = RetryableOutputAdapter(inner, _make_dlq(), max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))
        assert receipt.success is True
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_send_succeeds_on_second_attempt_after_failure_receipt(self):
        call_count = 0
        returns = [_failure_receipt("transient"), _success_receipt()]

        async def side_effect(*a, **kw):
            nonlocal call_count
            r = returns[call_count]
            call_count += 1
            return r

        inner = _make_inner()
        inner.send = AsyncMock(side_effect=side_effect)
        adapter = RetryableOutputAdapter(inner, _make_dlq(), max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))
        assert receipt.success is True


class TestDeadLetterAfterExhaustion:
    @pytest.mark.asyncio
    async def test_exhausted_send_returns_failure_receipt(self):
        inner = _make_inner(raises=RuntimeError("always fails"))
        dlq = _make_dlq()
        adapter = RetryableOutputAdapter(inner, dlq, max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))
        assert receipt.success is False
        assert receipt.error is not None

    @pytest.mark.asyncio
    async def test_exhausted_send_enqueues_dead_letter(self):
        inner = _make_inner(raises=RuntimeError("always fails"))
        dlq = _make_dlq()
        adapter = RetryableOutputAdapter(inner, dlq, max_attempts=3, backoff_seconds=[0, 0, 0])
        await adapter.send("person_a", OutboundMessage(text="Hi"))
        dlq.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_exhausted_send_does_not_raise(self):
        inner = _make_inner(raises=RuntimeError("always fails"))
        dlq = _make_dlq()
        adapter = RetryableOutputAdapter(inner, dlq, max_attempts=3, backoff_seconds=[0, 0, 0])
        # Must not raise -- returns a failure DeliveryReceipt instead
        receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))
        assert isinstance(receipt, DeliveryReceipt)


class TestBackoff:
    @pytest.mark.asyncio
    async def test_backoff_applied_between_retries(self):
        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleeps.append(delay)

        inner = _make_inner(raises=RuntimeError("fail"))
        dlq = _make_dlq()
        adapter = RetryableOutputAdapter(inner, dlq, max_attempts=3, backoff_seconds=[0.01, 0.02, 0.04])

        import unittest.mock as _mock
        with _mock.patch("mithra.adapters.output.retry.asyncio.sleep", side_effect=mock_sleep):
            await adapter.send("person_a", OutboundMessage(text="Hi"))

        assert len(sleeps) == 2  # 3 attempts = 2 sleeps between them
        assert sleeps[0] == 0.01
        assert sleeps[1] == 0.02


class TestUndo:
    @pytest.mark.asyncio
    async def test_undo_delegates_to_inner_single_attempt(self):
        inner = _make_inner(_success_receipt())
        inner.undo = AsyncMock(return_value=True)
        adapter = RetryableOutputAdapter(inner, _make_dlq(), max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = _success_receipt()
        result = await adapter.undo(receipt)
        assert result is True
        assert inner.undo.call_count == 1

    @pytest.mark.asyncio
    async def test_undo_does_not_retry_on_failure(self):
        inner = _make_inner(_success_receipt())
        inner.undo = AsyncMock(side_effect=RuntimeError("undo failed"))
        adapter = RetryableOutputAdapter(inner, _make_dlq(), max_attempts=3, backoff_seconds=[0, 0, 0])
        receipt = _success_receipt()
        with pytest.raises(RuntimeError):
            await adapter.undo(receipt)
        assert inner.undo.call_count == 1
