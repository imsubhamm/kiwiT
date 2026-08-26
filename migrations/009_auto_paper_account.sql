BEGIN;
-- Isolate new run budgets from old demonstration holdings; no real funds are moved.
INSERT INTO paper_accounts(account_id,initial_cash,cash_balance,status)
VALUES('kiwit-paper-auto',1000000,1000000,'active') ON CONFLICT(account_id) DO NOTHING;
INSERT INTO schema_migrations(version,name) VALUES(9,'auto_paper_account');
COMMIT;
