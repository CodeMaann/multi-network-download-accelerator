from __future__ import annotations

import asyncio
import contextlib
import random
import threading
import time

import pytest

from mnda.clock import (
    Clock,
    RealClock,
    VirtualClock,
    VirtualDeadlock,
    VirtualTimeViolation,
)

# --- virtual time semantics ---------------------------------------------------------------


def test_sleep_advances_virtual_time_exactly() -> None:
    clock = VirtualClock()

    async def main() -> float:
        await clock.sleep(2.5)
        return clock.now()

    assert clock.run(main()) == 2.5
    assert clock.now() == 2.5


def test_start_offset() -> None:
    clock = VirtualClock(start=100.0)
    assert clock.now() == 100.0

    async def main() -> None:
        await clock.sleep(1.0)

    clock.run(main())
    assert clock.now() == 101.0


def test_long_virtual_sleep_costs_no_real_time() -> None:
    # Three days is longer than asyncio's 24 h cap on a single select() timeout.
    clock = VirtualClock()

    async def main() -> None:
        await clock.sleep(3 * 86_400)

    t0 = time.perf_counter()
    clock.run(main())
    assert time.perf_counter() - t0 < 1.0
    assert clock.now() == 3 * 86_400


def test_timers_fire_in_deadline_order_ties_in_scheduling_order() -> None:
    clock = VirtualClock()
    log: list[tuple[str, float]] = []

    async def sleeper(name: str, delay: float) -> None:
        await clock.sleep(delay)
        log.append((name, clock.now()))

    async def main() -> None:
        await asyncio.gather(
            sleeper("c", 0.3), sleeper("a1", 0.1), sleeper("b", 0.2), sleeper("a2", 0.1)
        )

    clock.run(main())
    assert log == [("a1", 0.1), ("a2", 0.1), ("b", 0.2), ("c", 0.3)]


def test_timer_does_not_fire_early() -> None:
    # Regression for the stock 15.6 ms Windows clock resolution: with it, a timer due at
    # 15 ms would already fire when the loop reaches 12 ms.
    clock = VirtualClock()
    fired: list[float] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.015, lambda: fired.append(clock.now()))
        await clock.sleep(0.012)
        assert fired == []
        await clock.sleep(0.010)

    clock.run(main())
    assert fired == [0.015]


def test_wait_for_and_timeout_use_virtual_time() -> None:
    clock = VirtualClock()

    async def main() -> list[float]:
        stamps = []
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(clock.sleep(10.0), timeout=2.0)
        stamps.append(clock.now())
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await clock.sleep(10.0)
        stamps.append(clock.now())
        return stamps

    assert clock.run(main()) == [2.0, 2.5]


def test_paced_queue_pipeline() -> None:
    clock = VirtualClock()

    async def producer(q: asyncio.Queue[int | None]) -> None:
        for i in range(4):
            await clock.sleep(0.5)
            await q.put(i)
        await q.put(None)

    async def consumer(q: asyncio.Queue[int | None]) -> list[tuple[int, float]]:
        got = []
        while (item := await q.get()) is not None:
            got.append((item, clock.now()))
        return got

    async def main() -> list[tuple[int, float]]:
        q: asyncio.Queue[int | None] = asyncio.Queue(maxsize=1)
        _, got = await asyncio.gather(producer(q), consumer(q))
        return got

    assert clock.run(main()) == [(0, 0.5), (1, 1.0), (2, 1.5), (3, 2.0)]


def test_pending_tasks_are_cancelled_when_main_returns() -> None:
    clock = VirtualClock()
    cancelled_at: list[float] = []

    async def forever() -> None:
        try:
            while True:
                await clock.sleep(1.0)
        except asyncio.CancelledError:
            cancelled_at.append(clock.now())
            raise

    async def main() -> None:
        asyncio.get_running_loop().create_task(forever())
        await clock.sleep(5.5)

    clock.run(main())
    assert cancelled_at == [5.5]


