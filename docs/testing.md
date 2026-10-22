# Testing and reproducible evidence

Use Python 3.12, `uv`, Docker and Docker Compose. Run from the repository root.
The service needs no paid model or external account. Its AI endpoint is the supplied
mock, which does not follow the system prompt. Do not interpret these tests as a
production-model prompt evaluation.

## Deterministic suite

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r app/requirements-dev.txt
.venv/bin/pytest -q --cov=notification_service --cov-branch
.venv/bin/ruff check app scripts
.venv/bin/ruff format --check app scripts
docker compose config --quiet
git diff --check
```

Tests cover public contracts, real temporary SQLite databases, migration, atomic
capacity, repeated activation, cancellation during BEGIN/rollback, competing
connections, leases, stale owners, final-attempt exhaustion, actual subprocess
SIGKILL, worker limits and shutdown. Clocks, jitter and external transports are
controlled. They do not depend on the real provider's random decisions or timing.
Parser tests cover noisy/invalid JSON, aliases, refusals, missing fields and
adversarial outputs; semantic tests verify complete message correspondence, bounded
ASCII case restoration and rejection of missing/truncated content. Integration
tests traverse both required HTTP
endpoints using a deterministic HTTPX transport.

## Start the real stack

```sh
docker compose up -d --build --wait app influxdb grafana
curl --fail http://127.0.0.1:5000/health/ready
```

The application shares the provider's network namespace. Keep internal port 5000,
the extraction/notify endpoints at 127.0.0.1:3001, and k6's target `provider:5000`.
The application runs as UID 10001 and persists its database under `/data` in the
named `request-data` volume. Never use `down -v` to reset a failing benchmark.

If host port 5000 is occupied, create an ignored override before starting:

```sh
mkdir -p docs/evidence
cat > docs/evidence/compose.override.yaml <<'YAML'
services:
  provider:
    ports: !override
      - "3001:3001"
      - "15000:5000"
YAML
export COMPOSE_FILE=docker-compose.yaml:docs/evidence/compose.override.yaml
export TEST_BASE_URL=http://127.0.0.1:15000
docker compose up -d --build --wait app influxdb grafana
```

This requires Compose support for `!override`. Without the override set
`TEST_BASE_URL=http://127.0.0.1:5000`. All scripts inherit `COMPOSE_FILE`.
Do not change the shared network namespace/port configuration during a benchmark.

## Independent verifiable workload and worker comparison

The following workload uses new email/SMS recipients and explicitly delimited,
lowercase messages. It exercises real extraction and notification without the
deterministic message-corruption defects in the original k6 corpus. It does not
change production behavior or bypass the AI endpoint.

```sh
.venv/bin/python scripts/benchmark_workers.py \
  --base-url "$TEST_BASE_URL" --count 60 --repeats 2 \
  --output-dir docs/evidence/workers
```

The script recreates only the app with four worker configurations, preserves
all database records, waits out startup quota cooldown, and refuses to start
with a pending batch. The second repetition reverses configuration order. Each
batch's accepted IDs and HTTP durations are retained, followed by terminal/API
reconciliation. No pytest, builds, provider-check or other producer should overlap
the measurement. The output directory should be new for each experiment.

Each generated input file also contains `expected_by_id`, an independent intended
recipient/message/channel oracle. The verifier's `--expectations <input-file>`
option compares persisted sent payloads directly with that oracle, in addition to
revalidating semantics and checking provider confirmations. This catches a shared
bug in the production validator and its reuse by the verification tool. Never
derive expected messages from the extracted payload being evaluated.

The mock remains stochastic. Small batches and a shared developer machine do not
establish a universally optimal configuration. Report throughput, latency, backlog,
failure families, attempts, and actual deliveries together.

## Original unchanged k6 scenario

After selecting worker counts, recreate the app with that configuration, wait for
readiness and cooldown, and ensure no previous backlog remains. Then:

