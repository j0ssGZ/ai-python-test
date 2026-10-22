"""Create an independent, semantically verifiable real-provider workload."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx


async def run(base_url: str, count: int) -> dict:
    semaphore = asyncio.Semaphore(20)
    latencies = {"create": [], "activate": []}
    expected_by_id = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=10, trust_env=False) as client:

        async def create(index):
            channel = "email" if index % 2 == 0 else "sms"
            recipient = (
                f"recipient{index}@example.org" if channel == "email" else str(610000000 + index)
            )
            user_input = f"Send {channel} to {recipient}: order number {index} is ready."
            async with semaphore:
                start = time.perf_counter()
                response = await client.post("/v1/requests", json={"user_input": user_input})
                latencies["create"].append(time.perf_counter() - start)
                response.raise_for_status()
                assert response.status_code == 201
                request_id = response.json()["id"]
                expected_by_id[request_id] = {
                    "to": recipient,
                    "message": f"order number {index} is ready.",
                    "type": channel,
                }
                start = time.perf_counter()
                activated = await client.post(f"/v1/requests/{request_id}/process")
                latencies["activate"].append(time.perf_counter() - start)
                activated.raise_for_status()
                assert activated.status_code == 202
                return request_id

        since = time.time()
        ids = await asyncio.gather(*[create(i) for i in range(count)])
        return {
            "since": since,
            "until": time.time(),
            "accepted": len(ids),
            "ids": ids,
            "expected_by_id": expected_by_id,
            "http_latency_seconds": latencies,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 1000:
        parser.error("count must be between 1 and 1000")
    result = asyncio.run(run(args.base_url, args.count))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in ("ids", "http_latency_seconds", "expected_by_id")
            }
        )
    )


if __name__ == "__main__":
    main()
