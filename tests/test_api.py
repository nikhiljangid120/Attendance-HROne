"""Contract checks against a real MongoDB; never use a user's practice database."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

os.environ["MONGO_DB"] = "hrone_test_" + uuid4().hex
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
from app import main


def ms(local):
    return int(datetime.fromisoformat(local).replace(tzinfo=main.IST).timestamp() * 1000)


@pytest.fixture(scope="session")
def api():
    with TestClient(main.app) as session:
        assert session.get("/health").status_code == 200
        yield session
        assert main.db.name.startswith("hrone_test_")
        main.client.drop_database(main.db.name)


@pytest.fixture(autouse=True)
def clean(api):
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})


def employee(api, code="EMP0001", **changes):
    payload = dict(emp_code=code, name="Test Employee", email="test@example.com",
                   department="Engineering", joined_on="2026-01-01")
    payload.update(changes)
    response = api.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def log(code, day, status="PRESENT", **changes):
    payload = dict(emp_code=code, date=day, status=status, punch_in=main.instant(ms(day + "T09:30:00")),
                   punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False, history=[])
    if status in ("ABSENT", "LEAVE"):
        payload["punch_in"] = None
    payload.update(changes)
    main.db.attendance_logs.insert_one(payload)


def test_health_checks_database(api, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PyMongoError("offline")
    monkeypatch.setattr(main.db, "command", unavailable)
    assert api.get("/health").status_code == 503


@pytest.mark.parametrize("changes", [
    {"emp_code": "BAD"}, {"emp_code": "EMP123"}, {"name": ""}, {"name": "x" * 101},
    {"email": "invalid"}, {"department": ""}, {"department": "x" * 51},
    {"joined_on": "2026-02-30"}, {"joined_on": "20260101"}, {"shift_start": "9:30"},
    {"shift_end": "25:00"}, {"shift_start": "18:30"}])
def test_invalid_employees(api, changes):
    body = dict(emp_code="EMP0001", name="Test", email="a@b.com", department="Engineering", joined_on="2026-01-01")
    body.update(changes)
    assert api.post("/employees", json=body).status_code == 422


def test_employee_serialization_pagination_and_filter(api):
    created = employee(api, "EMP0002")
    employee(api, "EMP0001", department="Sales")
    assert type(created["created_at"]) is int and "_id" not in created
    assert api.get("/employees", params={"page_size": 1}).json()["items"][0]["emp_code"] == "EMP0001"
    assert api.get("/employees", params={"page": 2, "page_size": 1}).json()["items"][0]["emp_code"] == "EMP0002"
    filtered = api.get("/employees", params={"department": "Engineering"}).json()
    assert filtered["total"] == 1
    assert api.get("/employees", params={"department": "engineering"}).json()["total"] == 0


def test_concurrent_employee_creation(api):
    body = dict(emp_code="EMP0001", name="Test", email="a@b.com", department="Engineering", joined_on="2026-01-01")
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: api.post("/employees", json=body).status_code, range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7


@pytest.mark.parametrize("timestamp", [1783312500, "1783312500000", 1783312500000.0, True, None, 4102444800001])
def test_strict_timestamp_validation(api, timestamp):
    for route in ("punch-in", "punch-out"):
        assert api.post("/attendance/" + route, json={"emp_code": "EMP0001", "punched_at": timestamp}).status_code == 422


@pytest.mark.parametrize("clock,late", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45)])
def test_grace_period_and_truncation(api, clock, late):
    employee(api)
    timestamp = ms("2026-07-06T" + clock) + 900
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
    assert response.status_code == 201
    row = response.json()
    assert row["late_minutes"] == late and row["punch_in"] == timestamp // 1000 * 1000
    assert "id" not in row and "_id" not in row and row["history"] == []


def test_punch_status_unknown_and_duplicate(api):
    body = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")}
    assert api.post("/attendance/punch-in", json=body).status_code == 404
    employee(api)
    assert api.post("/attendance/punch-in", json={**body, "status": "ABSENT"}).status_code == 422
    assert api.post("/attendance/punch-in", json={**body, "status": "WFH", "late_minutes": 900}).json()["late_minutes"] == 0
    assert api.post("/attendance/punch-in", json=body).status_code == 409


def test_parallel_punch_in_and_out(api):
    employee(api)
    body = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")}
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(12)))
    assert codes.count(201) == 1 and codes.count(409) == 11
    body["punched_at"] = ms("2026-07-06T19:00:00")
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda _: api.post("/attendance/punch-out", json=body).status_code, range(12)))
    assert codes.count(200) == 1 and codes.count(409) == 11
    assert main.db.attendance_logs.find_one({})["history"] == []


@pytest.mark.parametrize("out,overtime", [("18:59:59", 0), ("19:00:00", 30)])
def test_overtime_boundary(api, out, overtime):
    employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")})
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T" + out)}).json()
    assert result["overtime_minutes"] == overtime


@pytest.mark.parametrize("seconds,hours,half_day", [(16181, 4.49, True), (16182, 4.5, False), (16218, 4.51, False)])
def test_half_up_hours_and_half_day(api, seconds, hours, half_day):
    employee(api)
    timestamp = ms("2026-07-06T09:30:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp + seconds * 1000}).json()
    assert result["work_hours"] == hours and result["half_day"] is half_day


def test_overnight_previous_date_and_next_day_out(api):
    employee(api, shift_start="22:00", shift_end="06:00")
    result = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T01:00:00")}).json()
    assert result["date"] == "2026-07-06" and result["late_minutes"] == 180
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T06:40:00")}).json()
    assert result["overtime_minutes"] == 40 and result["work_hours"] == 5.67


def test_punch_out_interval_and_latest_closed_record(api):
    employee(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    timestamp = ms("2026-07-06T09:30:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": timestamp})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp + 86401000}).status_code == 422
    log("EMP0001", "2026-07-07", punch_out=main.instant(ms("2026-07-07T18:30:00")), work_hours=9.0)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T19:00:00")}).status_code == 409


def test_attendance_sort_filters_and_legacy_defaults(api):
    employee(api)
    employee(api, "EMP0002")
    for code, day in [("EMP0002", "2026-07-07"), ("EMP0001", "2026-07-07"), ("EMP0001", "2026-07-06")]:
        log(code, day)
    main.db.attendance_logs.update_many({}, {"$unset": {"half_day": "", "history": ""}})
    result = api.get("/attendance", params={"page_size": 2}).json()
    assert result["total"] == 3
    assert [x["emp_code"] for x in result["items"]] == ["EMP0001", "EMP0002"]
    assert all(x["half_day"] is False and x["history"] == [] for x in result["items"])
    assert api.get("/attendance", params={"emp_code": "EMP0001", "date_to": "2026-07-06"}).json()["total"] == 1


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101},
    {"date_from": "2026-02-30"}, {"date_from": "2026-07-08", "date_to": "2026-07-01"}, {"status": "UNKNOWN"}])
def test_attendance_validation(api, params):
    assert api.get("/attendance", params=params).status_code == 422


def test_correction_audit_recompute_and_noop(api):
    employee(api)
    log("EMP0001", "2026-07-06", punch_in=main.instant(ms("2026-07-06T10:05:00")), late_minutes=35)
    body = {"punch_in": ms("2026-07-06T09:30:00"), "punch_out": ms("2026-07-06T19:00:00"),
            "reason": "reader fault", "regularized_by": "hr.admin", "work_hours": 900}
    response = api.patch("/attendance/EMP0001/2026-07-06", json=body)
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["late_minutes"] == 0 and row["work_hours"] == 9.5 and row["overtime_minutes"] == 30
    assert len(row["history"]) == 1
    assert row["history"][0]["changes"]["punch_in"] == {"from": ms("2026-07-06T10:05:00"), "to": body["punch_in"]}
    assert "half_day" not in row["history"][0]["changes"]
    assert api.patch("/attendance/EMP0001/2026-07-06", json=body).status_code == 422
    absent = {"status": "ABSENT", "reason": "manual correction", "regularized_by": "hr.admin"}
    row = api.patch("/attendance/EMP0001/2026-07-06", json=absent).json()
    assert row["punch_in"] is None and row["punch_out"] is None and row["work_hours"] is None
    assert len(row["history"]) == 2


@pytest.mark.parametrize("changes", [{"punch_in": None}, {"status": "ABSENT", "punch_in": ms("2026-07-06T09:00:00")},
    {"punch_in": ms("2026-07-07T09:00:00")}, {"punch_out": ms("2026-07-06T08:00:00")}, {"reason": "bad"}])
def test_correction_validation(api, changes):
    employee(api)
    log("EMP0001", "2026-07-06")
    body = {"reason": "reader fault", "regularized_by": "hr.admin", **changes}
    assert api.patch("/attendance/EMP0001/2026-07-06", json=body).status_code == 422


def test_concurrent_corrections_preserve_history(api, monkeypatch):
    employee(api)
    log("EMP0001", "2026-07-06")
    main.db.attendance_logs.update_one({}, {"$unset": {"history": "", "half_day": ""}})
    original = main.snapshot_filter
    barrier = Barrier(2)
    def synchronized(record):
        result = original(record)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(main, "snapshot_filter", synchronized)
    def edit(status):
        return api.patch("/attendance/EMP0001/2026-07-06", json={"status": status, "reason": "reader fault", "regularized_by": "hr.admin"}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["WFH", "ON_DUTY"])) == [200, 409]
    assert len(main.db.attendance_logs.find_one({})["history"]) == 1


def test_monthly_weekdays_joiners_and_missing_logs(api):
    employee(api, joined_on="2026-07-15")
    log("EMP0001", "2026-07-15", half_day=True, late_minutes=10)
    log("EMP0001", "2026-07-18", late_minutes=20, overtime_minutes=40)
    log("EMP0001", "2026-07-16", status="LEAVE")
    row = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert row == dict(emp_code="EMP0001", month="2026-07", working_days=13, present_days=0.5,
                       leave_days=1, late_count=2, total_late_minutes=30, total_overtime_minutes=40, attendance_pct=3.85)
    employee(api, "EMP0002", joined_on="2026-08-01")
    future = api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    assert api.get("/analytics/employees/UNKNOWN/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_headcount_record_weighted_average_and_null(api):
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        employee(api, code)
    employee(api, "EMP0004", joined_on="2026-08-01")
    employee(api, "EMP0005", department="Sales")
    log("EMP0001", "2026-07-06", work_hours=8.0)
    log("EMP0001", "2026-07-07", work_hours=8.0)
    log("EMP0002", "2026-07-06", work_hours=2.0, half_day=True, late_minutes=10)
    row, empty = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert row["headcount"] == 3 and row["avg_work_hours"] == 6.0 and row["present_days"] == 2.5
    assert empty["headcount"] == 1 and empty["avg_work_hours"] is None
    main.db.attendance_logs.update_many({}, {"$set": {"work_hours": 8.005}})
    assert api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}).json()["items"][0]["avg_work_hours"] == 8.01


def test_competition_rank_ties_cutoff_filter_and_orphans(api):
    for index, minutes in enumerate((40, 30, 30, 10), 1):
        code = f"EMP{index:04}"
        employee(api, code, department="Sales" if index == 1 else "Engineering")
        log(code, "2026-07-06", late_minutes=minutes)
    log("ORPHAN", "2026-07-06", late_minutes=1000)
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 2, 2]
    assert [r["emp_code"] for r in rows] == ["EMP0001", "EMP0002", "EMP0003"]
    rows = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1, "department": "Engineering"}).json()["items"]
    assert [r["rank"] for r in rows] == [1, 1]


def test_trend_empty_days_weekends_joiners_and_window(api):
    employee(api, joined_on="2026-07-07")
    employee(api, "EMP0002", joined_on="2026-07-08")
    log("EMP0001", "2026-07-07", half_day=True, late_minutes=10)
    log("EMP0001", "2026-07-11")
    row = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-14"})
    assert row.status_code == 200, row.text
    rows = row.json()["items"]
    assert len(rows) == 9 and rows[0]["headcount"] == 0 and rows[0]["attendance_rate"] is None
    assert rows[1]["attendance_rate"] == 0.5 and rows[1]["late_count"] == 1
    assert rows[2]["headcount"] == 2 and rows[2]["attendance_rate"] == 0.0
    assert rows[2]["moving_avg_7d"] == 0.25
    assert rows[5]["present_count"] == 1 and rows[5]["attendance_rate"] is None
    assert rows[-1]["moving_avg_7d"] == 0.0
    empty = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-08-03", "to": "2026-08-04"}).json()["items"]
    assert len(empty) == 2 and all(r["attendance_rate"] == 0 for r in empty)
    assert api.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-06", "to": "2026-07-07"}).status_code == 404
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-07", "to": "2026-07-06"}).status_code == 422
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422


def test_explain_index_scans_all_endpoints(api):
    employee(api)
    log("EMP0001", "2026-07-06", late_minutes=10)
    parameters = {
        "attendance_list": {"page_size": 1}, "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
        "department_summary": {"month": "2026-07"}, "late_leaderboard": {"month": "2026-07"},
        "department_trend": {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-07"}}
    for endpoint, params in parameters.items():
        response = api.get("/admin/explain/" + endpoint, params=params)
        assert response.status_code == 200, response.text
        assert '"IXSCAN"' in response.text and '"COLLSCAN"' not in response.text
        assert response.json()["endpoint"] == endpoint
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/unknown").status_code == 422


def test_trend_rate_rounding_and_short_null_windows(api):
    for index in range(3):
        employee(api, f"EMP{index:04}")
    log("EMP0000", "2026-07-06")
    result = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-04", "to": "2026-07-07"})
    rows = result.json()["items"]
    assert rows[0]["moving_avg_7d"] is None and rows[1]["moving_avg_7d"] is None
    assert rows[2]["attendance_rate"] == 0.3333 and rows[2]["moving_avg_7d"] == 0.3333
    assert rows[3]["moving_avg_7d"] == 0.1667


def test_trend_join_date_only_controls_headcount_not_stored_log_totals(api):
    employee(api, joined_on="2026-07-07")
    log("EMP0001", "2026-07-06", late_minutes=10)
    row = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-06"}).json()["items"][0]
    assert row["headcount"] == 0
    assert row["present_count"] == 1 and row["late_count"] == 1
    assert row["attendance_rate"] is None and row["moving_avg_7d"] is None


def test_fractional_seeded_instants_normalized_on_correction(api):
    employee(api)
    timestamp = main.instant(ms("2026-07-06T09:40:00")) + timedelta(milliseconds=900)
    log("EMP0001", "2026-07-06", punch_in=timestamp)
    result = api.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "reason": "reader correction", "regularized_by": "hr"})
    assert result.status_code == 200
    assert result.json()["punch_in"] == ms("2026-07-06T09:40:00")
    assert result.json()["late_minutes"] == 0


def test_punch_out_same_second_as_fractional_seed_is_invalid_interval(api):
    employee(api)
    timestamp = ms("2026-07-06T09:30:00")
    log("EMP0001", "2026-07-06", punch_in=main.instant(timestamp) + timedelta(milliseconds=900))
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp + 950})
    assert response.status_code == 422
    assert main.db.attendance_logs.find_one({})["punch_out"] is None


def test_openapi_describes_all_successful_response_bodies(api):
    schema = api.get("/openapi.json").json()
    operations = 0
    for path in schema["paths"].values():
        for operation in path.values():
            success = operation["responses"].get("200", operation["responses"].get("201"))
            body_schema = success["content"]["application/json"]["schema"]
            assert "$ref" in body_schema
            model = schema["components"]["schemas"][body_schema["$ref"].split("/")[-1]]
            assert model.get("required"), operation["summary"]
            operations += 1
    assert operations == 12


def test_employee_response_excludes_non_contract_database_fields(api):
    employee(api)
    main.db.employees.update_one({}, {"$set": {"internal_note": "should not be returned"}})
    row = api.get("/employees").json()["items"][0]
    assert "internal_note" not in row


def test_explain_pagination_and_filters_match_real_list(api):
    employee(api)
    for day in ("2026-07-06", "2026-07-07", "2026-07-08"):
        log("EMP0001", day)
    params = {"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-08", "status": "PRESENT", "page": 2, "page_size": 1}
    body = api.get("/attendance", params=params).json()
    assert body["total"] == 3 and body["items"][0]["date"] == "2026-07-07"
    result = api.get("/admin/explain/attendance_list", params=params).json()["explain"]
    assert result["executionStats"]["nReturned"] == 1
    assert result["command"]["skip"] == 1 and result["command"]["limit"] == 1
    assert result["command"]["filter"]["status"] == "PRESENT"


@pytest.mark.parametrize("month", ["2026-13", "2026-7", "0000-01"])
def test_invalid_month(api, month):
    assert api.get("/analytics/departments/summary", params={"month": month}).status_code == 422
