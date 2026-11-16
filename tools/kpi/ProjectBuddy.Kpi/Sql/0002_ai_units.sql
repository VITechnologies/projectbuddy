-- kpi_v1 AI Units (#339): AI cost is metered in AI Units (AIU), the unit the account quota is billed in. A Copilot CLI
-- run that reported 6.93 AIU used 7.0 quota units. The per-call usage-record cost (15 for claude-opus-5.5) is nominal: it
-- stays as nominal_units, and CLI sessions, which bill only AIU, record 0 for it. usd_per_unit prices a quota unit.

ALTER TABLE kpi_v1.model_calls ADD COLUMN aiu numeric CHECK (aiu >= 0);
ALTER TABLE kpi_v1.sessions ADD COLUMN aiu numeric CHECK (aiu >= 0);
COMMENT ON COLUMN kpi_v1.model_calls.aiu IS 'The call''s share of its measured interval''s AIU; null until measured';
COMMENT ON COLUMN kpi_v1.sessions.aiu IS 'The session''s measured AIU: checkpoint deltas, or the sum of shutdown totals';

DROP VIEW kpi_v1.item_costs;
DROP VIEW kpi_v1.call_items;
DROP VIEW kpi_v1.change_costs;

CREATE VIEW kpi_v1.change_costs AS
    WITH intervals AS (
        SELECT a.session_id, a.change_ref, a.at AS accepted_at,
               -- A merge accepted within the skew before the session's first event costs nothing.
               least(coalesce(lag(a.at) OVER (PARTITION BY a.session_id ORDER BY a.at, a.change_ref), s.first_at - interval '1 microsecond'), a.at) AS since
        FROM kpi_v1.accepted_changes AS a JOIN kpi_v1.sessions AS s USING (session_id)
    )
    SELECT i.session_id, i.change_ref, i.since, i.accepted_at,
           coalesce(m.calls, 0) AS calls, coalesce(m.aiu, 0) AS aiu, coalesce(m.units, 0) AS nominal_units,
           coalesce(m.aiu, 0) * kpi_v1.setting('usd_per_unit') AS ai_usd,
           coalesce(o.hours, 0) AS operator_hours,
           coalesce(o.hours, 0) * kpi_v1.tier_rate('T1') AS operator_usd
    FROM intervals AS i
    LEFT JOIN LATERAL (
        SELECT count(*) AS calls, sum(c.aiu) AS aiu, sum(c.cost) AS units FROM kpi_v1.model_calls AS c
        WHERE c.session_id = i.session_id AND c.at > i.since AND c.at <= i.accepted_at) AS m ON true
    LEFT JOIN LATERAL (
        SELECT sum(extract(epoch FROM upper(r.x) - lower(r.x))) / 3600 AS hours
        FROM kpi_v1.active_spans AS sp CROSS JOIN LATERAL (SELECT sp.span * tstzrange(i.since, i.accepted_at, '(]') AS x) AS r
        WHERE sp.session_id = i.session_id AND NOT isempty(r.x)) AS o ON true;

CREATE VIEW kpi_v1.call_items AS
    SELECT c.*, (SELECT w.item FROM kpi_v1.work_markers AS w
                 WHERE w.session_id = c.session_id AND w.at <= c.at ORDER BY w.at DESC, w.id DESC LIMIT 1) AS item
    FROM kpi_v1.model_calls AS c;

CREATE VIEW kpi_v1.item_costs AS
    SELECT ci.item, count(*) AS calls, coalesce(sum(ci.aiu), 0) AS aiu, coalesce(sum(ci.aiu), 0) * kpi_v1.setting('usd_per_unit') AS ai_usd,
           coalesce(sum(ci.aiu) FILTER (WHERE ci.at > r.first_review_at), 0) AS rework_aiu
    FROM kpi_v1.call_items AS ci
    LEFT JOIN (SELECT v.item, min(v.at) AS first_review_at FROM kpi_v1.reviews AS v GROUP BY v.item) AS r USING (item)
    WHERE ci.item IS NOT NULL
    GROUP BY ci.item;

CREATE OR REPLACE FUNCTION kpi_v1.report() RETURNS jsonb LANGUAGE sql STABLE AS $$
    WITH totals AS (
        SELECT count(*) AS calls, coalesce(sum(c.aiu), 0) AS aiu, coalesce(sum(c.cost), 0) AS nominal_units,
               count(c.aiu) AS measured_calls, coalesce(sum(c.aiu) FILTER (WHERE c.category = 'explore'), 0) AS explore_aiu
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
        'ai', jsonb_build_object('calls', totals.calls, 'measuredCalls', totals.measured_calls, 'aiu', totals.aiu,
            'usd', round(totals.aiu * kpi_v1.setting('usd_per_unit'), 2), 'nominalUnits', totals.nominal_units,
            'measuredAiu', (SELECT coalesce(sum(s.aiu), 0) FROM kpi_v1.sessions AS s)),
        'operator', jsonb_build_object(
            'activeHours', round(spans.hours, 2), 'entryHours', round(entries.hours, 2),
            'usd', round(spans.hours * kpi_v1.tier_rate('T1') + entries.usd, 2),
            'prompts', prompts.count, 'promptMinutesCapped', round(prompts.capped_minutes, 1)),
        'totalUsd', round(totals.aiu * kpi_v1.setting('usd_per_unit') + spans.hours * kpi_v1.tier_rate('T1') + entries.usd, 2),
        'aiuByInitiator', (SELECT coalesce(jsonb_object_agg(x.initiator, x.aiu), '{}') FROM
            (SELECT c.initiator, coalesce(sum(c.aiu), 0) AS aiu FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'aiuByModel', (SELECT coalesce(jsonb_object_agg(x.model, x.aiu), '{}') FROM
            (SELECT c.model, coalesce(sum(c.aiu), 0) AS aiu FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'aiuByCategory', (SELECT coalesce(jsonb_object_agg(x.category, x.aiu), '{}') FROM
            (SELECT c.category, coalesce(sum(c.aiu), 0) AS aiu FROM kpi_v1.model_calls AS c GROUP BY 1) AS x),
        'merges', jsonb_build_object(
            'trackedRepositories', (SELECT count(*) FROM kpi_v1.repositories),
            'merged', (SELECT count(*) FROM kpi_v1.forge_changes),
            'outsideSessions', (SELECT count(*) FROM kpi_v1.forge_changes) - (SELECT count(*) FROM kpi_v1.accepted_changes)),
        'explorationShare', CASE WHEN totals.aiu > 0 THEN round(totals.explore_aiu / totals.aiu, 4) END,
        'acceptedChanges', (SELECT jsonb_build_object(
            'count', count(*),
            'meanUsd', round(avg(c.total_usd), 2),
            'medianUsd', round((percentile_cont(0.5) WITHIN GROUP (ORDER BY c.total_usd))::numeric, 2)) FROM changes AS c),
        'costPerRequirementUsd', (SELECT round(sum(i.ai_usd) / sum(r.requirements), 2)
            FROM kpi_v1.item_costs AS i JOIN kpi_v1.requirement_counts AS r USING (item)),
        'firstPassYield', (SELECT round(avg((v.findings = 0)::int), 4) FROM kpi_v1.reviews AS v WHERE v.round = 1),
        'reworkAiu', (SELECT coalesce(sum(i.rework_aiu), 0) FROM kpi_v1.item_costs AS i),
        'specGaps', jsonb_build_object(
            'count', (SELECT count(*) FROM kpi_v1.spec_gaps),
            'perAcceptedChange', (SELECT round((SELECT count(*) FROM kpi_v1.spec_gaps)::numeric / nullif(count(*), 0), 3) FROM changes)),
        'recentChanges', (SELECT coalesce(jsonb_agg(jsonb_build_object(
            'change', x.change_ref, 'acceptedAt', x.accepted_at, 'calls', x.calls, 'aiu', x.aiu,
            'aiUsd', round(x.ai_usd, 2), 'operatorHours', round(x.operator_hours, 2), 'totalUsd', round(x.total_usd, 2))
            ORDER BY x.accepted_at DESC, x.change_ref), '[]')
            FROM (SELECT * FROM changes ORDER BY accepted_at DESC, change_ref LIMIT 10) AS x))
    FROM totals, spans, entries, prompts;
$$;
