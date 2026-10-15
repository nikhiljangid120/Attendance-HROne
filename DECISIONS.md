# Implementation decisions

1. **Indexes.** Unique employee-code and employee/date indexes enforce identities and support monthly lookups. Department/code supports sorted employee lists; department/joined and joined/department support eligibility queries. Date/employee and status/date/employee support attendance filters and ordering. Employee/punch supports selecting the latest eligible punch-in. A separate late-minutes index was rejected: the leaderboard ranks monthly totals, not individual log values, so the date index is more useful.

2. **Punch-in race.** Both requests validate input and find the employee. They attempt insertion under the unique employee/date index. MongoDB allows one insert; the other raises DuplicateKeyError, translated to 409. A preceding existence check is unnecessary.

3. **Ties.** MongoDB $rank assigns competition ranks before applying rank <= limit. Tied employees at the cutoff are all returned, ordered by emp_code. The result can exceed limit rows.

4. **Headcount.** Department summary starts from employees whose joined_on is on or before month end. Attendance is left-joined, so an eligible employee with no logs still contributes one person and zero attendance totals.

5. **At 100x.** Measure executionStats and workload latency first. Materialized daily department totals could reduce repeated aggregation; correction processing would also have to keep those totals consistent.
