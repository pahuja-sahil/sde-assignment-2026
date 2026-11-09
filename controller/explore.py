import sqlite3

conn = sqlite3.connect("controller/state.db")

rows = conn.execute("""
    SELECT workflow_id, cohort_id, welcome_message, voice, caller_id, prompt_version,
           SUM(dials), SUM(connected), SUM(engaged)
    FROM call_agg
    GROUP BY workflow_id, cohort_id, welcome_message, voice, caller_id, prompt_version
    ORDER BY workflow_id, SUM(dials) DESC
""").fetchall()

current = None
for wf, cohort, w, v, d, p, dials, conn_, eng in rows:
    if wf != current:
        print(f"\n=== {wf} ===")
        print("cohort    welcome voice caller prompt    dials  pickup  engage")
        current = wf
    pickup = conn_ / dials if dials else 0
    engage = eng / conn_ if conn_ else 0
    print(f"{cohort:9s} {w:7s} {v:5s} {d:6s} {p:6s} {dials:8d}  {pickup:.3f}  {engage:.3f}")