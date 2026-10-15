# Employee Attendance and Analytics API

Python 3.11+ and MongoDB 6.0+ are required. All application code is in app/main.py. The API reads BSON UTC dates and exposes epoch milliseconds; attendance calendar dates and shifts use IST.

## Run

From this repository root (the attendance-api folder):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "hrone_practice"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

With an activated virtual environment, the required command is `uvicorn app.main:app --port 8000`. MongoDB must already be running. A local .env is supported; injected environment variables take priority. Indexes are created automatically and idempotently. GET /health returns 200 only after MongoDB responds and indexes are ready; otherwise 503. Interactive documentation is at http://127.0.0.1:8000/docs.

For the prepared Windows development environment, this shortcut reuses a running MongoDB or starts the existing portable local installation, then runs the API:

```powershell
.\scripts\dev.ps1 -StartLocalMongo -Reload
```

Use `-Port 8002` if needed. The script runs the API on 127.0.0.1, never seeds or clears data, and leaves MongoDB running when the API stops. A clean clone must supply MongoDB separately; the portable server is a local development dependency excluded from Git.

For Linux/macOS, use `python -m venv .venv`, `source .venv/bin/activate`, `pip install -r requirements.txt`, export the two environment variables, then run the required Uvicorn command.

## Test

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tests\benchmark.py
```

Tests use real MongoDB, create uniquely named disposable databases, and remove them afterward. The benchmark seeds 1,000 employees and 100,000 attendance records before starting the app, checks readiness within 20 seconds including automatic index creation, then checks report results and executionStats index plans. Timings describe this local test run, not hosted production latency.

## Implementation

All twelve required method/path operations are implemented, including manual corrections, four aggregation-backed reports and the explain endpoint. No known endpoint is intentionally omitted. Concurrent inserts are protected by unique indexes. Punch-out and corrections use conditional atomic updates against the state read by that request; losing edits return 409. History is appended only for corrections. Missing legacy fields receive the specified defaults.

Explicit response models document all successful responses in /docs and filter fields to the public contract. Date query parameters describe their calendar format. Configuration loads only this repository's .env, with real environment variables retaining priority.

The trend pipeline uses $facet and indexed per-employee period lookups, generates its calendar spine with $range/$map/$dateAdd, and computes the moving average with $setWindowFields. This fills even an entirely empty range inside MongoDB. Report metrics read stored derived values. Half-up decimal rounding is explicit.

Joining dates control trend headcount independently of stored log totals. A weekday with zero eligible employees has a null attendance rate; weekends also have null rates. The seven-row moving window ignores null rate values and never includes dates before the requested range.

## Automated checks and source export

`.github/workflows/ci.yml` runs tests and the 100,000-log benchmark on push or pull request, using Python 3.11/MongoDB 6 and Python 3.13/MongoDB 7. It runs against a temporary MongoDB service, without database secrets or a Dockerfile. The workflow must run on GitHub before its remote result can be assessed.

```powershell
.\.venv\Scripts\python.exe scripts\submission.py --check
.\.venv\Scripts\python.exe scripts\submission.py
```

The first command checks required source files, the decisions word count, common credential patterns and prohibited files in Git's publishable set. The second exports a deterministic source-only ZIP and SHA256 file under `.local/submission/`. The archive uses an explicit file list; local data and the private kit are excluded. The requested submission is still a public Git repository URL, not this ZIP.

REVIEW.md documents starter defects; DECISIONS.md describes the implemented choices. Documentation and AI assistance were used during development; the candidate should review the code and explanations before submission and be prepared to explain them live.

## Local files and submission

The private kit copy, sample seed/data, virtual environment, portable local MongoDB files and .env are excluded by .gitignore. The seed script is only a local convenience: it deletes the selected database's employee/log contents. Never run it against an existing valuable database.

Publish this folder as its own repository, not the unrelated parent workspace. Do not include .env, secrets, a Dockerfile, reference problem documents, virtual environments or database dumps. Hosting the API is not required by the supplied submission instructions.
