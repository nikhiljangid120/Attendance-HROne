# Starter code review

Locations refer to the unmodified starter functions. Each example identifies an observable failure; repairs are in app/main.py.

| # | Where | What is wrong | How to notice it | Repair |
|---|---|---|---|---|
| 1 | health | Reports ready without contacting MongoDB. | Stop MongoDB: starter still returns 200. | Ping the database; return 503 when unavailable. |
| 2 | startup / create_employee | No unique employee-code index; check-then-insert races. | Send simultaneous creates with one code. | Create a unique index at startup; catch DuplicateKeyError as 409. |
| 3 | EmployeeIn | Employee-code, lengths, email and shift formats are unchecked. | BAD code, empty name, malformed email or 25:00 shift are accepted. | Match the contract's field constraints with Pydantic. |
| 4 | EmployeeIn | joined_on is an unchecked string. | 2026-02-30 or compact 20260701 is accepted. | Validate both YYYY-MM-DD format and actual calendar date. |
| 5 | EmployeeIn | Equal shift start/end is accepted. | Supply 09:30 for both. | Reject equal shift times with 422. |
| 6 | create_employee | datetime.now() is naive local time, interpreted as UTC by BSON. | Run on an IST machine and compare stored created_at with the actual instant. | Generate timezone-aware UTC instants. |
| 7 | response handling | Datetimes are returned as ISO strings rather than epoch milliseconds. | Inspect created_at or punch_in JSON types. | Recursively serialize instants as integer milliseconds, including audit changes. |
| 8 | list_employees | Skip is page * page_size. | First page omits the first 20 employees. | Use (page - 1) * page_size. |
| 9 | list_employees | total counts all employees rather than the filtered set. | Filter a department containing one of several employees. | Count with the same filter. |
| 10 | list_employees | No deterministic emp_code ordering. | Insert employees out of order; page boundaries depend on insertion order. | Sort by emp_code before pagination. |
| 11 | both list endpoints | Missing pagination bounds. | Request page=0 or page_size=101. | Enforce page >= 1 and page_size from 1 to 100. |
| 12 | PunchInIn | int coercion and absent range checks accept invalid timestamps. | Send seconds, an integer string, a float, a boolean or an out-of-range number. | Use a strict bounded epoch-milliseconds type; reject explicit null. |
| 13 | PunchInIn | Arbitrary status strings are allowed. | Punch in with ABSENT or UNKNOWN. | Limit punch-in to PRESENT, WFH and ON_DUTY. |
| 14 | punch_in | Unknown employee is dereferenced, producing a server error. | Punch in with a nonexistent emp_code. | Look up the employee and return 404 before constructing the record. |
| 15 | punch_in | datetime.fromtimestamp() and ts.date() depend on host timezone. | Use a UTC instant near IST midnight on a UTC host. | Decode as aware UTC and derive the calendar date in IST. |
| 16 | punch_in | Does not assign pre-shift-end overnight punches to the previous day. | 01:00 IST punch for a 22:00–06:00 shift. | Apply the previous-calendar-day rule. |
| 17 | punch_in / helpers | Milliseconds are not truncated before storage/calculation. | 09:40:00.900 should be stored as 09:40:00. | Truncate instants to whole seconds at the boundary and normalize corrected times. |
| 18 | compute_late_minutes | Grace is tested after flooring elapsed minutes. | 09:40:01 for a 09:30 shift returns zero instead of ten. | Test elapsed seconds > 600, then floor minutes since shift start. |
| 19 | compute_late_minutes | Shift clock is placed directly onto a naive punch datetime. | Host timezone differs from IST, or overnight punch occurs after midnight. | Construct aware IST shift bounds on the attendance date. |
| 20 | compute_work_hours | Built-in float round uses ties-even and binary arithmetic. | 16,218 seconds is 4.505 hours and must round to 4.51; the starter returns 4.50. | Divide integer seconds using Decimal and ROUND_HALF_UP. |
| 21 | compute_overtime | Missing 30-minute minimum. | A punch-out 29 minutes after shift end yields 29. | Return zero below 30 whole minutes. |
| 22 | compute_overtime | Overnight shift end is placed on the start date. | 22:00–06:00 shift, next-day 06:40 punch-out. | Move overnight shift end to the next day. |
| 23 | compute_overtime / client | Aware shift end is subtracted from a naive driver/punch datetime. | Directly exercise the helper with the starter's naive datetime. | Use tz_aware=True and aware instants throughout. |
| 24 | punch_in | Check-then-insert cannot enforce one record per employee/date. | Send parallel duplicate punch-ins. | Unique compound index; exactly one insert succeeds and duplicates return 409. |
| 25 | punch_in / list_attendance | Exposes generated id, contrary to the natural-key contract. | Inspect either attendance response. | Return only the contract's attendance fields. |
| 26 | list_attendance | Loads and sorts every matching record in Python. | Request one page from 100,000 records. | Filter, sort, skip and limit using MongoDB and indexed query builders. |
| 27 | list_attendance | Sort omits emp_code as a tie breaker. | Two employees have records on the same day. | Sort date descending, then emp_code ascending. |
| 28 | list_attendance | Dates, date ordering and status are not validated. | Invalid calendar date, reversed range or unknown status is accepted. | Shared calendar validators, enum status and range validation return 422. |
| 29 | list_attendance | Legacy missing history/half_day fields are returned without required defaults. | Seed a document that omits both. | Serialize missing history as [] and half_day as false; also default missing minute totals to zero. |
| 30 | startup / reads | Only default _id indexes exist; required filters and analytics are unsupported. | Explain representative date/employee/department queries. | Create workload-specific indexes idempotently at startup; explain the real builders. |

## Examined and retained

- load_dotenv() normally leaves existing environment variables in place. That behavior already met the environment-priority rule; it is made explicit with override=False.
- Removing employee _id before the response was correct. The separate attendance id field was the defect.
- Optional omission of punched_at correctly means current time. The repair preserves omission while rejecting invalid supplied values.
- Storing calendar dates as YYYY-MM-DD strings is intentional. Validated strings sort chronologically and match the seeded model.
- Ordinary synchronous def routes are appropriate for the synchronous PyMongo driver. FastAPI runs those handlers in its thread pool.
- No attendance history is created by punch-in. That is correct: history records manual corrections only.

## Validation

The integration suite covers validation, serialization, pagination, grace/rounding/overtime boundaries, overnight shifts, parallel creates/punches/corrections, legacy defaults, no-log employees, joins, weekends, ties, trend gaps and query plans. The separate benchmark verifies report results and index plans with 100,000 generated records in a disposable database.

Additional checks of the completed implementation, separate from starter defects: response models prevent extra employee database fields leaking into responses; a seeded fractional punch-in is compared at whole-second precision during punch-out; trend joining dates affect headcount independently of stored log totals. The benchmark also verifies automatic index creation when the database already contains 100,000 logs at startup.
