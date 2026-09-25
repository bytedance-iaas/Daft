"""The Daemon's CPU pool (design doc 04 §2.3, D54): one slot per CPU worker, shared fairly."""
from __future__ import annotations

import random
import threading

import pytest

from daemon.orchestr import cpupool
from daemon.orchestr.cpupool import CpuPool


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_slots_are_counted_and_given_back():
    pool = CpuPool(4)
    assert pool.acquire("a", 3) == 3
    assert pool.acquire("b", 3) == 1                  # only one left
    assert pool.used == 4 and pool.peak == 4
    pool.release("a", 2)
    assert (pool.used, pool.held("a")) == (2, 1)
    pool.sync("b", 0)                                 # b's episode is done
    assert pool.used == 1 and pool.held("b") == 0
    pool.release("a", 5)                              # never more than it holds
    assert pool.used == 0
    assert pool.acquire("a", 0) == 0 and pool.used == 0
    assert CpuPool(0).size == 1


def test_sync_follows_the_episodes_actually_out():
    pool = CpuPool(8)
    pool.acquire("a", 5)
    pool.sync("a", 2)                                 # three finished or went back to ready
    assert (pool.used, pool.held("a")) == (2, 2)
    pool.leave("a")
    assert pool.used == 0 and pool.snapshot()["held"] == {}


def test_a_lone_task_may_use_every_slot():
    pool = CpuPool(30)
    assert pool.acquire("a", 30) == 30
    pool.sync("a", 29)
    assert pool.acquire("a", 1) == 1                  # nobody waits: it takes the freed slot


def test_a_waiting_task_gets_the_freed_slots_until_it_has_its_share():
    clock = Clock()
    pool = CpuPool(30, clock=clock)
    assert pool.acquire("a", 30) == 30
    assert pool.acquire("b", 30) == 0                 # b waits
    for step in range(15):
        pool.sync("a", 29 - step)                     # one of a's episodes is done ...
        clock.now += 0.01
        assert pool.acquire("a", 1) == 0              # ... a is above its share: it may not refill
        assert pool.acquire("b", 30 - step) == 1      # b is below: it gets it
    assert (pool.held("a"), pool.held("b")) == (15, 15)
    pool.sync("a", 14)
    clock.now += 0.01
    # both at their share now: a freed slot goes to whoever asks
    assert pool.acquire("a", 1) == 1
    assert pool.used == 30


def test_a_task_that_stops_asking_stops_holding_the_others_back():
    clock = Clock()
    pool = CpuPool(10, clock=clock)
    pool.acquire("a", 10)
    assert pool.acquire("b", 4) == 0                  # b waits (share 5)
    pool.sync("a", 9)
    assert pool.acquire("a", 1) == 0                  # held back for b
    clock.now += cpupool.WAIT_S + 0.1                 # b's work went elsewhere (its queue ran dry)
    assert pool.acquire("a", 1) == 1


def test_three_tasks_share_thirty_slots():
    clock = Clock()
    pool = CpuPool(30, clock=clock)
    held = {"a": 0, "b": 0, "c": 0}
    held["a"] = pool.acquire("a", 30)
    rng = random.Random(3)
    for _ in range(400):
        clock.now += 0.01
        key = rng.choice("abc")
        if held[key] and rng.random() < 0.5:
            held[key] -= 1
            pool.sync(key, held[key])
        held[key] += pool.acquire(key, 30 - held[key])
        for other in "abc":                           # the others keep asking too
            if other != key:
                held[other] += pool.acquire(other, 30 - held[other])
        assert pool.used == sum(held.values()) <= 30
    assert all(8 <= n <= 12 for n in held.values()), held


def test_many_threads_never_exceed_the_pool():
    pool = CpuPool(6)
    over = []

    def worker(key):
        rng = random.Random(key)
        mine = 0
        for _ in range(2000):
            if mine and rng.random() < 0.5:
                mine -= 1
                pool.sync(key, mine)
            else:
                mine += pool.acquire(key, rng.randint(1, 3))
            if pool.used > pool.size:
                over.append(pool.used)
        pool.leave(key)

    threads = [threading.Thread(target=worker, args=(k,)) for k in "abcd"]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not over and pool.used == 0 and pool.peak <= 6


def test_a_block_waits_for_its_share_and_gives_it_back():
    clock = Clock()
    pool = CpuPool(10, clock=clock)
    pool.acquire("pipeline", 10)
    waits = []

    def step(_s):
        clock.now += 0.01
        # the other task's episodes finish one by one; it may only refill up to its share
        pool.sync("pipeline", pool.held("pipeline") - 1)
        pool.acquire("pipeline", 1)

    with pool.block("retry", 8, on_wait=lambda: waits.append(1), sleep=step) as got:
        assert got == 5                               # min(want, share of 10 between two)
        assert pool.held("retry") == 5 and pool.used <= 10
    assert waits == [1] and pool.held("retry") == 0


def test_a_block_takes_what_it_needs_and_at_least_one():
    pool = CpuPool(30)
    with pool.block("r", 3) as got:
        assert got == 3
    with pool.block("r", 0) as got:
        assert got == 1
    assert pool.used == 0


def test_a_stop_while_a_block_waits_gives_back_what_it_gathered():
    clock = Clock()
    pool = CpuPool(4, clock=clock)
    pool.acquire("a", 4)
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:                           # it holds one of its two by now
            assert pool.held("r") == 1
            raise KeyboardInterrupt("stop")

    def step(_s):
        clock.now += 0.01
        pool.sync("a", pool.held("a") - 1)            # one slot frees per turn; "r" takes it

    with pytest.raises(KeyboardInterrupt):
        with pool.block("r", 4, check=check, sleep=step):
            pass
    assert pool.held("r") == 0 and pool.used == pool.held("a") == 3
