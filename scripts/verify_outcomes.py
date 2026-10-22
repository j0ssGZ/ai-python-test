"""Reconcile an isolated accepted batch with durable processing evidence and API state."""

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from notification_service.extraction import Notification, validate_semantics  # noqa: E402

SNAPSHOT = """
import json, os, sqlite3, sys
from pathlib import Path
path = Path(os.environ['AI_NOTIFY_DATABASE_PATH']).resolve()
with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5) as db:
    db.row_factory=sqlite3.Row
    rows = [dict(r) for r in db.execute(
        'SELECT * FROM requests WHERE created_at>=? AND created_at<=?',
        (float(sys.argv[1]),float(sys.argv[2])))]
    attempts = [dict(r) for r in db.execute('''SELECT attempts.* FROM attempts
        JOIN requests ON requests.id=attempts.request_id
        WHERE requests.created_at>=? AND requests.created_at<=?''',
        (float(sys.argv[1]),float(sys.argv[2])))]
    print(json.dumps({'requests':rows,'attempts':attempts}))
"""


def snapshot(since: float, until: float) -> dict:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            SNAPSHOT,
            str(since),
            str(until),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {}
    return {
        "count": len(ordered),
        "mean": statistics.mean(ordered),
        **{
            f"p{p}": ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))]
            for p in (50, 95, 99)
        },
        "max": ordered[-1],
    }


def assess(data: dict, expected_count: int) -> dict:
    rows, attempts = data["requests"], data["attempts"]
    states = Counter(r["status"] for r in rows)
    categories = Counter(r["failure_category"] for r in rows if r["status"] == "failed")
    codes = Counter(r["failure_code"] for r in rows if r["status"] == "failed")
    errors = []
    if expected_count <= 0 or len(rows) != expected_count:
        errors.append("accepted_count_mismatch_or_empty_batch")
    if states["queued"] + states["processing"]:
        errors.append("pending_work")
    by_id = {}
    for attempt in attempts:
        by_id.setdefault(attempt["request_id"], []).append(attempt)
    correct_sent = 0
    for row in rows:
        history = by_id.get(row["id"], [])
        if row["activated_at"] is None:
            errors.append("unactivated_request")
        if row["status"] == "failed" and not row["failure_category"]:
            errors.append("unclassified_failure")
        notify = [a for a in history if a["stage"] == "notify"]
        extraction = [a for a in history if a["stage"] == "extract" and a["outcome"] == "succeeded"]
        if notify and (len(extraction) != 1 or not row["extracted_json"]):
            errors.append("notification_without_validated_extraction")
        if any(a["failure_category"] == "refusal" for a in history) and notify:
            errors.append("notification_after_refusal")
        if row["status"] == "sent":
            confirmations = [
                a
                for a in notify
                if a["outcome"] == "succeeded" and a["provider_id"] == row["provider_id"]
            ]
            if len(confirmations) != 1 or not row["provider_id"]:
                errors.append("missing_or_duplicate_confirmation")
            try:
                candidate = Notification.model_validate_json(row["extracted_json"])
                validate_semantics(row["user_input"], candidate)
                correct_sent += 1
            except Exception:
                errors.append("sent_semantic_mismatch")
    timings = {}
    for stage in ("extract", "notify"):
        timings[stage + "_attempt_seconds"] = percentiles(
            [
                a["finished_at"] - a["started_at"]
                for a in attempts
                if a["stage"] == stage and a["finished_at"] is not None
            ]
        )
    timings["terminal_seconds"] = percentiles(
        [r["updated_at"] - r["created_at"] for r in rows if r["status"] in ("sent", "failed")]
    )
    timings["first_extraction_wait_seconds"] = percentiles(
        [
            a["started_at"] - r["activated_at"]
            for r in rows
            for a in by_id.get(r["id"], [])
            if a["stage"] == "extract" and a["number"] == 1 and r["activated_at"] is not None
        ]
    )
    return {
        "expected_accepted": expected_count,
        "total": len(rows),
        "states": dict(states),
        "failure_categories": dict(categories),
        "failure_codes": dict(codes),
        "attempt_counts": dict(Counter(a["stage"] for a in attempts)),
        "attempt_failure_categories": dict(
            Counter(a["failure_category"] for a in attempts if a["failure_category"])
        ),
        "repaired_extractions": sum(r["repaired"] == 1 for r in rows),
        "semantically_verified_sent": correct_sent,
        "timings": timings,
        "processing_span_seconds": max((r["updated_at"] for r in rows), default=0)
        - min((r["created_at"] for r in rows), default=0),
        "integrity_errors": dict(Counter(errors)),
        "integrity_passed": not errors,
        "all_sent": bool(rows) and states["sent"] == len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=float, required=True)
    parser.add_argument("--until", type=float, default=1e20)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    backlog = []
    while True:
        data = snapshot(args.since, args.until)
        states = Counter(row["status"] for row in data["requests"])
        sample = {"at": time.time(), "states": dict(states)}
        backlog.append(sample)
        print(json.dumps(sample), flush=True)
        if (
            len(data["requests"]) == args.expected_count
            and not states["queued"]
            and not states["processing"]
        ) or time.monotonic() >= deadline:
            break
        time.sleep(args.interval)
    report = assess(data, args.expected_count)

    def check_api(row):
        try:
            with urllib.request.urlopen(
                args.base_url.rstrip("/") + "/v1/requests/" + row["id"], timeout=10
            ) as response:
                return response.status == 200 and json.load(response) == {
                    "id": row["id"],
                    "status": row["status"],
                }
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        report["api_matches"] = sum(executor.map(check_api, data["requests"]))
    report["integrity_passed"] &= report["api_matches"] == args.expected_count
    report.update({"since": args.since, "until": args.until, "backlog_samples": backlog})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "backlog_samples"}), flush=True)
    return 0 if report["integrity_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
