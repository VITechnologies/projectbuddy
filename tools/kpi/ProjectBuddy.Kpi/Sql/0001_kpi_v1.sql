-- kpi_v1 (#328, #330): the cost of AI-assisted work, from the agent's own session event logs, and the KPIs that steer
-- spec-driven design. Ingested facts are keyed by their event or tool-call IDs, so ingesting a log again changes nothing;
-- the agent records work markers, spec gaps, reviews, requirement counts and operator entries as they happen.

CREATE SCHEMA kpi_v1;

-- A work item or change: owner/repo#n on GitHub, group[/subgroup...]/project!n on GitLab.
CREATE DOMAIN kpi_v1.work_ref AS text CHECK (VALUE ~ '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+[#!][0-9]+$');
CREATE DOMAIN kpi_v1.session_id AS text CHECK (VALUE ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');

CREATE TABLE kpi_v1.settings (
    key text PRIMARY KEY CHECK (key IN ('usd_per_unit', 'idle_gap_minutes', 'prompt_cap_minutes', 'merge_skew_minutes')),
    value numeric NOT NULL CHECK (value > 0)
);
INSERT INTO kpi_v1.settings VALUES ('usd_per_unit', 0.04), ('idle_gap_minutes', 30), ('prompt_cap_minutes', 10), ('merge_skew_minutes', 2);

CREATE TABLE kpi_v1.tiers (
    tier text PRIMARY KEY CHECK (tier ~ '^T[1-9]$'),
    role text NOT NULL,
    usd_per_hour numeric NOT NULL CHECK (usd_per_hour > 0)
);
INSERT INTO kpi_v1.tiers VALUES
    ('T1', 'accountable owner', 150), ('T2', 'senior engineer and reviewer', 110),
    ('T3', 'engineer running runbooks', 75), ('T4', 'technician', 45);

CREATE TABLE kpi_v1.sessions (
    session_id kpi_v1.session_id PRIMARY KEY,
    first_at timestamptz NOT NULL,
    last_at timestamptz NOT NULL CHECK (last_at >= first_at),
    events bigint NOT NULL CHECK (events > 0)
);

-- One row per billed model call (session.usage_record). The category is what the call asked for, by the tools it
-- requested: ask (ask_user), edit (edit or create), delegate (task or write_agent), run (bash), memory, explore (any
-- other tool, all read-only), or respond (no tool).
CREATE TABLE kpi_v1.model_calls (
    event_id uuid PRIMARY KEY,
    session_id text NOT NULL REFERENCES kpi_v1.sessions,
    at timestamptz NOT NULL,
    initiator text NOT NULL,
    model text NOT NULL,
    cost numeric NOT NULL CHECK (cost >= 0),
    input_tokens bigint NOT NULL,
    output_tokens bigint NOT NULL,
    cache_read_tokens bigint NOT NULL,
    category text NOT NULL CHECK (category IN ('ask', 'edit', 'delegate', 'run', 'memory', 'explore', 'respond'))
);
CREATE INDEX model_calls_session_at ON kpi_v1.model_calls (session_id, at);

CREATE TABLE kpi_v1.tool_calls (
    session_id text NOT NULL REFERENCES kpi_v1.sessions,
    tool_call_id text NOT NULL,
    tool text NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    success boolean,
    PRIMARY KEY (session_id, tool_call_id)
);
CREATE INDEX tool_calls_session_started ON kpi_v1.tool_calls (session_id, started_at);

-- The repositories whose merged pull or merge requests are the sessions' accepted changes (projectbuddy-kpi track).
CREATE TABLE kpi_v1.repositories (
    repository text PRIMARY KEY CHECK (repository ~ '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$'),
    forge text NOT NULL CHECK (forge IN ('github', 'gitlab')),
    CHECK (forge = 'gitlab' OR repository ~ '^[^/]+/[^/]+$')
);

-- Every request a tracked repository merged since the first session (projectbuddy-kpi resolve).
CREATE TABLE kpi_v1.forge_changes (
    change_ref kpi_v1.work_ref PRIMARY KEY,
    merged_at timestamptz NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE kpi_v1.operator_messages (
    event_id uuid PRIMARY KEY,
    session_id text NOT NULL REFERENCES kpi_v1.sessions,
    at timestamptz NOT NULL
);

-- Derived on every ingest from all of the session's event times: spans whose gaps are at most idle_gap_minutes.
CREATE TABLE kpi_v1.active_spans (
    session_id text NOT NULL REFERENCES kpi_v1.sessions,
    span tstzrange NOT NULL CHECK (NOT isempty(span)),
    PRIMARY KEY (session_id, span)
);

-- Recorded by the agent. A marker attributes the session's calls from its time to the next marker to its work item.
CREATE TABLE kpi_v1.work_markers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id kpi_v1.session_id NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    item kpi_v1.work_ref NOT NULL
);

-- A fact the agent had to discover that the specification should have stated.
CREATE TABLE kpi_v1.spec_gaps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id kpi_v1.session_id NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    item kpi_v1.work_ref NOT NULL,
    kind text NOT NULL CHECK (kind IN ('code-read', 'operator-question', 'failed-run')),
    fact text NOT NULL CHECK (btrim(fact) <> ''),
    belongs_in text NOT NULL CHECK (btrim(belongs_in) <> '')
);

CREATE TABLE kpi_v1.reviews (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id kpi_v1.session_id NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    item kpi_v1.work_ref NOT NULL,
    round integer NOT NULL CHECK (round >= 1),
    findings integer NOT NULL CHECK (findings >= 0),
    UNIQUE (item, round)
);

CREATE TABLE kpi_v1.requirement_counts (
    item kpi_v1.work_ref PRIMARY KEY,
    requirements integer NOT NULL CHECK (requirements > 0)
);

-- Operator time outside the agent's sessions, by tier.
CREATE TABLE kpi_v1.operator_entries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    tier text NOT NULL REFERENCES kpi_v1.tiers,
    minutes numeric NOT NULL CHECK (minutes > 0),
    activity text NOT NULL CHECK (activity IN ('prompts', 'approvals', 'technician', 'review', 'watching', 'restarts')),
    item kpi_v1.work_ref,
    note text
);

