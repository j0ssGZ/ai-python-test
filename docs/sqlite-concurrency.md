# SQLite concurrency investigation

Investigation date: 2026-10-22. Baseline: `0b49a7a`, personal-fork main.
Scope: the two historical Linux quota-test failures. Application behavior, SQLite
configuration, dependencies, provider and original k6 scripts are unchanged.

**Status: CLOSED — a reproduced test synchronization defect, with bounded SQLite
contention still an explicit operational limitation.** The fix is commit `b19f4e4`.

## Finding and causal evidence

The failing test was
`app/tests/test_recovery.py::test_concurrent_quota_reservations_and_restart_cooldown`.
Both retained historical runs reported `sqlite3.OperationalError: database is locked`
at the repository transaction's `await db.execute("BEGIN IMMEDIATE")`.
The stack continued through aiosqlite's execute, _execute (awaiting its future),
and Connection.run (the SQLite thread executing the statement). One run overlapped
other work; an isolated rerun also failed. Totals: 140 passed / 1 failed in 39.03 s
and 36.86 s. Neither log recorded native error codes, transaction timings, CPU
throttling, or storage I/O latency.

The original schedule creates 100 coroutines at once, alternating between two
repository connections, starting with B. Each connection's mutex serializes its
50 calls: at most two SQLite contenders, with 49 additional calls queued behind
each initial call. All use logical time 100, a quota of 50 per 10 seconds and zero
spacing. Each starts an immediate transaction, expires admissions, reads
quota/cooldown, possibly inserts an admission, and commits. A deferral also commits.

