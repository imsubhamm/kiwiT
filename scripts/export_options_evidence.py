"""Export a reproducible, read-only options evidence bundle (no broker/model calls)."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from kiwit.database import DatabaseSettings, PostgresDatabase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    url = os.getenv('KIWIT_READONLY_DATABASE_URL', os.getenv('KIWIT_DATABASE_URL', ''))
    db = PostgresDatabase(DatabaseSettings(url))
    with db.transaction() as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        def records(query):
            cursor = connection.execute(query)
            columns = [col.name for col in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        bundle = {'format': 'banknifty-evidence-v1', 'execution': 'paper-only',
            'sessions': records('SELECT trading_date,state FROM banknifty_sessions ORDER BY trading_date'),
            'reports': records('SELECT trading_date,report,delivery_status FROM banknifty_daily_reports ORDER BY trading_date'),
            'market_tape': records("SELECT trading_date,observed_at,recorded_at,market_snapshot FROM banknifty_market_history "
                                   "WHERE scan_state='live_observation' ORDER BY recorded_at,observed_at"),
            'calls': records('SELECT call_id,trading_date,created_at,state,snapshot,result FROM banknifty_ai_calls ORDER BY slot'),
            'events': records('SELECT event_id,trading_date,event_at,kind,detail FROM banknifty_events ORDER BY event_id'),
            'outcomes': records('SELECT * FROM banknifty_trade_outcomes ORDER BY last_exit_at'),
            'coverage': records("SELECT trading_date,scan_state,count(*),min(observed_at),max(observed_at) "
                                "FROM banknifty_market_history GROUP BY 1,2 ORDER BY 1,2")}
    body = json.dumps(bundle, default=str, sort_keys=True).encode()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(body)
    print(json.dumps({'sha256': hashlib.sha256(body).hexdigest(), 'calls': len(bundle['calls']),
                      'events': len(bundle['events']), 'path': str(args.output)}))


if __name__ == '__main__':
    main()
