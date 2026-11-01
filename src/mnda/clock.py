"""Time sources. Every timestamp in a run comes from exactly one Clock.

RealClock
    Monotonic time and ordinary asyncio sleeps, for real downloads and the loopback tier.

VirtualClock
    Deterministic simulated time for the virtual tier, which produces the headline numbers.
    Code runs on a VirtualEventLoop: whenever no callback is ready, the loop jumps straight to
    the next timer instead of waiting. An hour of simulated download takes as long as the CPU
    work inside it, and the same program produces the same timeline on every run.

How the virtual loop works
    asyncio's BaseEventLoop._run_once() computes how long it may wait for the next timer and
    passes that to ``self._selector.select(timeout)``, which is the only place a real loop
    blocks. VirtualEventLoop installs a selector that advances the virtual clock by ``timeout``
    and returns immediately. ``select(None)`` means nothing is ready and no timer is pending,
    which in a world with no threads and no real I/O can never resolve, so it raises
    VirtualDeadlock instead of hanging.

Strict mode (the default) turns rule 1 of docs/01-design.md into a runtime check: while a
virtual run is in progress, calling time.monotonic / perf_counter / sleep (and their _ns
variants), starting a thread, or using an executor raises VirtualTimeViolation. The first
violation is recorded and re-raised when the run ends even if the code under test swallowed
it with a broad ``except``. time.time() is left alone because the logging module uses it for
record timestamps; it must never be used for event timestamps either, and the static
architecture test (added with sched/) covers that.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Executor
from contextlib import contextmanager, nullcontext
from typing import Any, Protocol, TypeVar, TypeVarTuple, runtime_checkable

__all__ = [
    "Clock",
    "RealClock",
    "VirtualClock",
    "VirtualDeadlock",
    "VirtualEventLoop",
    "VirtualTimeViolation",
]

T = TypeVar("T")
Ts = TypeVarTuple("Ts")

# Two timers this close together fire in the same loop iteration. It must be > 0: the loop
# advances exactly to a timer's deadline and then runs handles with ``when < now + resolution``.
# The stock value is the monotonic clock's resolution, 15.6 ms on Windows, which would make
# virtual timers fire up to 15.6 ms early.
_VIRTUAL_RESOLUTION = 1e-9

_TRAPPED_TIME_FUNCS = (
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "sleep",
)


class VirtualTimeViolation(RuntimeError):
    """Code running in virtual time touched real time, a thread, or an executor."""


class VirtualDeadlock(RuntimeError):
    """Every task is waiting and no timer is pending, so virtual time can never advance."""


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic timeline. Only differences between values are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Monotonic real time. Matches loop.time() on the standard asyncio loops."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _VirtualSelector:
    def __init__(self, loop: VirtualEventLoop) -> None:
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        if timeout is None:
            raise VirtualDeadlock(
                "virtual loop has nothing ready and no timer pending: every task is awaiting "
                "something that cannot happen in virtual time (a future nobody resolves, a "
                "thread, or real I/O)"
            )
        if timeout > 0:
            self._loop._advance(timeout)
        return []

    def close(self) -> None:
        pass


class VirtualEventLoop(asyncio.BaseEventLoop):
    """An asyncio loop whose time() is simulated and never waits in real time.

    It has no sockets, pipes, subprocesses, or executor: those methods raise, and in this
    project the virtual tier must not use them anyway.
    """

    def __init__(self, start: float = 0.0) -> None:
        super().__init__()
        self._now = float(start)
        self._selector = _VirtualSelector(self)
        self._clock_resolution = _VIRTUAL_RESOLUTION

    def time(self) -> float:
        return self._now

    def _advance(self, seconds: float) -> None:
        self._now += seconds

    # Hooks BaseEventLoop requires from concrete loops.
    def _process_events(self, event_list: list[Any]) -> None:
        pass

    def _write_to_self(self) -> None:
        # The select() call never blocks, so nothing ever needs waking up.
        pass

    def run_in_executor(
        self,
        executor: Executor | None,
        func: Callable[[*Ts], T],
        *args: *Ts,
    ) -> asyncio.Future[T]:
        raise VirtualTimeViolation(
            "run_in_executor / asyncio.to_thread in virtual time: threads run in real time "
            "and would break determinism"
        )

    def set_default_executor(self, executor: Any) -> None:
        raise VirtualTimeViolation("executors are not available in virtual time")


@contextmanager
def _forbid_real_time(record: Callable[[VirtualTimeViolation], None]) -> Iterator[None]:
    saved_funcs = {name: getattr(time, name) for name in _TRAPPED_TIME_FUNCS}
    saved_start = threading.Thread.start

    def make_trap(what: str) -> Callable[..., Any]:
        def trap(*_args: Any, **_kwargs: Any) -> Any:
            exc = VirtualTimeViolation(
                f"{what} called during a virtual-time run; use the injected Clock instead"
            )
            record(exc)
            raise exc

        return trap

    for name in _TRAPPED_TIME_FUNCS:
        setattr(time, name, make_trap(f"time.{name}()"))
    setattr(threading.Thread, "start", make_trap("threading.Thread.start()"))  # noqa: B010
    try:
        yield
    finally:
        for name, func in saved_funcs.items():
            setattr(time, name, func)
        setattr(threading.Thread, "start", saved_start)  # noqa: B010


class VirtualClock:
    """Simulated time. Use it once, as ``clock.run(main(clock))``.

    ``now()`` can be read before the run (it returns ``start``) and after it (it returns the
    time at which the run finished).
    """

    def __init__(self, start: float = 0.0, *, strict: bool = True) -> None:
        self._loop = VirtualEventLoop(start)
        self._strict = strict
        self._used = False

    @property
    def loop(self) -> VirtualEventLoop:
        return self._loop

    def now(self) -> float:
        return self._loop.time()

    async def sleep(self, seconds: float) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("VirtualClock.sleep() awaited outside its own virtual loop")
        await asyncio.sleep(seconds)

    def run(self, main: Coroutine[Any, Any, T]) -> T:
        """Run ``main`` to completion in virtual time and return its result.

        Tasks still pending when ``main`` returns are cancelled, as with asyncio.run().
        """
        if self._used:
            main.close()
            raise RuntimeError("a VirtualClock runs once; create a new one per run")
        self._used = True

        violations: list[VirtualTimeViolation] = []
        guard = _forbid_real_time(violations.append) if self._strict else nullcontext()
        try:
            with guard, asyncio.Runner(loop_factory=lambda: self._loop) as runner:
                result = runner.run(main)
        except VirtualTimeViolation:
            raise
        except BaseException as exc:
            if violations:
                raise violations[0] from exc
            raise
        if violations:
            # Code under test caught the violation and carried on; the run is still invalid.
            raise violations[0]
        return result
