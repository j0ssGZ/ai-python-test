"""Compare bounded worker settings with the unchanged real provider.

Run alone, after tests and image builds. COMPOSE_FILE supports external overrides.
No database records are deleted, and every batch must drain before the next one.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from create_batch import run
from verify_outcomes import snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--configs", nargs="+", default=["8:2", "16:2", "16:4", "24:4"])
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        configs = args.configs if repeat % 2 == 0 else list(reversed(args.configs))
        for config in configs:
            ai, notify = map(int, config.split(":"))
            retained = snapshot(0, 1e20)["requests"]
            if any(r["status"] in ("queued", "processing") for r in retained):
                raise RuntimeError("Previous backlog must drain before benchmarking")
            name = f"ai-{ai}-notify-{notify}-repeat-{repeat + 1}"
            env = {
                **os.environ,
                "AI_NOTIFY_AI_WORKERS": str(ai),
                "AI_NOTIFY_NOTIFY_WORKERS": str(notify),
            }
            subprocess.run(
                ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "--wait", "app"],
                env=env,
                check=True,
            )
            # Exclude conservative startup quota cooldown from steady-state comparison.
            time.sleep(16)
            with urllib.request.urlopen(args.base_url + "/health/ready", timeout=10) as response:
                assert response.status == 200
            print(json.dumps({"event": "batch_start", "name": name}), flush=True)
            batch = asyncio.run(run(args.base_url, args.count))
            batch["configuration"] = {"ai_workers": ai, "notify_workers": notify}
            (args.output_dir / f"{name}-input.json").write_text(json.dumps(batch, indent=2) + "\n")
            output = args.output_dir / f"{name}-outcomes.json"
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("verify_outcomes.py")),
                    "--since",
                    str(batch["since"]),
                    "--until",
                    str(batch["until"]),
                    "--expected-count",
                    str(batch["accepted"]),
                    "--base-url",
                    args.base_url,
                    "--interval",
                    "1",
                    "--timeout",
                    "300",
                    "--output",
                    str(output),
                ],
                check=True,
            )
            report = json.loads(output.read_text())
            if report["failure_categories"].get("semantic", 0):
                raise RuntimeError("Verifiable workload unexpectedly failed semantic validation")
            print(
                json.dumps(
                    {
                        "event": "batch_complete",
                        "name": name,
                        "states": report["states"],
                        "span": report["processing_span_seconds"],
                    }
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
