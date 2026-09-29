import asyncio

import pytest

from app.errors import ServiceBusyError
from app.scraping.host_limiter import HostLimiter

HOST = "example.com"
SHORT_WAIT_SECONDS = 0.01


@pytest.mark.anyio
async def test_requests_beyond_the_host_limit_are_rejected():
    limiter = HostLimiter(max_per_host=1, wait_timeout_seconds=SHORT_WAIT_SECONDS)

    async with limiter.slot(HOST):
        with pytest.raises(ServiceBusyError, match=HOST):
            async with limiter.slot(HOST):
                pass


@pytest.mark.anyio
async def test_other_hosts_are_not_affected():
    limiter = HostLimiter(max_per_host=1, wait_timeout_seconds=SHORT_WAIT_SECONDS)
    entered = []

    async with limiter.slot(HOST):
        async with limiter.slot("other.example"):
            entered.append("other.example")

    assert entered == ["other.example"]


@pytest.mark.anyio
async def test_waiting_request_gets_the_slot_when_it_is_released():
    limiter = HostLimiter(max_per_host=1, wait_timeout_seconds=1)
    order = []

    async def first():
        async with limiter.slot(HOST):
            await asyncio.sleep(0)
            order.append("first")

    async def second():
        async with limiter.slot(HOST):
            order.append("second")

    await asyncio.gather(first(), second())

    assert order == ["first", "second"]
