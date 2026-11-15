"""Delivering "take note" from a keyboard shortcut to the recording browser.

The browser that records holds one streaming connection per study session. A
shortcut press arrives as an HTTP request on any API process; it is published
to the user's channel, picked up by the process holding the stream, and the
browser answers with an acknowledgement once the note is safely stored.

Two buses with the same interface: Redis (production: works across API
processes) and in-memory (tests, single process without Redis).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Protocol

from app.core.config import settings

#: A recorder that has not refreshed its entry for this long is gone.
RECORDER_TTL_SECONDS = 45
#: How often an idle stream wakes up (refreshes its entry, sends a heartbeat).
HEARTBEAT_SECONDS = 15
#: A stored answer is returned again for a repeated press for this long.
RESULT_TTL_SECONDS = 600
#: How long a press waits for the browser to confirm.
ACK_TIMEOUT_SECONDS = 6.0


class NotConnected(Exception):
    """Nobody is listening on the user's channel."""


class CommandBus(Protocol):
    def listen(
        self, user_id: uuid.UUID, connection_id: str, session_id: str
    ) -> AsyncIterator[dict | None]:
        """Register a recorder and yield its commands (None = heartbeat)."""
        ...

    async def recorders(self, user_id: uuid.UUID) -> dict[str, str]:
        """Live recorders: connection id → study session id."""
        ...

    async def deliver(
        self, user_id: uuid.UUID, command: dict, *, timeout: float
    ) -> dict | None:
        """Send a command; the browser's answer, or None when it did not
        confirm in time. Raises NotConnected when nobody received it."""
        ...

    async def acknowledge(
        self, user_id: uuid.UUID, invocation: str, result: dict
    ) -> None: ...

    async def result(self, user_id: uuid.UUID, invocation: str) -> dict | None: ...

    async def aclose(self) -> None: ...


class MemoryCommandBus:
    def __init__(self) -> None:
        self._queues: dict[uuid.UUID, dict[str, asyncio.Queue]] = {}
        self._sessions: dict[uuid.UUID, dict[str, str]] = {}
        self._results: dict[tuple[uuid.UUID, str], tuple[float, dict]] = {}
        self._waiters: dict[tuple[uuid.UUID, str], asyncio.Future] = {}
        self.heartbeat_seconds = float(HEARTBEAT_SECONDS)

    async def listen(self, user_id, connection_id, session_id):
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(user_id, {})[connection_id] = queue
        self._sessions.setdefault(user_id, {})[connection_id] = session_id
        try:
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), self.heartbeat_seconds)
                except TimeoutError:
                    yield None
        finally:
            self._queues.get(user_id, {}).pop(connection_id, None)
            self._sessions.get(user_id, {}).pop(connection_id, None)

    async def recorders(self, user_id):
        return dict(self._sessions.get(user_id, {}))

    async def deliver(self, user_id, command, *, timeout):
        queues = list(self._queues.get(user_id, {}).values())
        if not queues:
            raise NotConnected
        key = (user_id, command["invocation"])
        future = asyncio.get_running_loop().create_future()
        self._waiters[key] = future
        try:
            for queue in queues:
                queue.put_nowait(command)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return None
        finally:
            self._waiters.pop(key, None)

    async def acknowledge(self, user_id, invocation, result):
        key = (user_id, invocation)
        self._results[key] = (time.monotonic() + RESULT_TTL_SECONDS, result)
        waiter = self._waiters.get(key)
        if waiter is not None and not waiter.done():
            waiter.set_result(result)

    async def result(self, user_id, invocation):
        entry = self._results.get((user_id, invocation))
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]

    async def aclose(self) -> None:
        return None


class RedisCommandBus:
    def __init__(self, client) -> None:
        self._redis = client

    @staticmethod
    def _active(user_id) -> str:
        return f"study:active:{user_id}"

    @staticmethod
    def _channel(user_id) -> str:
        return f"study:cmd:{user_id}"

    @staticmethod
    def _ack(user_id, invocation) -> str:
        return f"study:ack:{user_id}:{invocation}"

    @staticmethod
    def _result(user_id, invocation) -> str:
        return f"study:result:{user_id}:{invocation}"

    async def _refresh(self, user_id, connection_id, session_id) -> None:
        key = self._active(user_id)
        await self._redis.hset(
            key, connection_id, json.dumps({"session": session_id, "at": time.time()})
        )
        await self._redis.expire(key, RECORDER_TTL_SECONDS * 2)

    async def listen(self, user_id, connection_id, session_id):
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(user_id))
        try:
            await self._refresh(user_id, connection_id, session_id)
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
                )
                await self._refresh(user_id, connection_id, session_id)
                yield json.loads(message["data"]) if message else None
        finally:
            with contextlib.suppress(Exception):
                await self._redis.hdel(self._active(user_id), connection_id)
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()

    async def recorders(self, user_id):
        key = self._active(user_id)
        live: dict[str, str] = {}
        for raw_id, raw in (await self._redis.hgetall(key)).items():
            connection_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            entry = json.loads(raw)
            if time.time() - entry["at"] > RECORDER_TTL_SECONDS:
                await self._redis.hdel(key, connection_id)
            else:
                live[connection_id] = entry["session"]
        return live

    async def deliver(self, user_id, command, *, timeout):
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._ack(user_id, command["invocation"]))
        try:
            receivers = await self._redis.publish(
                self._channel(user_id), json.dumps(command)
            )
            if not receivers:
                raise NotConnected
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=remaining
                )
                if message:
                    return json.loads(message["data"])
            # The answer may have landed between publish and subscribe races.
            return await self.result(user_id, command["invocation"])
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()

    async def acknowledge(self, user_id, invocation, result):
        body = json.dumps(result)
        await self._redis.set(
            self._result(user_id, invocation), body, ex=RESULT_TTL_SECONDS
        )
        await self._redis.publish(self._ack(user_id, invocation), body)

    async def result(self, user_id, invocation):
        raw = await self._redis.get(self._result(user_id, invocation))
        return json.loads(raw) if raw else None

    async def aclose(self) -> None:
        await self._redis.aclose()


_bus: CommandBus | None = None


def get_command_bus() -> CommandBus:
    global _bus
    if _bus is None:
        if settings.RATE_LIMIT_BACKEND == "memory":
            _bus = MemoryCommandBus()
        else:
            import redis.asyncio as aioredis

            _bus = RedisCommandBus(aioredis.from_url(settings.REDIS_URL))
    return _bus


def set_command_bus(bus: CommandBus | None) -> None:
    """Replace the process bus (tests)."""
    global _bus
    _bus = bus