def test_same_seed_same_timeline() -> None:
    def simulate(seed: int) -> list[tuple[int, float]]:
        clock = VirtualClock()
        rng = random.Random(seed)
        log: list[tuple[int, float]] = []

        async def worker(wid: int) -> None:
            for _ in range(50):
                await clock.sleep(rng.expovariate(10.0))
                log.append((wid, clock.now()))

        async def main() -> None:
            await asyncio.gather(*(worker(w) for w in range(4)))

        clock.run(main())
        return log

    assert simulate(7) == simulate(7)
    assert simulate(7) != simulate(8)


def test_ordinary_exceptions_propagate() -> None:
    clock = VirtualClock()

    async def main() -> None:
        await clock.sleep(1.0)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        clock.run(main())


def test_clock_runs_once() -> None:
    clock = VirtualClock()

    async def main() -> None:
        pass

    clock.run(main())
    with pytest.raises(RuntimeError, match="runs once"):
        clock.run(main())


# --- deadlock and strict-mode violations --------------------------------------------------


def test_waiting_on_nothing_is_a_deadlock_not_a_hang() -> None:
    clock = VirtualClock()

    async def main() -> None:
        await asyncio.get_running_loop().create_future()

    with pytest.raises(VirtualDeadlock):
        clock.run(main())


@pytest.mark.parametrize("name", ["monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"])
def test_reading_real_time_is_a_violation(name: str) -> None:
    clock = VirtualClock()

    async def main() -> None:
        getattr(time, name)()

    with pytest.raises(VirtualTimeViolation, match=name):
        clock.run(main())


def test_blocking_sleep_is_a_violation() -> None:
    clock = VirtualClock()

    async def main() -> None:
        time.sleep(0.001)  # noqa: ASYNC251

    with pytest.raises(VirtualTimeViolation, match="sleep"):
        clock.run(main())


def test_to_thread_is_a_violation() -> None:
    clock = VirtualClock()

    async def main() -> None:
        await asyncio.to_thread(lambda: None)

    with pytest.raises(VirtualTimeViolation, match="executor"):
        clock.run(main())


def test_starting_a_thread_is_a_violation() -> None:
    clock = VirtualClock()

    async def main() -> None:
        threading.Thread(target=lambda: None).start()

    with pytest.raises(VirtualTimeViolation, match="Thread"):
        clock.run(main())


def test_swallowed_violation_is_still_reported() -> None:
    clock = VirtualClock()

    async def main() -> str:
        with contextlib.suppress(Exception):
            time.monotonic()
        return "carried on"

    with pytest.raises(VirtualTimeViolation):
        clock.run(main())


def test_guards_are_removed_after_a_run_even_after_a_violation() -> None:
    clock = VirtualClock()

    async def main() -> None:
        time.monotonic()

    with pytest.raises(VirtualTimeViolation):
        clock.run(main())

    assert time.monotonic() > 0
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()


def test_non_strict_mode_allows_real_time() -> None:
    clock = VirtualClock(strict=False)

    async def main() -> float:
        return time.monotonic()

    assert clock.run(main()) > 0


# --- protocol and real clock -------------------------------------------------------------


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(RealClock(), Clock)
    assert isinstance(VirtualClock(), Clock)


def test_real_clock_is_monotonic_and_sleeps() -> None:
    clock = RealClock()
    a = clock.now()
    asyncio.run(clock.sleep(0))
    assert clock.now() >= a


def test_virtual_sleep_outside_its_loop_is_rejected() -> None:
    clock = VirtualClock()
    with pytest.raises(RuntimeError, match="outside its own virtual loop"):
        asyncio.run(clock.sleep(0.1))


def test_throughput_of_the_virtual_loop() -> None:
    # 200k timer wakeups is roughly a 4 GB download in 256 KB pieces over 12 connections
    # with a few timers per piece. It should take seconds, not minutes.
    clock = VirtualClock()

    async def worker() -> None:
        for _ in range(20_000):
            await clock.sleep(0.001)

    async def main() -> None:
        await asyncio.gather(*(worker() for _ in range(10)))

    t0 = time.perf_counter()
    clock.run(main())
    assert time.perf_counter() - t0 < 15.0
    assert clock.now() == pytest.approx(20.0)
