from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select

from app.database import SessionLocal
from app.domain import OutboxEvent, OutboxStatus
from app.settings import settings

WORKER_ID = os.getenv("HOSTNAME", "outbox-worker")


def backoff_seconds(attempt: int) -> int:
    return min(300, 2 ** max(1, attempt))


def webhook_for_event(event_type: str) -> str:
    if event_type == "message.outbound.requested":
        return settings.n8n_outbound_webhook_url
    if event_type.startswith("product.") and settings.n8n_product_webhook_url:
        return settings.n8n_product_webhook_url
    return settings.n8n_publish_webhook_url


async def claim_batch() -> list[OutboxEvent]:
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.outbox_lock_seconds)
    async with SessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status.in_([OutboxStatus.pending, OutboxStatus.processing]),
                    or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                    or_(OutboxEvent.locked_at.is_(None), OutboxEvent.locked_at < stale_before),
                )
                .order_by(OutboxEvent.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(settings.outbox_batch_size)
            )
            events = list(result.scalars().all())
            for event in events:
                event.status = OutboxStatus.processing
                event.locked_at = now
                event.locked_by = WORKER_ID
            return events


async def mark_delivered(event_id) -> None:
    async with SessionLocal() as session:
        event = await session.get(OutboxEvent, event_id)
        if event:
            event.status = OutboxStatus.delivered
            event.locked_at = None
            event.locked_by = None
            event.last_error = None
            await session.commit()


async def mark_failure(event_id, error: str) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        event = await session.get(OutboxEvent, event_id)
        if not event:
            return
        event.attempt_count += 1
        event.last_error = error[:4000]
        event.locked_at = None
        event.locked_by = None
        if event.attempt_count >= settings.outbox_max_attempts:
            event.status = OutboxStatus.dead_letter
            event.dead_lettered_at = now
            event.next_attempt_at = None
        else:
            event.status = OutboxStatus.pending
            event.next_attempt_at = now + timedelta(seconds=backoff_seconds(event.attempt_count))
        await session.commit()


async def deliver(event: OutboxEvent, client: httpx.AsyncClient) -> None:
    payload = {
        "event_id": str(event.id),
        "workspace_id": str(event.workspace_id),
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "idempotency_key": event.idempotency_key,
        "payload": json.loads(event.payload_json),
    }
    response = await client.post(
        webhook_for_event(event.event_type),
        json=payload,
        headers={"X-Automation-Secret": settings.n8n_webhook_secret},
    )
    response.raise_for_status()


async def run_once() -> int:
    events = await claim_batch()
    if not events:
        return 0
    async with httpx.AsyncClient(timeout=30) as client:
        for event in events:
            try:
                await deliver(event, client)
            except Exception as exc:
                await mark_failure(event.id, f"{type(exc).__name__}: {exc}")
            else:
                await mark_delivered(event.id)
    return len(events)


async def main() -> None:
    while True:
        processed = await run_once()
        if processed == 0:
            await asyncio.sleep(settings.outbox_poll_seconds)


if __name__ == "__main__":
    asyncio.run(main())