The test required every operation to succeed despite a **one-second bounded wait
for the writer lock**, implicitly requiring fairness across independent connections.
SQLite permits [only one WAL writer](https://www.sqlite.org/wal.html); its
[busy handler](https://www.sqlite.org/c3ref/busy_timeout.html) sleeps and retries
until its accumulated waiting budget is exhausted. It does not provide the
application's per-connection FIFO mutex across connections. A queued writer can
repeatedly reacquire between another writer's attempts. Atomic quota decisions and
timely admission of every contending writer are distinct properties.

Current unthrottled runs of the original test passed 10/10 on each of overlayfs,
tmpfs and a persistent Docker volume. The historical symptom is intermittent;
normal storage does not invariably fail, and tmpfs success is not proof of correctness.

Controlled reproduction used **--cpus 0.02** (2% of one CPU), leaving repository and
SQLite settings unchanged. The **exact original test failed 3/3** on overlayfs at
the same BEGIN boundary with the same exception. This is deliberately adverse
scheduling, not a reconstruction of measured historical resource pressure.
An earlier five-run instrumented overlayfs comparison at 0.10 CPU had no errors.

An instrumented five-run overlayfs burst recorded seven native **SQLITE_BUSY,
code 5**, across four runs. The losing connection waited approximately 1.10–1.30 s,
including asynchronous scheduling/cleanup. During those intervals the other
connection completed 10–28 transactions. The longest measured interval from
successful BEGIN return through COMMIT completion was 605 ms; the other runs'
maxima were 264–299 ms. This establishes repeated writer reacquisition under a
bounded timeout, rather than a permanently leaked transaction. All five runs
persisted exactly 50 admissions, returned quick_check=ok and ended with neither
connection in a transaction. The probe settles every task and records every error.

The demonstrated defect is the test's unsupported scheduling assumption. Resource
pressure exposes it; WAL or the filesystem need not be misconfigured. No application
atomicity or cancellation defect was demonstrated. The exact historical source of
CPU/I/O delay remains unknown because those runs lack telemetry. The reproduction
establishes a sufficient failure mechanism, not evidence of historical disk failure.

## Transaction and lifecycle review

- Initialization uses autocommit, WAL, synchronous=FULL, busy_timeout=1000
  (also connect(timeout=1)) and foreign keys. Recorded wal_autocheckpoint is 1000
  pages. Startup migrations are sequential; simultaneous migrations are unsupported.
- Each aiosqlite thread serializes statements on its connection. The repository's
  asyncio.Lock additionally spans BEGIN, statements, COMMIT and failure cleanup.
  Independent repositories do not share that mutex. HTTP, rate-limit sleeps and
  retry backoff run outside transactions.
- BEGIN is inside exception cleanup. Cancelling an await may leave a queued SQLite
  operation running. Rollback queues behind it and is shielded through repeated
  cancellation; the mutex remains held until cleanup completes. New tests exercise
  an actually busy BEGIN alongside existing cancellation-after-BEGIN and repeated
  cancellation-during-rollback tests.
- Lease selection/update and attempt creation are atomic. Provider calls follow
  commit. Token/expiry checks fence stale completion. Persisted retry timestamps
  and validated extraction survive reopen/SIGKILL. These boundaries did not change.
- Shutdown stops acquisition, drains workers, cancels remaining tasks, waits for
  cleanup, then closes clients/repository. Repository close takes its mutex.
  The corrected test uses TaskGroup to cancel/join a failed contender's siblings
  before teardown; original gather propagated the first failure without itself
  joining remaining tasks. No teardown-caused leak was observed or inferred.
- Busy API writes return 503 without acknowledging uncommitted work. Worker storage
  exceptions remain logged and the loop retries; after a lease is acquired,
  persisted state/expiry govern recovery. An uncertain remote send retains the
  documented duplicate-delivery risk. Readiness is not a progress SLA.

## Minimal correction and regression coverage

Two independent producers now make 50 rounds using asyncio.Barrier(2). Both
writers race each round; neither can enqueue another round while its competitor
is still completing the previous one. There are still 100 real reservations,
two connections, the original quota and the unchanged one-second timeout. There
are no timing sleeps, catches hiding lock errors, or tmpfs-only fixtures.

Assertions require exactly 50 admissions, exactly 50 deferrals of 10.001 s,
exactly 50 stored admissions, the original 15-second restart cooldown, and that
same cooldown after both connections close and a new repository opens.
Additional tests:

- Preserve a 100-task burst on the single shared repository used by deployment.
- Hold an owner transaction until a contender returns native SQLITE_BUSY. Assert
  the unchanged 1000 ms setting, no failed insertion, no waiter transaction leak,
  no rollback of the owner, successful reuse, persisted rows and quick_check=ok
  after reopen. The transaction scope establishes ordering without sleeps.
- Cancel a contender after SQLite's trace callback signals its real blocked BEGIN.
  Verify cleanup completes, the owner stays intact and later writes succeed.
- Exercise actual contention through HTTP: 503 with no accepted row, then 201
  and exactly one row after release. This is not a mocked database exception.

No application retry, timeout, schema, lease, parser, worker or delivery code
changed. The diagnostic script requires a new scratch directory, records
BEGIN/COMMIT/ROLLBACK and transaction events, retains native errors and returns
nonzero when burst reservations fail. It is not used by the service.

## Storage matrix and results

Host: macOS 26.5.1 arm64, Python 3.12.14, SQLite 3.53.1, aiosqlite 0.21.0.
Linux: Docker Desktop Engine 29.7.2, kernel 7.0.12-linuxkit aarch64, 10 CPUs,
8,319,504,384 bytes configured VM memory; Python 3.12.15, SQLite 3.46.1,
aiosqlite 0.21.0. No CPU cap applies to ordinary verification.

The development image was built once before measurements:
sha256:a9ee20ada2616d0c32c6b5221178275e24c12e2dbb89aa654d515be0a24b4732.
Application/test source was mounted read-only. Databases were under /sqlite-tests,
never in the host source bind mount:

- Normal writable container layer: overlayfs, no database volume/tmpfs mount.
- tmpfs: explicit --tmpfs /sqlite-tests.
- Persistent local volume: ai-python-test-sqlite-investigation-20261022 at
  /sqlite-tests, ext4 on /dev/vda1 according to mountinfo. stat -f reports the
  generic ext2/ext3 label. This is deployment's local-volume storage class,
  using a separate volume that cannot alter production records.

The production volume/app were untouched. No load producer, provider check or
dependency installation overlapped measurements. Mountinfo, versions, per-run exit
status, stdout/stderr and timings are under ignored docs/evidence/sqlite-lock/.
Historical failures remain in docs/evidence/zero-sends/linux-tests.log and
linux-tests-isolated.log.

Complete-suite results after the test correction:

- macOS: **160 passed in 7.29 s**, 91% combined statement/branch coverage.
- Linux overlayfs: **160 passed in 7.74 s**.
- Linux tmpfs: **160 passed in 6.41 s**.
- Linux persistent ext4 volume: **160 passed in 7.69 s**.
- Additional focused repetitions: **50/50 on each of those four configurations**,
  200/200 total, with no failed reservation or assertion. These are additional to
  the focused scenario included in each complete suite.

Every complete suite includes actual subprocess SIGKILL in both stages, persisted
notification retries without repeated extraction, concurrent claim fencing,
bounded AI/delivery workers, cancellation, shutdown and controlled HTTP integration.
A new container then reopened the persistent test database and verified 50
admissions, notify cooldown 115, schema version 3 and quick_check=ok.

That cross-container check used SQLite mode=ro; its directory was mounted writable,
matching deployment's directory permissions. An initial attempt with the volume read-only
returned "unable to open database file"; WAL readers can need to create sidecars.
The failed diagnostic is retained in volume-readonly-probe.log. This is a different
error/configuration from the quota contention finding, not a discarded suite failure.

Controlled comparisons at 0.02 CPU, five runs per schedule/storage:

- Overlayfs: unsynchronized burst failed in **4/5 runs, 7 SQLITE_BUSY errors**;
  synchronized producers passed **5/5**, zero errors.
- tmpfs: unsynchronized burst failed in **3/5 runs, 5 SQLITE_BUSY errors**;
  synchronized producers passed **5/5**, zero errors.
- Persistent ext4 volume: unsynchronized burst failed in **5/5 runs, 8 SQLITE_BUSY
  errors**; synchronized producers passed **5/5**, zero errors.

All these instrumented runs retained exactly 50 committed admissions, quick_check=ok
and no open transaction after settlement. Synchronized runs also returned exactly
50 successful admissions and 50 deferrals. tmpfs and volume experiments alternated
burst/paired schedules per repetition; the overlayfs schedules were separate runs.
The measured cgroup cpu.max was 2000/100000 microseconds for the comparisons.
These traces compare mechanisms, not universally optimal scheduling or performance.

The controlled comparisons are separate from ordinary verification and the three
unmodified-test reproductions. No original k6 rerun is needed for this change:
application, dependencies and deployment behavior are byte-for-byte unchanged,
and all HTTP integration tests ran on every configuration. The previous real-send
benchmark remains documented separately; HTTP test counts do not establish sends.

## Reproduction

Use the host Python 3.12 development environment and run the complete pytest suite,
ruff check, and ruff format --check as documented in [testing.md](testing.md).
For Linux, build a dedicated test image once, based on the existing application
image, with development dependencies preinstalled:

```sh
docker build -t ai-python-test-sqlite-tests -f - . <<'DOCKERFILE'
FROM ai-python-test-app:latest
USER root
COPY app/requirements.txt app/requirements-dev.txt /test-requirements/
RUN pip install --no-cache-dir -r /test-requirements/requirements-dev.txt
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/workspace/app
ENTRYPOINT ["python"]
DOCKERFILE
docker volume create ai-python-test-sqlite-check
docker run --rm -v ai-python-test-sqlite-check:/sqlite-tests \
  -v "$PWD/app:/workspace/app:ro" -v "$PWD/scripts:/workspace/scripts:ro" \
  -v "$PWD/pyproject.toml:/workspace/pyproject.toml:ro" \
  ai-python-test-sqlite-tests -m pytest -q -p no:cacheprovider \
  --basetemp=/sqlite-tests/full /workspace/app/tests
```

Repeat without the volume mount for overlayfs; replace it with
`--tmpfs /sqlite-tests` for tmpfs. Use only a dedicated test volume: pytest owns
and may remove its `--basetemp` directory. Repeat the focused node 50 times with
fresh pytest temporary directories and retain every exit code, not just the last:

```sh
.venv/bin/pytest -q \
  app/tests/test_recovery.py::test_concurrent_quota_reservations_and_restart_cooldown
```

For the instrumented adverse burst, including native SQLite error codes:

```sh
mkdir -p docs/evidence/sqlite-check
docker run --rm --cpus 0.02 \
  -v "$PWD/app:/workspace/app:ro" -v "$PWD/scripts:/workspace/scripts:ro" \
  -v "$PWD/docs/evidence/sqlite-check:/evidence" \
  ai-python-test-sqlite-tests /workspace/scripts/probe_sqlite_contention.py \
  --directory /sqlite-tests/burst --output /evidence/burst.json --repeats 5
```

A nonzero exit with reported SQLITE_BUSY is a diagnostic result, not something
to suppress. Repeat with `--paired`, distinct output/directory paths, and the
tmpfs/volume mounts to compare scheduling and storage independently. Cgroup quotas
do not reproduce a particular historical machine state; record observed results,
not an expected number of failures.

## Residual limits

A one-second busy budget cannot guarantee success against arbitrary external
writers, suspension, or slow storage. Even one competing transaction can exceed
that budget. The synchronized test checks quota correctness without assuming
cross-connection fairness; it does not certify arbitrary load or multi-process
deployment. Exactly one application process/connection remains supported. Busy
writes must remain visible as failures/503s, not acknowledged notifications.

These tests verify process crash recovery and logical persistence on local storage.
They do not simulate power loss, controller write-cache failure, disk exhaustion,
network filesystems or volume loss. quick_check is an integrity check, not proof
of physical durability. Historical I/O latency was not captured; future incidents
need transaction and host resource traces before attributing them to disk.