CREATE FUNCTION kpi_v1.setting(p_key text) RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT s.value FROM kpi_v1.settings AS s WHERE s.key = p_key;
$$;

CREATE FUNCTION kpi_v1.tier_rate(p_tier text) RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT t.usd_per_hour FROM kpi_v1.tiers AS t WHERE t.tier = p_tier;
$$;

-- A change is accepted, at its merge time, by the session active when the forge merged it: one of the session's active
-- spans lies within merge_skew_minutes of the merge (for clock skew). However it was merged (a merge command, an API
-- call, a helper script, or auto-merge while the session waited), the forge's record decides. When several sessions
-- were active, the one whose latest tool call started at or before the merge accepts it. A merge while no session was
-- active is not a session's acceptance.
CREATE VIEW kpi_v1.accepted_changes AS
    SELECT f.change_ref, a.session_id, f.merged_at AS at
    FROM kpi_v1.forge_changes AS f
    CROSS JOIN LATERAL (
        SELECT sp.session_id
        FROM kpi_v1.active_spans AS sp
        WHERE sp.span && tstzrange(f.merged_at - kpi_v1.setting('merge_skew_minutes')::float8 * interval '1 minute',
                                   f.merged_at + kpi_v1.setting('merge_skew_minutes')::float8 * interval '1 minute', '[]')
        ORDER BY (SELECT max(t.started_at) FROM kpi_v1.tool_calls AS t
                  WHERE t.session_id = sp.session_id AND t.started_at <= f.merged_at) DESC NULLS LAST, sp.session_id
        LIMIT 1) AS a;

