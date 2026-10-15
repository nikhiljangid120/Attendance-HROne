"""Attendance API: IST calendar values, UTC BSON instants, epoch-ms HTTP values."""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

from bson import Decimal128, ObjectId, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
index_lock = Lock()
indexes_ready = False


def ensure_indexes():
    global indexes_ready
    with index_lock:
        if indexes_ready:
            return
        db.employees.create_indexes([
            IndexModel([("emp_code", 1)], unique=True, name="employee_code"),
            IndexModel([("department", 1), ("emp_code", 1)], name="department_code"),
            IndexModel([("department", 1), ("joined_on", 1), ("emp_code", 1)], name="department_joined"),
            IndexModel([("joined_on", 1), ("department", 1)], name="joined_department"),
        ])
        db.attendance_logs.create_indexes([
            IndexModel([("emp_code", 1), ("date", 1)], unique=True, name="employee_date"),
            IndexModel([("date", -1), ("emp_code", 1)], name="date_employee"),
            IndexModel([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee"),
            IndexModel([("emp_code", 1), ("punch_in", -1)], name="employee_punch"),
        ])
        indexes_ready = True


@asynccontextmanager
async def lifespan(application):
    try:
        ensure_indexes()
    except PyMongoError:
        logging.exception("MongoDB unavailable at startup; health will report 503")
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def validate_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def validate_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


CalendarDate = Annotated[str, BeforeValidator(validate_date), Field(json_schema_extra={"format": "date"})]
Month = Annotated[str, BeforeValidator(validate_month), Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000,
                                   description="Integer milliseconds since the Unix epoch; fractions are truncated to whole seconds.")]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: CalendarDate

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("omit punched_at to use the current time; null is invalid")
        return values


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, values):
        if isinstance(values, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in values and values[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return values


# Output models document the contract and filter unrelated database fields.
# Input models stay separate: clients cannot set audit or derived values.
class HealthResponse(BaseModel):
    status: Literal["ok"]


class ErrorResponse(BaseModel):
    detail: str


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: CalendarDate
    created_at: int


class EmployeePage(BaseModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int


class FieldChange(BaseModel):
    from_value: Any = Field(alias="from")
    to_value: Any = Field(alias="to")


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, FieldChange]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: CalendarDate
    status: Status
    punch_in: int | None
    punch_out: int | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(BaseModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentMetrics(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentMetrics]


class LeaderboardRow(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardRow]


class TrendRow(BaseModel):
    date: CalendarDate
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class Trend(BaseModel):
    department: str
    items: list[TrendRow]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


NOT_FOUND = {404: {"model": ErrorResponse, "description": "Employee, record or department not found"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Duplicate event or concurrent update"}}


def instant(milliseconds=None):
    if milliseconds is None:
        return datetime.now(UTC).replace(microsecond=0)
    return datetime.fromtimestamp(milliseconds // 1000, UTC)


def public(value):
    """Convert nested BSON values, including audit changes, at the HTTP boundary."""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def attendance_response(record):
    fields = ("emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
              "late_minutes", "overtime_minutes", "half_day", "history")
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    return public({field: record.get(field, defaults.get(field)) for field in fields})


def employee_or_404(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "unknown employee")
    return employee


def attendance_date(punch_in, employee):
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(employee["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, employee):
    start = datetime.combine(Date.fromisoformat(day), time.fromisoformat(employee["shift_start"]), IST)
    end = datetime.combine(Date.fromisoformat(day), time.fromisoformat(employee["shift_end"]), IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_work_hours(punch_in, punch_out):
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(record, employee):
    values = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if record["status"] not in PRESENCE:
        return values
    start, end = shift_bounds(record["date"], employee)
    seconds_late = int((record["punch_in"] - start).total_seconds())
    values["late_minutes"] = seconds_late // 60 if seconds_late > 600 else 0
    if record.get("punch_out") is not None:
        hours = compute_work_hours(record["punch_in"], record["punch_out"])
        overtime = int((record["punch_out"] - end).total_seconds()) // 60
        values.update(work_hours=hours, overtime_minutes=overtime if overtime >= 30 else 0, half_day=hours < 4.5)
    return values


def validate_interval(punch_in, punch_out):
    if punch_out <= punch_in or punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")


def snapshot_filter(record):
    """A concurrent writer changes the snapshot, so this request cannot overwrite it."""
    query = {"_id": record["_id"]}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                  "overtime_minutes", "half_day", "history"):
        query[field] = record[field] if field in record else {"$exists": False}
    return query


@app.get("/health", response_model=HealthResponse, tags=["system"],
         responses={503: {"model": ErrorResponse, "description": "MongoDB unavailable"}})
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=EmployeeResponse, tags=["employees"], responses=CONFLICT)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = instant()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(department: str | None = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    query = {} if department is None else {"department": department}
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": public(items), "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord,
          tags=["attendance"], responses={**NOT_FOUND, **CONFLICT})
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    timestamp = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_date(timestamp, employee),
           "status": body.status, "punch_in": timestamp, "punch_out": None, "history": []}
    doc.update(derived_fields(doc, employee))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_response(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecord,
          tags=["attendance"], responses={**NOT_FOUND, **CONFLICT})
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    timestamp = instant(body.punched_at)
    record = db.attendance_logs.find_one(
        # Compare at whole-second precision without computing over every stored
        # value: this indexable bound includes a seeded fractional punch in the
        # same second, which validate_interval then correctly rejects as equal.
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lt": timestamp + timedelta(seconds=1)}},
        sort=[("punch_in", -1)], hint="employee_punch")
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    proposed = {**record, "punch_in": record["punch_in"].replace(microsecond=0), "punch_out": timestamp}
    validate_interval(proposed["punch_in"], timestamp)
    changes = {"punch_in": proposed["punch_in"], "punch_out": timestamp, **derived_fields(proposed, employee)}
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(record), {"$set": changes}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently")
    return attendance_response(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    hint = "employee_date" if emp_code is not None else "status_date_employee" if status is not None else "date_employee"
    return query, hint


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(emp_code: str | None = None, date_from: CalendarDate | None = None,
                    date_to: CalendarDate | None = None, status: Status | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    records = db.attendance_logs.find(query).hint(hint).sort([("date", -1), ("emp_code", 1)])
    records = records.skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(record) for record in records],
            "total": db.attendance_logs.count_documents(query, hint=hint), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord,
           tags=["attendance"], responses={**NOT_FOUND, **CONFLICT})
def regularize(emp_code: str, date: CalendarDate, body: RegularizeIn):
    employee = employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no record for this date")
    proposed = {**record}
    for field in ("status", "punch_in", "punch_out"):
        if field in body.model_fields_set:
            value = getattr(body, field)
            proposed[field] = instant(value) if field != "status" else value
    for field in ("punch_in", "punch_out"):
        if proposed.get(field) is not None:
            proposed[field] = proposed[field].replace(microsecond=0)
    if proposed["status"] in ("ABSENT", "LEAVE"):
        if {"punch_in", "punch_out"} & body.model_fields_set:
            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
        proposed.update(punch_in=None, punch_out=None)
    else:
        if proposed.get("punch_in") is None:
            raise HTTPException(422, "presence requires punch_in")
        if attendance_date(proposed["punch_in"], employee) != date:
            raise HTTPException(422, "punch_in must stay on the attendance date")
        if proposed.get("punch_out") is not None:
            validate_interval(proposed["punch_in"], proposed["punch_out"])
    proposed.update(derived_fields(proposed, employee))
    before = attendance_response(record)
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for field in fields:
        previous = record.get(field, before[field])
        if previous != proposed.get(field):
            changes[field] = {"from": previous, "to": proposed.get(field)}
    if not changes:
        raise HTTPException(422, "correction changes nothing")
    entry = {"at": instant(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(record),
        {"$set": {field: proposed.get(field) for field in fields}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed concurrently; reload before correcting")
    return attendance_response(updated)


# Analytics builders are reused by the explain endpoint. No report fetches all
# logs into Python to calculate its result.
def month_bounds(month):
    start = Date.fromisoformat(month + "-01")
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start.isoformat(), end.isoformat()


def working_days(start, end):
    if start > end:
        return 0
    weeks, remainder = divmod((end - start).days + 1, 7)
    return weeks * 5 + sum((start.weekday() + offset) % 7 < 5 for offset in range(remainder))


def rounded(expression, places=2):
    """Nonnegative report metrics: decimal half-up rather than $round ties-even."""
    scale = 10 ** places
    return {"$cond": [{"$eq": [expression, None]}, None, {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")]}}, scale]}}]}


def weekday_expression(field="$date"):
    return {"$in": [{"$dayOfWeek": {
        "date": {"$dateFromString": {"dateString": field, "timezone": "Asia/Kolkata"}},
        "timezone": "Asia/Kolkata"}}, [2, 3, 4, 5, 6]]}


def present_weight(weekdays_only=True):
    conditions = [{"$in": ["$status", PRESENCE]}]
    if weekdays_only:
        conditions.append(weekday_expression())
    return {"$cond": [{"$and": conditions}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def metric_group():
    closed_present = {"$and": [{"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"_id": None, "present_days": {"$sum": present_weight()},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [closed_present, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_count": {"$sum": {"$cond": [closed_present, 1, 0]}}}


def monthly_pipeline(emp_code, month):
    start, end = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, {"$group": metric_group()}]


def summary_pipeline(month, department=None):
    start, end = month_bounds(month)
    query = {"joined_on": {"$lte": end}}
    if department is not None:
        query["department"] = department
    pipeline = [{"$match": query}, {"$lookup": {
        "from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": start, "$lte": end}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$group": metric_group()}], "as": "metrics"}},
        {"$set": {"metrics": {"$arrayElemAt": ["$metrics", 0]}}}]
    group = {"_id": "$department", "headcount": {"$sum": 1}}
    for field in ("present_days", "late_count", "total_late_minutes", "hours_sum", "hours_count", "on_duty_count"):
        group[field] = {"$sum": {"$ifNull": ["$metrics." + field, 0]}}
    group["leave_count"] = {"$sum": {"$ifNull": ["$metrics.leave_days", 0]}}
    pipeline += [{"$group": group}, {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
        "present_days": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]}, rounded({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$sort": {"department": 1}}]
    return pipeline, "department_joined" if department is not None else "joined_department"


def leaderboard_pipeline(month, limit, department=None):
    start, end = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]
    return pipeline


def trend_pipeline(department, start, end):
    first = datetime.combine(Date.fromisoformat(start), time(), IST)
    count = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    if count < 1 or count > 92:
        raise HTTPException(422, "range must contain 1 to 92 days")
    # Aggregate the period once, then generate the calendar spine in MongoDB.
    # The spine comes from employees, so an entirely empty log range still works.
    return [{"$match": {"department": department}},
        {"$facet": {
            "employees": [{"$group": {"_id": None, "joining_dates": {"$push": "$joined_on"}}}],
            "daily": [{"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
                    {"$project": {"_id": 0, "date": 1, "weight": present_weight(False),
                        "late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}], "as": "logs"}},
                {"$unwind": "$logs"},
                {"$group": {"_id": "$logs.date", "present_count": {"$sum": "$logs.weight"}, "late_count": {"$sum": "$logs.late"}}}]}},
        {"$project": {"daily": 1, "joining_dates": {"$ifNull": [{"$arrayElemAt": ["$employees.joining_dates", 0]}, []]}}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, count]}, "as": "offset", "in":
            {"$dateAdd": {"startDate": first, "unit": "day", "amount": "$$offset", "timezone": "Asia/Kolkata"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}},
        {"$set": {"metrics": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "row", "cond": {"$eq": ["$$row._id", "$date"]}}}, 0]},
            "headcount": {"$size": {"$filter": {"input": "$joining_dates", "as": "joined", "cond": {"$lte": ["$$joined", "$date"]}}}},
            "is_working_day": weekday_expression()}},
        {"$set": {"present_count": {"$ifNull": ["$metrics.present_count", 0]},
            "late_count": {"$ifNull": ["$metrics.late_count", 0]}}},
        {"$set": {"raw_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, None]}}},
        {"$set": {"attendance_rate": rounded("$raw_rate", 4)}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": rounded("$moving_avg_7d", 4)}}, {"$sort": {"date": 1}}]


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly,
         tags=["analytics"], responses=NOT_FOUND)
def employee_monthly(emp_code: str, month: Month):
    employee = employee_or_404(emp_code)
    start, end = month_bounds(month)
    days = working_days(max(Date.fromisoformat(start), Date.fromisoformat(employee["joined_on"])), Date.fromisoformat(end))
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month), hint="employee_date"))
    metrics = rows[0] if rows else {}
    result = {"emp_code": emp_code, "month": month, "working_days": days}
    for field in ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
        result[field] = metrics.get(field, 0)
    result["attendance_pct"] = (float((Decimal(str(result["present_days"])) * 100 / days)
        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if days else None)
    return result


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(month: Month, department: str | None = None):
    pipeline, hint = summary_pipeline(month, department)
    return {"month": month, "items": public(list(db.employees.aggregate(pipeline, hint=hint)))}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(month: Month, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department), hint="date_employee")
    return {"month": month, "items": public(list(rows))}


@app.get("/analytics/departments/{department}/trend", response_model=Trend,
         tags=["analytics"], responses=NOT_FOUND)
def department_trend(department: str, from_date: CalendarDate = Query(alias="from"), to_date: CalendarDate = Query(alias="to")):
    pipeline = trend_pipeline(department, from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": public(list(db.employees.aggregate(pipeline, hint="department_joined")))}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse, tags=["admin"])
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: Month | None = None,
    department: str | None = None, limit: int = Query(10, ge=1, le=50),
    date_from: CalendarDate | None = None, date_to: CalendarDate | None = None, status: Status | None = None,
    from_date: CalendarDate | None = Query(None, alias="from"), to_date: CalendarDate | None = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        command = {"find": collection, "filter": query, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint != "department_trend" and month is None:
            raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            pipeline, hint = monthly_pipeline(emp_code, month), "employee_date"
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline, hint = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            pipeline, hint = leaderboard_pipeline(month, limit, department), "date_employee"
        else:
            if department is None or from_date is None or to_date is None:
                raise HTTPException(422, "department, from and to are required")
            collection = "employees"
            pipeline, hint = trend_pipeline(department, from_date, to_date), "department_joined"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}
