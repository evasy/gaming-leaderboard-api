#!/usr/bin/env python3
"""A small load probe: seed a board, then measure read/write latency.

Not a replacement for a real load test, but enough to answer "does rank lookup
actually stay flat as the board grows?" with numbers instead of assertions.

    python scripts/bench.py --base-url http://localhost:8080 --players 5000
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from collections.abc import Awaitable, Callable

import httpx


async def _measure(
    label: str, calls: list[Callable[[], Awaitable[httpx.Response]]], concurrency: int
) -> None:
    latencies: list[float] = []
    failures = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def run(call: Callable[[], Awaitable[httpx.Response]]) -> None:
        nonlocal failures
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await call()
                if response.status_code >= 400:
                    failures += 1
            except Exception:
                failures += 1
                return
            latencies.append((time.perf_counter() - started) * 1000)

    wall_start = time.perf_counter()
    await asyncio.gather(*(run(c) for c in calls))
    wall = time.perf_counter() - wall_start

    if not latencies:
        print(f"{label:<28} all {len(calls)} requests failed")
        return

    ordered = sorted(latencies)

    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))]

    print(
        f"{label:<28} n={len(calls):<6} "
        f"rps={len(calls) / wall:>8.0f}  "
        f"p50={statistics.median(ordered):>6.2f}ms  "
        f"p95={pct(0.95):>6.2f}ms  "
        f"p99={pct(0.99):>6.2f}ms  "
        f"max={ordered[-1]:>7.2f}ms  "
        f"errors={failures}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--game", default=f"bench-{int(time.time())}")
    parser.add_argument("--players", type=int, default=2000)
    parser.add_argument("--reads", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0, limits=limits) as client:
        health = await client.get("/readyz")
        health.raise_for_status()
        print(f"target : {args.base_url}")
        print(f"backend: {health.json()['backend']}")
        print(f"game   : {args.game}\n")

        users = [f"u{i:06d}" for i in range(args.players)]

        await _measure(
            "write  POST /scores",
            [
                (
                    lambda u=u: client.post(  # type: ignore[misc]
                        f"/v1/games/{args.game}/scores",
                        json={"user_id": u, "score": random.randint(1, 1_000_000)},
                    )
                )
                for u in users
            ],
            args.concurrency,
        )

        await _measure(
            "read   GET  /leaderboard",
            [
                (lambda: client.get(f"/v1/games/{args.game}/leaderboard?limit=100"))
                for _ in range(args.reads)
            ],
            args.concurrency,
        )

        await _measure(
            "read   GET  /users/{id}",
            [
                (
                    lambda u=random.choice(users): client.get(  # type: ignore[misc]
                        f"/v1/games/{args.game}/users/{u}"
                    )
                )
                for _ in range(args.reads)
            ],
            args.concurrency,
        )

        await _measure(
            "read   GET  /users/{id}/context",
            [
                (
                    lambda u=random.choice(users): client.get(  # type: ignore[misc]
                        f"/v1/games/{args.game}/users/{u}/context?radius=5"
                    )
                )
                for _ in range(args.reads)
            ],
            args.concurrency,
        )


if __name__ == "__main__":
    asyncio.run(main())
