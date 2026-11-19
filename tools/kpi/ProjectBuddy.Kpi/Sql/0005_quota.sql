-- kpi_v1 quota (#373): the premium-interactions quota snapshots that each model call's event carries, and the settings
-- `quota` reads them with. A snapshot's overage is the quota's units used beyond the entitlement in the billing cycle;
-- it only grows within a cycle, so a session keeps a snapshot only when its overage differs from the one before.
-- `budget_usd` has no default, so `quota` refuses until `budget` sets it. `reserve_usd` and `budget_usd` may be 0.

ALTER TABLE kpi_v1.settings DROP CONSTRAINT settings_key_check;
ALTER TABLE kpi_v1.settings ADD CONSTRAINT settings_key_check CHECK (
    key IN ('usd_per_unit', 'idle_gap_minutes', 'prompt_cap_minutes', 'merge_skew_minutes', 'budget_usd', 'reserve_usd', 'burn_window_hours')
    OR key ~ '^farm_reference_aiu_per_job:[a-z0-9][a-z0-9-]*$');
ALTER TABLE kpi_v1.settings DROP CONSTRAINT settings_value_check;
ALTER TABLE kpi_v1.settings ADD CONSTRAINT settings_value_check CHECK (value > 0 OR (value = 0 AND key IN ('budget_usd', 'reserve_usd')));
INSERT INTO kpi_v1.settings VALUES ('burn_window_hours', 24);

CREATE TABLE kpi_v1.quota_snapshots (
    session_id text NOT NULL REFERENCES kpi_v1.sessions,
    at timestamptz NOT NULL,
    used_requests numeric NOT NULL,
    entitlement_requests numeric NOT NULL,
    overage numeric NOT NULL,
    reset_date timestamptz NOT NULL,
    PRIMARY KEY (session_id, at)
);
CREATE INDEX quota_snapshots_at ON kpi_v1.quota_snapshots (at);
