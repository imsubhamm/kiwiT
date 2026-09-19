"""Scheduled diagnostics. No market/provider calls, orders or session starts."""
import hashlib
import json
import os
from datetime import UTC, datetime

from kiwit.banknifty import BankNiftyStore
from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.intraday import SignalMailer
from kiwit.options_operations import diagnostics


def main():
    store = BankNiftyStore(PostgresDatabase(DatabaseSettings.from_env()))
    result = diagnostics(store, datetime.now(UTC))
    mailer = SignalMailer()
    if result['reason_codes'] and mailer.configured:
        now = datetime.now(UTC)
        reason_key = hashlib.sha256(json.dumps(sorted(result['reason_codes'])).encode()).hexdigest()
        with store.locked() as connection:
            claimed = connection.execute(
                "INSERT INTO banknifty_operational_alerts(reason_key,last_attempt,delivery_status) VALUES(%s,%s,'claimed') "
                "ON CONFLICT(reason_key) DO UPDATE SET last_attempt=EXCLUDED.last_attempt,delivery_status='claimed' "
                "WHERE banknifty_operational_alerts.last_attempt<EXCLUDED.last_attempt-interval '1 hour' RETURNING reason_key",
                (reason_key, now)).fetchone()
        if claimed:
            delivery, _ = mailer.send_operational_alert(result['reason_codes'],
                os.getenv('KIWIT_DASHBOARD_URL', 'https://kiwit.tathyaforge.in/dashboard'))
            with store.locked() as connection:
                connection.execute("UPDATE banknifty_operational_alerts SET delivery_status=%s WHERE reason_key=%s",
                                   (delivery, reason_key))
    print(json.dumps(result))
    raise SystemExit(1 if result['status'] == 'degraded' else 0)


if __name__ == '__main__':
    main()