```sh
mkdir -p docs/evidence/official
python3 -c 'import time; print(time.time())' > docs/evidence/official/since
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/docs/evidence/official:/results" load-test \
  --quiet --summary-export=/results/k6-summary.json
python3 -c 'import time; print(time.time())' > docs/evidence/official/until
```

Inspect the exported metrics. The complete scenario has seven checks and three
HTTP requests per successful iteration. Require no failed checks and no HTTP
failures; a zero exit code is insufficient because the original script defines
no thresholds. Use the actual iteration/acceptance count as `EXPECTED_COUNT`:

```sh
.venv/bin/python scripts/verify_outcomes.py \
  --since "$(cat docs/evidence/official/since)" \
  --until "$(cat docs/evidence/official/until)" \
  --expected-count "$EXPECTED_COUNT" --base-url "$TEST_BASE_URL" \
  --output docs/evidence/official/outcomes.json
```

When checks or creation fail, do not blindly equate iterations with accepted work.
Reconcile the available acceptance evidence and report any ambiguity. The verifier
requires a nonempty count, reads SQLite snapshots without modifying records, waits
for terminal outcomes, verifies every public status, checks the extraction/delivery
history and revalidates every sent candidate against its source.

`integrity_passed` means evidence is consistent. It does **not** mean all requests
were sent or all were understood. `all_sent`, terminal categories, attempt failures
and semantically verified sends are separate report fields. A benchmark that
rejects every request must be reported as such. Known mock corruption is described
in [semantics.md](semantics.md). The original k6 check accepts `failed` and does not
verify message correctness or eventual completion.

Provider access logs can corroborate calls/statuses and notification `trace_id`.
The mock's extraction hit metric includes non-success responses and may lose points
when InfluxDB is unavailable. It cannot replace durable outcome evidence. Nor does
an acknowledgment from this mock prove real inbox delivery or exactly-once effects.

After the backlog has drained, run the original direct provider check separately:

```sh
docker compose run --rm --no-deps provider-check
```

It has ten iterations, five checks each, and no thresholds. Keep its results
separate from application tests and throughput measurements.

## Persistence and crash checks

The deterministic suite kills actual subprocesses in both processing stages. For
a separate Docker experiment, create and activate a small verifiable batch, wait
for processing, then kill only the app with `docker compose kill -s SIGKILL app`.
Restart with `docker compose start app`, retain the volume, and verify the accepted
batch to completion. Expired leases and restart cooldown intentionally delay work.
An uncertain notification attempt may duplicate; do not claim exactly-once recovery.

Recreate only the app with `docker compose up -d --no-deps --force-recreate --wait app`
and query prior terminal IDs to verify volume persistence. Keep operational crash
experiments separate from performance runs.

Create and verify a separate batch with an independent oracle:

```sh
mkdir -p docs/evidence/smoke
.venv/bin/python scripts/create_batch.py --base-url "$TEST_BASE_URL" \
  --count 12 --output docs/evidence/smoke/input.json
# Read since/until from input.json; use accepted as EXPECTED_COUNT.
.venv/bin/python scripts/verify_outcomes.py --base-url "$TEST_BASE_URL" \
  --since "$SINCE" --until "$UNTIL" --expected-count "$EXPECTED_COUNT" \
  --expectations docs/evidence/smoke/input.json \
  --output docs/evidence/smoke/outcomes.json
```

## Evidence retention

Record commit/source hashes, app/provider image IDs, installed packages, Docker
resources, worker counts, timestamps and preexisting backlog. Save raw results under
ignored `docs/evidence/` and summarize measured findings in `validation.md`.
Never commit database files, logs, coverage artifacts, credentials or generated
benchmark reports. InfluxDB's supplied retention policy is only one hour; export
before evidence expires. Source and image changes invalidate performance comparison
unless explicitly identified. Preserve unsuccessful experiments rather than hiding them.
