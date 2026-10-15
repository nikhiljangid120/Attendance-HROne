"""Reproducible 100k-log verification in an isolated, disposable database."""
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MONGO_DB"] = "hrone_benchmark_" + uuid4().hex
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
from fastapi.testclient import TestClient
from app import main


def run():
    try:
        main.db.employees.insert_many([
            dict(emp_code=f"EMP{i:04}", name=f"Employee {i}", email=f"e{i}@example.com",
                 department="Engineering" if i < 500 else "Sales", joined_on="2026-01-01",
                 shift_start="09:30", shift_end="18:30", created_at=main.instant())
            for i in range(1000)])
        first = date(2026, 7, 1)
        for offset in range(100):
            day = (first + timedelta(days=offset)).isoformat()
            punch_start = datetime.combine(date.fromisoformat(day), time(9, 30), main.IST)
            main.db.attendance_logs.insert_many([
                dict(emp_code=f"EMP{i:04}", date=day, status="PRESENT", punch_in=punch_start, punch_out=punch_start + timedelta(hours=8),
                     work_hours=9.0, late_minutes=i % 4 * 10, overtime_minutes=0, half_day=False, history=[])
                for i in range(1000)])
        # Stored analytics fields are authoritative: these logs deliberately
        # report nine stored hours for an eight-hour interval.
        assert set(main.db.employees.index_information()) == {"_id_"}
        assert set(main.db.attendance_logs.index_information()) == {"_id_"}
        startup_started = perf_counter()
        with TestClient(main.app) as api:
            assert api.get("/health").status_code == 200
            startup_ms = round((perf_counter() - startup_started) * 1000, 2)
            assert startup_ms < 20000, f"Readiness exceeded 20 seconds: {startup_ms}ms"
            cases = {
                "attendance_list": ("/attendance", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2, "page_size": 20}),
                "employee_monthly": ("/analytics/employees/EMP0001/monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                "department_summary": ("/analytics/departments/summary", {"month": "2026-07"}),
                "late_leaderboard": ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
                "department_trend": ("/analytics/departments/Engineering/trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"})}
            timings = {}
            for name, (path, params) in cases.items():
                started = perf_counter()
                result = api.get(path, params=params)
                timings[name] = round((perf_counter() - started) * 1000, 2)
                assert result.status_code == 200, result.text
                body = result.json()
                if name == "attendance_list":
                    assert body["total"] == 31000 and len(body["items"]) == 20
                elif name == "employee_monthly":
                    assert body["working_days"] == 23 and body["present_days"] == 23
                    assert body["attendance_pct"] == 100 and body["total_late_minutes"] == 310
                elif name == "department_summary":
                    assert len(body["items"]) == 2
                    assert all(r["headcount"] == 500 and r["present_days"] == 11500 and r["avg_work_hours"] == 9 for r in body["items"])
                elif name == "late_leaderboard":
                    assert len(body["items"]) == 250 and all(r["rank"] == 1 and r["total_late_minutes"] == 930 for r in body["items"])
                else:
                    assert len(body["items"]) == 31
                    assert all(r["headcount"] == 500 and r["present_count"] == 500 for r in body["items"])
                    assert all(r["attendance_rate"] == (1 if r["is_working_day"] else None) for r in body["items"])
                explained = api.get("/admin/explain/" + name, params=params)
                assert explained.status_code == 200, explained.text
                assert '"IXSCAN"' in explained.text and '"COLLSCAN"' not in explained.text, name
            # Verify the employee-filtered list path on the larger dataset too.
            filtered = api.get("/admin/explain/attendance_list", params={"emp_code": "EMP0001", "status": "PRESENT", "page": 2})
            assert filtered.status_code == 200 and '"IXSCAN"' in filtered.text and '"COLLSCAN"' not in filtered.text
            print(json.dumps({"employees": 1000, "logs": 100000, "startup_with_seeded_data_ms": startup_ms, "request_ms": timings,
                              "all_explains": "IXSCAN present, no COLLSCAN", "result_checks": "passed"}, indent=2))
            assert main.db.name.startswith("hrone_benchmark_")
            main.client.drop_database(main.db.name)
    finally:
        # A separate cleanup client also handles exceptions after the app closes.
        from pymongo import MongoClient
        with MongoClient(os.environ["MONGO_URI"]) as cleanup:
            cleanup.drop_database(os.environ["MONGO_DB"])


if __name__ == "__main__":
    run()