-- Each accepted change costs what its session spent after the session's previous accepted change (or the session's
-- start) up to and including its acceptance: model calls, and the session's active time at the T1 rate.
CREATE VIEW kpi_v1.change_costs AS
    WITH intervals AS (
        SELECT a.session_id, a.change_ref, a.at AS accepted_at,
               -- A merge accepted within the skew before the session's first event costs nothing.
               least(coalesce(lag(a.at) OVER (PARTITION BY a.session_id ORDER BY a.at, a.change_ref), s.first_at - interval '1 microsecond'), a.at) AS since
        FROM kpi_v1.accepted_changes AS a JOIN kpi_v1.sessions AS s USING (session_id)
    )
    SELECT i.session_id, i.change_ref, i.since, i.accepted_at,
           coalesce(m.calls, 0) AS calls, coalesce(m.units, 0) AS units,
           coalesce(m.units, 0) * kpi_v1.setting('usd_per_unit') AS ai_usd,
           coalesce(o.hours, 0) AS operator_hours,
           coalesce(o.hours, 0) * kpi_v1.tier_rate('T1') AS operator_usd
    FROM intervals AS i
    LEFT JOIN LATERAL (
        SELECT count(*) AS calls, sum(c.cost) AS units FROM kpi_v1.model_calls AS c
        WHERE c.session_id = i.session_id AND c.at > i.since AND c.at <= i.accepted_at) AS m ON true
    LEFT JOIN LATERAL (
        SELECT sum(extract(epoch FROM upper(r.x) - lower(r.x))) / 3600 AS hours
        FROM kpi_v1.active_spans AS sp CROSS JOIN LATERAL (SELECT sp.span * tstzrange(i.since, i.accepted_at, '(]') AS x) AS r
        WHERE sp.session_id = i.session_id AND NOT isempty(r.x)) AS o ON true;

-- Each model call with the work item of the latest marker at or before it in its session.
CREATE VIEW kpi_v1.call_items AS
    SELECT c.*, (SELECT w.item FROM kpi_v1.work_markers AS w
                 WHERE w.session_id = c.session_id AND w.at <= c.at ORDER BY w.at DESC, w.id DESC LIMIT 1) AS item
    FROM kpi_v1.model_calls AS c;

CREATE VIEW kpi_v1.item_costs AS
    SELECT ci.item, count(*) AS calls, sum(ci.cost) AS units, sum(ci.cost) * kpi_v1.setting('usd_per_unit') AS ai_usd,
           coalesce(sum(ci.cost) FILTER (WHERE ci.at > r.first_review_at), 0) AS rework_units
    FROM kpi_v1.call_items AS ci
    LEFT JOIN (SELECT v.item, min(v.at) AS first_review_at FROM kpi_v1.reviews AS v GROUP BY v.item) AS r USING (item)
    WHERE ci.item IS NOT NULL
    GROUP BY ci.item;

