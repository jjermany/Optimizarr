from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from queue import Empty, Queue
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4

from app.core.database import SessionLocal
from app.services.monitoring_service import get_system_metrics


@dataclass(slots=True)
class Subscription:
    client_id: str
    queue: Queue[dict[str, Any]]


class RealtimeBroker:
    def __init__(self) -> None:
        self._subscriptions: dict[str, Queue[dict[str, Any]]] = {}
        self._lock = Lock()
        self._stop_event = Event()
        self._metrics_thread: Thread | None = None
        self._last_metrics_payload: dict[str, Any] | None = None
        self._job_progress_last_emit: dict[int, float] = {}
        self._job_last_signature: dict[int, str] = {}

    def start(self) -> None:
        if self._metrics_thread and self._metrics_thread.is_alive():
            return
        self._stop_event.clear()
        self._metrics_thread = Thread(target=self._metrics_loop, name='realtime-metrics', daemon=True)
        self._metrics_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._metrics_thread and self._metrics_thread.is_alive():
            self._metrics_thread.join(timeout=2)
        self._metrics_thread = None

    def subscribe(self) -> Subscription:
        client_id = uuid4().hex
        client_queue: Queue[dict[str, Any]] = Queue(maxsize=100)
        with self._lock:
            self._subscriptions[client_id] = client_queue
        return Subscription(client_id=client_id, queue=client_queue)

    def unsubscribe(self, client_id: str) -> None:
        with self._lock:
            self._subscriptions.pop(client_id, None)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        envelope = {
            'type': event_type,
            'data': data,
            'timestamp': time.time(),
        }
        with self._lock:
            subscribers = list(self._subscriptions.values())

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(envelope)
            except Exception:
                continue

    def publish_notification(self, message: str, level: str = 'info') -> None:
        self.publish('notification', {'message': message, 'level': level})

    def publish_library_update(self, action: str, library: dict[str, Any]) -> None:
        self.publish('library_update', {'action': action, 'library': library})

    def publish_metrics_update(self, payload: dict[str, Any]) -> None:
        self.publish('metrics_update', payload)

    def publish_system_event(self, event: str, **data: Any) -> None:
        payload = {'event': event}
        payload.update(data)
        self.publish('system_event', payload)

    def publish_job_update(self, job_payload: dict[str, Any], *, throttle_progress: bool = True) -> None:
        job_id = int(job_payload['id'])
        status = str(job_payload.get('status') or '')
        progress = int(job_payload.get('progress_percent') or 0)
        signature = json.dumps({'status': status, 'progress_percent': progress}, sort_keys=True)
        now = time.monotonic()

        if throttle_progress and status == 'running':
            last_emit = self._job_progress_last_emit.get(job_id, 0.0)
            if now - last_emit < 1.0:
                last_signature = self._job_last_signature.get(job_id)
                if last_signature == signature:
                    return

        self._job_progress_last_emit[job_id] = now
        self._job_last_signature[job_id] = signature
        self.publish('job_update', job_payload)

    def _metrics_loop(self) -> None:
        while not self._stop_event.is_set():
            db = SessionLocal()
            try:
                payload = get_system_metrics(db)
            finally:
                db.close()

            if payload != self._last_metrics_payload:
                self._last_metrics_payload = payload
                self.publish_metrics_update(payload)

            self._stop_event.wait(timeout=1.0)


broker = RealtimeBroker()
async def next_message(subscription: Subscription, timeout_seconds: float = 15.0) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(subscription.queue.get, True, timeout_seconds)
    except Empty:
        return None
