#!/usr/bin/env python3
"""Topic semantics: fan-out, backpressure policies, close, and cross-loop delivery."""
import asyncio
import sys
import threading

from aether import BLOCK, DROP_NEWEST, DROP_OLDEST, ERROR, Topic, TopicFull

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


async def fanout():
    topic = Topic("t")
    a, b = topic.subscribe(), topic.subscribe()
    check(topic.subscribers == 2, f"expected 2 subscribers, got {topic.subscribers}")
    delivered = await topic.emit({"n": 1})
    check(delivered == 2, f"emit reported {delivered} deliveries")
    check(await a.__anext__() == {"n": 1}, "subscriber a did not receive")
    check(await b.__anext__() == {"n": 1}, "subscriber b did not receive")
    a.close()
    check(topic.subscribers == 1, "close did not unregister")
    await topic.emit({"n": 2})
    check(await b.__anext__() == {"n": 2}, "surviving subscriber missed a message")


async def ordering():
    topic = Topic("t")
    sub = topic.subscribe()
    for i in range(50):
        await topic.emit(i)
    got = [await sub.__anext__() for _ in range(50)]
    check(got == list(range(50)), "messages arrived out of order")


async def waits_when_empty():
    topic = Topic("t")
    sub = topic.subscribe()

    async def emit_later():
        await asyncio.sleep(0.05)
        await topic.emit("late")

    task = asyncio.create_task(emit_later())
    item = await asyncio.wait_for(sub.__anext__(), 2)
    check(item == "late", f"woke with {item!r}")
    await task


async def policy_drop_oldest():
    topic = Topic("t", maxsize=3, policy=DROP_OLDEST)
    sub = topic.subscribe()
    for i in range(6):
        await topic.emit(i)
    got = [await sub.__anext__() for _ in range(3)]
    check(got == [3, 4, 5], f"drop_oldest kept {got}, expected [3, 4, 5]")
    check(sub.dropped == 3, f"drop_oldest counted {sub.dropped} drops, expected 3")


async def policy_drop_newest():
    topic = Topic("t", maxsize=3, policy=DROP_NEWEST)
    sub = topic.subscribe()
    for i in range(6):
        await topic.emit(i)
    got = [await sub.__anext__() for _ in range(3)]
    check(got == [0, 1, 2], f"drop_newest kept {got}, expected [0, 1, 2]")
    check(sub.dropped == 3, f"drop_newest counted {sub.dropped} drops")


async def policy_error():
    topic = Topic("t", maxsize=2, policy=ERROR)
    topic.subscribe()
    await topic.emit(1)
    await topic.emit(2)
    try:
        await topic.emit(3)
        failures.append("error policy did not raise when full")
    except TopicFull:
        pass


async def policy_block():
    topic = Topic("t", maxsize=2, policy=BLOCK)
    sub = topic.subscribe()
    await topic.emit(1)
    await topic.emit(2)

    started = asyncio.Event()

    async def emit_third():
        started.set()
        await topic.emit(3)

    task = asyncio.create_task(emit_third())
    await started.wait()
    await asyncio.sleep(0.05)
    check(not task.done(), "block policy did not wait on a full subscriber")
    check(await sub.__anext__() == 1, "block policy lost the first message")
    await asyncio.wait_for(task, 2)
    rest = [await sub.__anext__() for _ in range(2)]
    check(rest == [2, 3], f"block policy delivered {rest}, expected [2, 3]")
    check(sub.dropped == 0, "block policy dropped a message")


async def closing_ends_iteration():
    topic = Topic("t")
    sub = topic.subscribe()
    await topic.emit("a")

    async def drain():
        return [item async for item in sub]

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    sub.close()
    got = await asyncio.wait_for(task, 2)
    check(got == ["a"], f"drained {got}, expected buffered items then a clean stop")


async def context_manager():
    topic = Topic("t")
    async with topic.subscribe() as sub:
        await topic.emit("x")
        check(await sub.__anext__() == "x", "context-managed subscription missed a message")
    check(topic.subscribers == 0, "context manager did not close the subscription")


def cross_loop():
    """The M3 premise: subscribers on different event loops all receive.

    Each thread runs its own loop, exactly as Aether's worker loops do.
    """
    topic = Topic("cross")
    received: dict[int, list] = {}
    ready = threading.Barrier(4)
    done = threading.Barrier(4)

    def consumer(index: int):
        async def run():
            sub = topic.subscribe()
            ready.wait()
            got = []
            for _ in range(3):
                got.append(await asyncio.wait_for(sub.__anext__(), 5))
            received[index] = got
            sub.close()

        asyncio.run(run())
        done.wait()

    threads = [threading.Thread(target=consumer, args=(i,), daemon=True) for i in range(3)]
    for t in threads:
        t.start()
    ready.wait()

    async def produce():
        for i in range(3):
            n = await topic.emit(f"msg-{i}")
            check(n == 3, f"emit reached {n} subscribers, expected 3")

    asyncio.run(produce())
    done.wait()
    for t in threads:
        t.join(timeout=5)

    check(len(received) == 3, f"only {len(received)} of 3 loops received anything")
    for index, got in received.items():
        check(got == ["msg-0", "msg-1", "msg-2"],
              f"loop {index} received {got}")


async def main_async():
    for coro in (fanout, ordering, waits_when_empty, policy_drop_oldest,
                 policy_drop_newest, policy_error, policy_block,
                 closing_ends_iteration, context_manager):
        try:
            await asyncio.wait_for(coro(), 10)
        except Exception as e:
            failures.append(f"{coro.__name__} raised {type(e).__name__}: {e}")


def main() -> None:
    asyncio.run(main_async())
    print(f"single-loop checks: {'PASS' if not failures else 'FAIL'}")
    before = len(failures)
    try:
        cross_loop()
    except Exception as e:
        failures.append(f"cross_loop raised {type(e).__name__}: {e}")
    print(f"cross-loop fan-out: {'PASS' if len(failures) == before else 'FAIL'}")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
