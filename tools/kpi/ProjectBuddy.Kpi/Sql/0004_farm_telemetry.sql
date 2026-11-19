-- kpi_v1 farm telemetry (ALW#10): the local AI farm's jobs and device samples, ingested from the export that
-- assurance-learning-workbench writes from farm_v1 (`export-farm-telemetry`). Rows are keyed by the sha256 of the farm
-- run record they came from, so ingesting an export again adds nothing. The export holds counts and timings only.
--
-- AIU avoided is an estimate, reported apart from measured AIU (ALW decision 7): a workload's completed jobs times its
-- reference cost, `farm_reference_aiu_per_job:<workload>` in kpi_v1.settings, set from paid runs that kpi_v1 measured.
-- A workload without a reference has no estimate. No device telemetry leaves the peer (ALW decision 8), so a device
-- sample is local-only and a peer instance has jobs, counted from infer's counters, but no utilization.

ALTER TABLE kpi_v1.settings DROP CONSTRAINT settings_key_check;
ALTER TABLE kpi_v1.settings ADD CONSTRAINT settings_key_check CHECK (
    key IN ('usd_per_unit', 'idle_gap_minutes', 'prompt_cap_minutes', 'merge_skew_minutes')
    OR key ~ '^farm_reference_aiu_per_job:[a-z0-9][a-z0-9-]*$');

CREATE DOMAIN kpi_v1.farm_id AS text CHECK (VALUE ~ '^[a-z0-9][a-z0-9-]*$');
CREATE DOMAIN kpi_v1.farm_source AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- One row per farm run record, workload (pipeline stage) and instance; `jobs` counts the stage's items there.
CREATE TABLE kpi_v1.farm_jobs (
    source_sha256 kpi_v1.farm_source NOT NULL,
    recorded_at timestamptz NOT NULL,
    workload kpi_v1.farm_id NOT NULL,
    instance_id kpi_v1.farm_id NOT NULL,
    backend text NOT NULL CHECK (backend IN ('local-ollama', 'peer-infer')),
    jobs integer NOT NULL CHECK (jobs >= 0),
    cache_hits integer NOT NULL CHECK (cache_hits >= 0),
    answered integer NOT NULL CHECK (answered >= 0),
    failed integer NOT NULL CHECK (failed >= 0),
    eval_tokens bigint NOT NULL CHECK (eval_tokens >= 0),
    window_seconds numeric(12, 3) NOT NULL CHECK (window_seconds >= 0),
    PRIMARY KEY (source_sha256, workload, instance_id),
    CHECK (cache_hits + answered + failed <= jobs)
);

-- One row per farm run record and local instance: the seconds it had an item in flight within the run's busy span.
CREATE TABLE kpi_v1.farm_device_samples (
    source_sha256 kpi_v1.farm_source NOT NULL,
    recorded_at timestamptz NOT NULL,
    instance_id kpi_v1.farm_id NOT NULL,
    backend text NOT NULL CHECK (backend = 'local-ollama'),
    window_seconds numeric(12, 3) NOT NULL CHECK (window_seconds > 0),
    busy_seconds numeric(12, 3) NOT NULL CHECK (busy_seconds >= 0 AND busy_seconds <= window_seconds),
    PRIMARY KEY (source_sha256, instance_id)
);

-- Utilization per device: busy seconds over the busy spans of the runs that sampled it.
CREATE VIEW kpi_v1.farm_device_utilization AS
    SELECT s.instance_id, count(*) AS samples, sum(s.busy_seconds) AS busy_seconds, sum(s.window_seconds) AS window_seconds,
           round(sum(s.busy_seconds) / sum(s.window_seconds), 4) AS utilization
    FROM kpi_v1.farm_device_samples AS s GROUP BY s.instance_id;

-- Per workload: a job is completed by a cache hit or an answer. Jobs/s divides the completed jobs by the busy spans of
-- the runs that ran the workload, each run's span counted once however many instances shared it.
CREATE VIEW kpi_v1.farm_workloads AS
    WITH windows AS (
        SELECT r.workload, sum(r.window_seconds) AS window_seconds
        FROM (SELECT DISTINCT j.source_sha256, j.workload, j.window_seconds FROM kpi_v1.farm_jobs AS j) AS r GROUP BY r.workload
    )
    SELECT j.workload, sum(j.jobs) AS jobs, sum(j.cache_hits + j.answered) AS completed, sum(j.cache_hits) AS cache_hits,
           w.window_seconds, kpi_v1.setting('farm_reference_aiu_per_job:' || j.workload) AS reference_aiu_per_job,
           sum(j.cache_hits + j.answered) * kpi_v1.setting('farm_reference_aiu_per_job:' || j.workload) AS aiu_avoided_estimate
    FROM kpi_v1.farm_jobs AS j JOIN windows AS w USING (workload) GROUP BY j.workload, w.window_seconds;

CREATE FUNCTION kpi_v1.farm_report() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'runs', (SELECT count(DISTINCT j.source_sha256) FROM kpi_v1.farm_jobs AS j),
        'jobs', coalesce(sum(w.jobs), 0), 'completed', coalesce(sum(w.completed), 0), 'cacheHits', coalesce(sum(w.cache_hits), 0),
        'cacheHitRate', round(sum(w.cache_hits) / nullif(sum(w.jobs), 0), 4),
        'jobsPerSecond', round(sum(w.completed) / nullif((SELECT sum(r.window_seconds) FROM
            (SELECT DISTINCT j.source_sha256, j.window_seconds FROM kpi_v1.farm_jobs AS j) AS r), 0), 4),
        'aiuAvoidedEstimate', round(sum(w.aiu_avoided_estimate), 2),
        'workloadsWithoutReference', coalesce(jsonb_agg(w.workload ORDER BY w.workload) FILTER (WHERE w.reference_aiu_per_job IS NULL), '[]'),
        'byWorkload', coalesce(jsonb_agg(jsonb_build_object(
            'workload', w.workload, 'jobs', w.jobs, 'completed', w.completed, 'cacheHits', w.cache_hits,
            'cacheHitRate', round(w.cache_hits::numeric / nullif(w.jobs, 0), 4),
            'jobsPerSecond', round(w.completed / nullif(w.window_seconds, 0), 4),
            'referenceAiuPerJob', w.reference_aiu_per_job, 'aiuAvoidedEstimate', round(w.aiu_avoided_estimate, 2))
            ORDER BY w.workload), '[]'),
        'devices', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'instanceId', d.instance_id, 'samples', d.samples, 'busySeconds', d.busy_seconds, 'windowSeconds', d.window_seconds,
            'utilization', d.utilization) ORDER BY d.instance_id), '[]') FROM kpi_v1.farm_device_utilization AS d))
    FROM kpi_v1.farm_workloads AS w;
$$;

-- The report keeps every existing figure and adds the farm's under `farm`. A later migration that replaces the report
-- replaces kpi_v1.report_core().
ALTER FUNCTION kpi_v1.report() RENAME TO report_core;
CREATE FUNCTION kpi_v1.report() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT kpi_v1.report_core() || jsonb_build_object('farm', kpi_v1.farm_report());
$$;
