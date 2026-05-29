"""Dependency-light local load test (fallback for environments without k6).

Drives concurrent POST /v1/query traffic for a fixed duration and reports
throughput + latency percentiles. Same intent as load/query_load.js.

Usage (start the server with raised limits first):
    USQL_RATELIMIT_RPM_MULTIPLIER=100000 uvicorn src.main:app --port 8099 &
    python load/load_local.py --duration 30 --concurrency 64
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.auth import mint_token  # noqa: E402

SQL = "SELECT pr.id, pr.state FROM github.pull_requests pr WHERE pr.state = 'open' LIMIT 10"


async def worker(client, url, headers, deadline, lat, errors):
    payload = {"sql": SQL}
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        try:
            r = await client.post(url, json=payload, headers=headers)
            lat.append(time.monotonic() - t0)
            if r.status_code != 200:
                errors[0] += 1
        except Exception:
            errors[0] += 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8099")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--concurrency", type=int, default=64)
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {mint_token('eng', 'acme-corp')}"}
    url = f"{args.base}/v1/query"
    lat: list[float] = []
    errors = [0]

    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency * 2)
    async with httpx.AsyncClient(timeout=10, limits=limits) as client:
        deadline = time.monotonic() + args.duration
        start = time.monotonic()
        await asyncio.gather(*[
            worker(client, url, headers, deadline, lat, errors)
            for _ in range(args.concurrency)
        ])
        elapsed = time.monotonic() - start

    lat.sort()
    n = len(lat)
    def pct(p): return lat[min(n - 1, int(p / 100 * n))] * 1000 if n else 0
    print(f"requests:    {n}")
    print(f"duration:    {elapsed:.1f}s")
    print(f"throughput:  {n / elapsed:.0f} req/s")
    print(f"errors:      {errors[0]}")
    print(f"latency p50: {pct(50):.1f} ms")
    print(f"latency p95: {pct(95):.1f} ms")
    print(f"latency p99: {pct(99):.1f} ms")


if __name__ == "__main__":
    asyncio.run(main())
