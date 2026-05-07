import sqlite3

c = sqlite3.connect('file:./data/stock_analysis.db?mode=ro', uri=True)
for tbl in ('kline_audit_runs', 'kline_audit_gaps'):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info('{tbl}')").fetchall()]
    print(f"{tbl}: {cols}")