CREATE FUNCTION kpi_v1.report() RETURNS jsonb LANGUAGE sql STABLE AS $$
    WITH totals AS (
        SELECT count(*) AS calls, coalesce(sum(c.cost), 0) AS units,
               coalesce(sum(c.cost) FILTER (WHERE c.category = 'explore'), 0) AS explore_units
        FROM kpi_v1.model_calls AS c
    ),
    -- The operator's time is the union of all sessions' active spans: overlapping sessions do not count it twice.
    spans AS (
        SELECT coalesce(sum(extract(epoch FROM upper(r) - lower(r))), 0) / 3600 AS hours
        FROM unnest(coalesce((SELECT range_agg(s.span) FROM kpi_v1.active_spans AS s), '{}'::tstzmultirange)) AS r
    ),
    entries AS (
        SELECT coalesce(sum(e.minutes), 0) / 60 AS hours, coalesce(sum(e.minutes / 60 * t.usd_per_hour), 0) AS usd
        FROM kpi_v1.operator_entries AS e JOIN kpi_v1.tiers AS t USING (tier)
    ),
    changes AS (SELECT c.*, c.ai_usd + c.operator_usd AS total_usd FROM kpi_v1.change_costs AS c),
    prompts AS (
        SELECT count(*) AS count,
               coalesce(sum(least(extract(epoch FROM t.completed_at - t.started_at) / 60, kpi_v1.setting('prompt_cap_minutes'))), 0) AS capped_minutes
        FROM kpi_v1.tool_calls AS t WHERE t.tool = 'ask_user' AND t.completed_at IS NOT NULL
    )
    SELECT jsonb_build_object(
        'usdPerUnit', kpi_v1.setting('usd_per_unit'),
        'sessions', (SELECT count(*) FROM kpi_v1.sessions),
        'ai', jsonb_build_object('calls', totals.calls, 'units', totals.units, 'usd', totals.units * kpi_v1.setting('usd_per_unit')),
        'operator', jsonb_build_object(
            'activeHours', round(spans.hours, 2), 'entryHours', round(entries.hours, 2),
            'usd', round(spans.hours * kpi_v1.tier_rate('T1') + entries.usd, 2),
            'prompts', prompts.count, 'promptMinutesCapped', round(prompts.capped_minutes, 1)),
        'totalUsd', round(totals.units * kpi_v1.setting('usd_per_unit') + spans.hours * kpi_v1.tier_rate('T1') + entries.usd, 2),
        'unitsByInitiator', (SELECT coalesce(jsonb_object_agg(x.initiator, x.units), '{}') FROM
            (SELECT c.initiator, sum(c.cost) AS units FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'unitsByModel', (SELECT coalesce(jsonb_object_agg(x.model, x.units), '{}') FROM
            (SELECT c.model, sum(c.cost) AS units FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'unitsByCategory', (SELECT coalesce(jsonb_object_agg(x.category, x.units), '{}') FROM
            (SELECT c.category, sum(c.cost) AS units FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'merges', jsonb_build_object(
            'trackedRepositories', (SELECT count(*) FROM kpi_v1.repositories),
            'merged', (SELECT count(*) FROM kpi_v1.forge_changes),
            'outsideSessions', (SELECT count(*) FROM kpi_v1.forge_changes) - (SELECT count(*) FROM kpi_v1.accepted_changes)),
        'explorationShare', CASE WHEN totals.units > 0 THEN round(totals.explore_units / totals.units, 4) END,
        'acceptedChanges', (SELECT jsonb_build_object(
            'count', count(*),
            'meanUsd', round(avg(c.total_usd), 2),
            'medianUsd', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY c.total_usd))::numeric, 2)) FROM changes AS c),
        'costPerRequirementUsd', (SELECT round(sum(i.ai_usd) / sum(r.requirements), 2)
            FROM kpi_v1.item_costs AS i JOIN kpi_v1.requirement_counts AS r USING (item)),
        'firstPassYield', (SELECT round(avg((v.findings = 0)::int), 4) FROM kpi_v1.reviews AS v WHERE v.round = 1),
        'reworkUnits', (SELECT coalesce(sum(i.rework_units), 0) FROM kpi_v1.item_costs AS i),
        'specGaps', jsonb_build_object(
            'count', (SELECT count(*) FROM kpi_v1.spec_gaps),
            'perAcceptedChange', (SELECT round((SELECT count(*) FROM kpi_v1.spec_gaps)::numeric / nullif(count(*), 0), 3) FROM changes)),
        'recentChanges', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'change', x.change_ref, 'acceptedAt', x.accepted_at, 'calls', x.calls, 'units', x.units,
            'aiUsd', round(x.ai_usd, 2), 'operatorHours', round(x.operator_hours, 2), 'totalUsd', round(x.total_usd, 2))
            ORDER BY x.accepted_at DESC, x.change_ref), '[]')
            FROM (SELECT * FROM changes ORDER BY accepted_at DESC, change_ref LIMIT 10) AS x))
    FROM totals, spans, entries, prompts;
$$;
