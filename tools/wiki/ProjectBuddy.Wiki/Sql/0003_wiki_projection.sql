-- wiki_projection (#307, #309): the governed projection of the requirement and test pages, and the RSR registry
-- envelopes built from it. The projection is computed from the stored bytes by immutable functions, never stored
-- beside them, so it cannot disagree with them; render_page(parse_page(bytes)) gives the bytes back by construction.

CREATE FUNCTION wiki_v1.parser_version() RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT 'projectbuddy-wiki-pages-v1';
$$;

-- The governed directories: each holds one scope's pages, named after their IDs, with fixed sections, or one marker
-- kind for pages without sections.
CREATE FUNCTION wiki_v1.governed_scopes()
RETURNS TABLE (directory text, scope text, id_prefix text, headings text[], marker_kind text, status_of_marker text)
LANGUAGE sql IMMUTABLE AS $$
    VALUES
        ('system-requirements', 'system', 'SYS-REQ',
         ARRAY['Requirement', 'Rationale', 'Fit Criterion', 'Verification', 'Source', 'Priority', 'TestID', 'TestArtifact', 'CodeRef', 'Status'],
         'retired', 'superseded'),
        ('software-requirements', 'software', 'REQ',
         ARRAY['Requirement', 'Rationale', 'Fit Criterion', 'Verification', 'Source', 'Priority', 'TestID', 'TestArtifact', 'CodeRef', 'Status'],
         'retired', 'superseded'),
        ('tests', 'test', 'TEST', ARRAY['Stimulus', 'Required observation'], 'stub', 'active');
$$;

-- The one canonical page layout. A page with sections is
--   "# <id>[: <title>]\n\n## <heading>\n\n<body>[\n\n## <heading>\n\n<body>...]\n"
-- and a marker page is
--   "# <id>[: <title>]\n\n<!-- page-kind: <kind> -->\n<body>\n"
-- whose body is held as the single section ["Body", <body>].
CREATE FUNCTION wiki_v1.render_page(p_id text, p_title text, p_kind text, p_sections jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '# ' || p_id || coalesce(': ' || p_title, '') || E'\n\n' ||
        CASE p_kind
            WHEN 'sections' THEN
                (SELECT string_agg('## ' || (s.value ->> 0) || E'\n\n' || (s.value ->> 1), E'\n\n' ORDER BY s.ordinality)
                 FROM jsonb_array_elements(p_sections) WITH ORDINALITY AS s)
            ELSE '<!-- page-kind: ' || p_kind || E' -->\n' || (p_sections -> 0 ->> 1)
        END || E'\n';
$$;

-- Splits a page into its parts, or names why it does not have the canonical layout.
CREATE FUNCTION wiki_v1.parse_page(p_content bytea)
RETURNS TABLE (page_id text, title text, page_kind text, sections jsonb, reason text)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_text text;
    v_head text[];
    v_rest text;
    v_marker text[];
    v_source text;
    v_bodies text[];
    v_headings text[];
BEGIN
    BEGIN
        v_text := convert_from(p_content, 'UTF8');
    EXCEPTION WHEN character_not_in_repertoire THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, NULL::jsonb, 'not-utf8';
        RETURN;
    END;

    v_head := regexp_match(v_text, '^# ((?:SYS-REQ|REQ|TEST)-[0-9]{3,})(?:: ([^\n]+))?\n');
    IF v_head IS NULL THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, NULL::jsonb, 'no-id-heading';
        RETURN;
    END IF;

    v_rest := substr(v_text, char_length('# ' || v_head[1] || coalesce(': ' || v_head[2], '')) + 2);
    -- "sections" names the layout with sections, so it is never a marker kind.
    v_marker := regexp_match(v_rest, '^\n<!-- page-kind: ([a-z]+) -->\n(.*)\n$');
    IF v_marker IS NOT NULL AND v_marker[1] <> 'sections' THEN
        RETURN QUERY SELECT v_head[1], v_head[2], v_marker[1], jsonb_build_array(jsonb_build_array('Body', v_marker[2])), NULL::text;
        RETURN;
    END IF;

    IF v_rest !~ '^\n## ' OR right(v_rest, 1) <> E'\n' THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, NULL::jsonb, 'not-canonical';
        RETURN;
    END IF;

    -- "\n" + the rest without its final newline starts with the first separator "\n\n## <heading>\n\n".
    v_source := E'\n' || left(v_rest, -1);
    v_bodies := regexp_split_to_array(v_source, '\n\n## ([^\n]+)\n\n');
    v_headings := ARRAY(
        SELECT m.match[1]
        FROM regexp_matches(v_source, '\n\n## ([^\n]+)\n\n', 'g') WITH ORDINALITY AS m(match, ordinality)
        ORDER BY m.ordinality);
    IF v_bodies[1] <> '' THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text, NULL::jsonb, 'not-canonical';
        RETURN;
    END IF;

    RETURN QUERY SELECT v_head[1], v_head[2], 'sections',
        (SELECT jsonb_agg(jsonb_build_array(h.heading, v_bodies[h.ordinality + 1]) ORDER BY h.ordinality)
         FROM unnest(v_headings) WITH ORDINALITY AS h(heading, ordinality)),
        NULL::text;
END;
$$;

-- A page's registry text: its fields as compact JSON with keys in code-point order, the convention of RSR's source
-- snapshots (Python json.dumps with sort_keys, compact separators and ensure_ascii=False gives the same bytes).
CREATE FUNCTION wiki_v1.node_text(p_id text, p_title text, p_kind text, p_sections jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '{' || string_agg(to_json(f.key)::text || ':' || to_json(f.value)::text, ',' ORDER BY f.key COLLATE "C") || '}'
    FROM (
        SELECT 'ID' AS key, p_id AS value
        UNION ALL SELECT 'Title', p_title WHERE p_title IS NOT NULL
        UNION ALL SELECT 'Page kind', p_kind WHERE p_kind <> 'sections'
        UNION ALL SELECT s.value ->> 0, s.value ->> 1 FROM jsonb_array_elements(p_sections) AS s
    ) AS f;
$$;

-- The IDs a text links to as [[ID]], [[ID|label]] or [[ID#section]], once each, in order of first appearance.
CREATE FUNCTION wiki_v1.page_links(p_text text) RETURNS TABLE (linked_id text)
LANGUAGE sql IMMUTABLE AS $$
    SELECT l.id
    FROM (
        SELECT m.match[1] AS id, min(m.ordinality) AS first
        FROM regexp_matches(p_text, '\[\[((?:SYS-REQ|REQ|TEST)-[0-9]{3,})(?:[|#][^]]*)?\]\]', 'g') WITH ORDINALITY AS m(match, ordinality)
        GROUP BY m.match[1]
    ) AS l
    ORDER BY l.first;
$$;

-- Every file under a governed directory of a state, with its projection and, when it cannot be governed, why.
CREATE FUNCTION wiki_v1.governed_pages(p_snapshot uuid)
RETURNS TABLE (path text, sha256 text, scope text, page_id text, title text, page_kind text, sections jsonb, status text, node_text text, problem text)
LANGUAGE sql STABLE AS $$
    SELECT f.path, f.sha256, d.scope, p.page_id, p.title, p.page_kind, p.sections,
        CASE WHEN p.page_kind = 'sections' THEN 'active' ELSE d.status_of_marker END,
        t.text,
        CASE
            WHEN f.path !~ ('^' || d.directory || '/[^/]+[.]md$') THEN 'unexpected-file'
            WHEN p.reason IS NOT NULL THEN p.reason
            WHEN f.path <> d.directory || '/' || p.page_id || '.md' THEN 'id-does-not-match-path'
            WHEN p.page_id !~ ('^' || d.id_prefix || '-[0-9]{3,}$') THEN 'id-outside-scope'
            WHEN char_length(p.page_id) > 128 THEN 'id-too-long'
            WHEN p.page_kind = 'sections'
                AND (SELECT array_agg(s.value ->> 0 ORDER BY s.ordinality) FROM jsonb_array_elements(p.sections) WITH ORDINALITY AS s) <> d.headings
                THEN 'unexpected-sections'
            WHEN p.page_kind NOT IN ('sections', d.marker_kind) THEN 'unexpected-page-kind'
            WHEN char_length(t.text) > 16384 THEN 'text-too-long'
        END
    FROM wiki_v1.snapshot_files AS f
    JOIN wiki_v1.governed_scopes() AS d ON starts_with(f.path, d.directory || '/')
    JOIN wiki_v1.blobs AS b ON b.sha256 = f.sha256
    CROSS JOIN LATERAL wiki_v1.parse_page(b.content) AS p
    CROSS JOIN LATERAL (SELECT wiki_v1.node_text(p.page_id, p.title, p.page_kind, p.sections) AS text) AS t
    WHERE f.snapshot_uuid = p_snapshot
    ORDER BY f.path COLLATE "C";
$$;

-- The trace edges of a state, each with the scope whose registry snapshot holds it (the scope of its source node),
-- and, when it cannot be exported, why:
--   verifies:   TEST -> SYS-REQ or REQ, from the requirement's TestID field;
--   allocates:  SYS-REQ -> REQ, from the system requirement's CodeRef field;
--   supersedes: REQ -> retired REQ (or SYS-REQ -> retired SYS-REQ), from the retired page's links.
CREATE FUNCTION wiki_v1.governed_edges(p_snapshot uuid)
RETURNS TABLE (from_id text, edge_type text, holder_scope text, to_scope text, to_id text, field text, path text, problem text)
LANGUAGE sql STABLE AS $$
    WITH pages AS (
        SELECT g.* FROM wiki_v1.governed_pages(p_snapshot) AS g WHERE g.problem IS NULL
    ),
    links AS (
        SELECT p.page_id, p.scope, p.page_kind, p.path, s.value ->> 0 AS field, l.linked_id
        FROM pages AS p
        CROSS JOIN LATERAL jsonb_array_elements(p.sections) AS s
        CROSS JOIN LATERAL wiki_v1.page_links(s.value ->> 1) AS l
    ),
    edges AS (
        SELECT l.linked_id AS from_id, 'verifies' AS edge_type, 'test' AS holder_scope, l.scope AS to_scope, l.page_id AS to_id, l.field, l.path,
               CASE WHEN l.linked_id !~ '^TEST-' THEN 'link-outside-field' END AS problem
        FROM links AS l WHERE l.scope IN ('system', 'software') AND l.page_kind = 'sections' AND l.field = 'TestID'
        UNION ALL
        SELECT l.page_id, 'allocates', 'system', 'software', l.linked_id, l.field, l.path,
               CASE WHEN l.linked_id !~ '^REQ-' THEN 'link-outside-field' END
        FROM links AS l WHERE l.scope = 'system' AND l.page_kind = 'sections' AND l.field = 'CodeRef'
        UNION ALL
        SELECT l.linked_id, 'supersedes', l.scope, l.scope, l.page_id, l.field, l.path,
               CASE WHEN l.linked_id = l.page_id OR NOT EXISTS (
                   SELECT 1 FROM pages AS same WHERE same.scope = l.scope AND same.page_id = l.linked_id) THEN 'link-outside-scope' END
        FROM links AS l WHERE l.page_kind = 'retired'
    )
    SELECT e.from_id, e.edge_type, e.holder_scope, e.to_scope, e.to_id, e.field, e.path,
        coalesce(e.problem, CASE
            WHEN NOT EXISTS (SELECT 1 FROM pages AS p WHERE p.scope = e.holder_scope AND p.page_id = e.from_id) THEN 'dangling-link'
            WHEN NOT EXISTS (SELECT 1 FROM pages AS p WHERE p.scope = e.to_scope AND p.page_id = e.to_id) THEN 'dangling-link'
        END)
    FROM edges AS e
    ORDER BY e.holder_scope, e.from_id COLLATE "C", e.edge_type, e.to_scope, e.to_id COLLATE "C";
$$;

-- A summary of a state's projection: pages per scope and kind, edges per type, and every problem.
CREATE FUNCTION wiki_v1.projection_report(p_snapshot uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH pages AS (SELECT * FROM wiki_v1.governed_pages(p_snapshot)),
    edges AS (SELECT * FROM wiki_v1.governed_edges(p_snapshot)),
    problems AS (
        SELECT p.path, NULL::text AS link, p.problem FROM pages AS p WHERE p.problem IS NOT NULL
        UNION ALL
        SELECT e.path, e.from_id || ' ' || e.edge_type || ' ' || e.to_id, e.problem FROM edges AS e WHERE e.problem IS NOT NULL
    )
    SELECT jsonb_build_object(
        'parserVersion', wiki_v1.parser_version(),
        'pages', (SELECT coalesce(jsonb_object_agg(c.scope, c.kinds), '{}') FROM (
            SELECT p.scope, jsonb_object_agg(coalesce(p.page_kind, 'unparsed'), p.count) AS kinds
            FROM (SELECT g.scope, g.page_kind, count(*) AS count FROM pages AS g GROUP BY 1, 2) AS p GROUP BY 1) AS c),
        'edges', (SELECT coalesce(jsonb_object_agg(e.edge_type, e.count), '{}') FROM (
            SELECT g.edge_type, count(*) AS count FROM edges AS g WHERE g.problem IS NULL GROUP BY 1) AS e),
        'problemCount', (SELECT count(*) FROM problems),
        'problems', (SELECT coalesce(jsonb_agg(jsonb_strip_nulls(jsonb_build_object('path', x.path, 'link', x.link, 'problem', x.problem))
                         ORDER BY x.path COLLATE "C", x.link COLLATE "C"), '[]')
                     FROM (SELECT * FROM problems ORDER BY path COLLATE "C", link COLLATE "C" LIMIT 100) AS x));
$$;

-- The rsr/registry-snapshot@1 envelope of one scope of a recorded commit, for registry_v1.ingest_snapshot.
-- p_identity names the registered source: {source_uuid, constellation_uuid, manifest_digest, source_ref}. p_targets
-- names the registry snapshots this scope's edges point into: software needs none, system needs software, and test
-- needs system and software, so the scopes are ingested in that order. Any page or edge that cannot be exported
-- refuses every scope: the registry must never hold a partial wiki. So does an envelope over the registry's limits.
--
-- The registry keys a snapshot by its tree digest, which leaves out the commit: a later commit with the same governed
-- content has the same key and a different source_revision, which the registry refuses as a changed payload. The
-- exporter looks the key up first and reuses that snapshot, as RSR's own bootstrap does.
CREATE FUNCTION wiki_v1.registry_envelope(p_commit text, p_scope text, p_identity jsonb, p_targets jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_snapshot uuid;
    v_needed text[];
    v_report jsonb;
    v_tree text;
    v_envelope jsonb;
BEGIN
    v_snapshot := wiki_v1.commit_snapshot(p_commit);
    IF v_snapshot IS NULL THEN
        RAISE EXCEPTION 'commit % is not recorded', p_commit USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_needed := CASE p_scope WHEN 'software' THEN ARRAY[]::text[] WHEN 'system' THEN ARRAY['software'] WHEN 'test' THEN ARRAY['software', 'system'] END;
    IF v_needed IS NULL THEN
        RAISE EXCEPTION 'scope must be system, software or test' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Each guard is its own statement: SQL does not promise to evaluate OR from left to right.
    IF coalesce(jsonb_typeof(p_identity), '') <> 'object' THEN
        RAISE EXCEPTION 'the source identity must be a JSON object' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(p_identity) AS k) IS DISTINCT FROM ARRAY['constellation_uuid', 'manifest_digest', 'source_ref', 'source_uuid']
        OR coalesce(p_identity ->> 'source_uuid', '') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR coalesce(p_identity ->> 'constellation_uuid', '') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        OR coalesce(p_identity ->> 'manifest_digest', '') !~ '^[0-9a-f]{64}$'
        OR coalesce(btrim(p_identity ->> 'source_ref'), '') = '' THEN
        RAISE EXCEPTION 'the source identity needs exactly source_uuid, constellation_uuid, manifest_digest and source_ref' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF coalesce(jsonb_typeof(p_targets), '') <> 'object' THEN
        RAISE EXCEPTION 'the registry targets must be a JSON object' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF coalesce((SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(p_targets) AS k), ARRAY[]::text[]) <> v_needed
        OR EXISTS (SELECT 1 FROM jsonb_each(p_targets) AS t
                   WHERE jsonb_typeof(t.value) <> 'string' OR t.value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RAISE EXCEPTION 'the % scope needs registry snapshots for exactly: %', p_scope, coalesce(nullif(array_to_string(v_needed, ', '), ''), 'none')
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_report := wiki_v1.projection_report(v_snapshot);
    IF (v_report ->> 'problemCount')::integer > 0 THEN
        RAISE EXCEPTION 'the wiki has % pages or links that cannot be exported', v_report ->> 'problemCount'
            USING ERRCODE = 'data_exception', DETAIL = (v_report -> 'problems')::text;
    END IF;

    -- The idempotency key's tree digest covers this scope's files and the registry snapshots its edges point into.
    v_tree := 'projectbuddy/wiki-scope-tree@1' || E'\n' || p_scope || E'\n' || wiki_v1.parser_version() || E'\n'
        || coalesce((SELECT string_agg(g.path || E'\t' || g.sha256 || E'\n', '' ORDER BY g.path COLLATE "C")
                     FROM wiki_v1.governed_pages(v_snapshot) AS g WHERE g.scope = p_scope), '')
        || coalesce((SELECT string_agg('target' || E'\t' || t.key || E'\t' || t.value || E'\n', '' ORDER BY t.key)
                     FROM jsonb_each_text(p_targets) AS t), '');

    v_envelope := jsonb_build_object(
        'schema', 'rsr/registry-snapshot@1',
        'source_uuid', p_identity ->> 'source_uuid',
        'constellation_uuid', p_identity ->> 'constellation_uuid',
        'manifest_digest', p_identity ->> 'manifest_digest',
        'scope', p_scope,
        'source_ref', p_identity ->> 'source_ref',
        'source_revision', p_commit,
        'tree_digest', encode(sha256(convert_to(v_tree, 'UTF8')), 'hex'),
        'parser_version', wiki_v1.parser_version(),
        'nodes', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'id', g.page_id, 'kind', g.scope, 'status', g.status,
                'content_digest', encode(sha256(convert_to(g.node_text, 'UTF8')), 'hex'),
                'text', g.node_text, 'location', g.path) ORDER BY g.page_id COLLATE "C")
            FROM wiki_v1.governed_pages(v_snapshot) AS g WHERE g.scope = p_scope), '[]'),
        'edges', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'from_id', e.from_id, 'type', e.edge_type,
                'to_snapshot_uuid', CASE WHEN e.to_scope = p_scope THEN NULL ELSE p_targets ->> e.to_scope END,
                'to_id', e.to_id, 'metadata', jsonb_build_object('field', e.field, 'page', e.path))
                ORDER BY e.from_id COLLATE "C", e.edge_type, e.to_scope, e.to_id COLLATE "C")
            FROM wiki_v1.governed_edges(v_snapshot) AS e WHERE e.holder_scope = p_scope), '[]'));

    -- registry_v1.ingest_snapshot's limits.
    IF jsonb_array_length(v_envelope -> 'nodes') > 10000 OR jsonb_array_length(v_envelope -> 'edges') > 50000
        OR octet_length(v_envelope::text) > 16777216 THEN
        RAISE EXCEPTION 'the % envelope exceeds the registry limits of 10000 nodes, 50000 edges and 16 MiB', p_scope
            USING ERRCODE = 'program_limit_exceeded';
    END IF;

    RETURN v_envelope;
END;
$$;
