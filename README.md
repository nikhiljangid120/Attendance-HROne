# Employee Attendance and Analytics API

A FastAPI backend for employee records, attendance tracking, audited corrections, and MongoDB-based analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer, running locally or through Atlas

## Setup

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/nikhiljangid120/Attendance-HROne.git
cd Attendance-HROne
python -m venv .venv
```

Activate the environment:

- Linux/macOS: `source .venv/bin/activate`
- Windows PowerShell: `.\.venv\Scripts\Activate.ps1`

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment, or copy `.env.example` to `.env` and configure them:

```dotenv
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=attendance_db
```

Environment variables take priority over values in `.env`.

## Run

From the repository root:

```bash
uvicorn app.main:app --port 8000
```

- Interactive API documentation: http://127.0.0.1:8000/docs
- OpenAPI schema: http://127.0.0.1:8000/openapi.json
- Readiness check: http://127.0.0.1:8000/health

Indexes are created automatically at startup. Existing attendance data is retained. The health endpoint returns 200 when MongoDB responds and indexes are ready, or 503 when unavailable.

## Features

- Employee creation and filtered, paginated listing.
- Punch-in and punch-out with grace, overtime, half-day, and overnight-shift rules.
- Manual corrections with recomputed metrics and an appended audit history.
- Employee monthly summaries, department summaries, a late leaderboard, and daily trends.
- MongoDB execution statistics through `/admin/explain/{endpoint}`.

All application logic is in `app/main.py`. Unique indexes prevent duplicate employee codes and employee/date attendance records. Conditional atomic updates protect concurrent punch-outs and corrections.

API timestamps are integer epoch milliseconds; MongoDB stores UTC datetimes. Shift times and attendance dates use IST. Work hours and reported metrics use decimal half-up rounding. Analytics read stored attendance metrics through aggregation pipelines, with gap filling and moving averages performed in MongoDB.

## Tests

With MongoDB running:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tests/benchmark.py
```

Tests use uniquely named disposable databases and remove them afterward. The benchmark seeds 1,000 employees and 100,000 attendance records before startup, then checks readiness, report results, and index usage.

GitHub Actions runs these checks on Python 3.11 with MongoDB 6 and Python 3.13 with MongoDB 7.

## Technical notes

- [REVIEW.md](REVIEW.md) documents starter-code defects and fixes.
- [DECISIONS.md](DECISIONS.md) explains index selection, concurrency, ranking ties, headcount, and scaling.
