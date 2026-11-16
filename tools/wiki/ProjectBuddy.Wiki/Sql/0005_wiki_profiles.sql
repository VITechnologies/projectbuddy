-- wiki_profiles (#307, #329, rsr/ecosystem-wiki-standard@2): each member wiki declares its governed scopes in a root
-- `.wiki-profile.json` (without one, projectbuddy's own profile applies), and the whole-wiki rules that
-- scripts/Assemble-WikiSpec.py --strict enforces (hub membership, page budget) become findings (#325).

CREATE OR REPLACE FUNCTION wiki_v1.parser_version() RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT 'projectbuddy-wiki-pages-v2';
$$;

-- The profile a wiki without `.wiki-profile.json` has: @1's three scopes.
CREATE FUNCTION wiki_v1.default_profile() RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object('schema', 'rsr/wiki-profile@1', 'scopes', jsonb_build_array(
        jsonb_build_object('scope', 'system', 'directory', 'system-requirements', 'idPrefix', 'SYS-REQ', 'sections',
            jsonb_build_array('Requirement', 'Rationale', 'Fit Criterion', 'Verification', 'Source', 'Priority', 'TestID', 'TestArtifact', 'CodeRef', 'Status')),
        jsonb_build_object('scope', 'software', 'directory', 'software-requirements', 'idPrefix', 'REQ', 'sections',
            jsonb_build_array('Requirement', 'Rationale', 'Fit Criterion', 'Verification', 'Source', 'Priority', 'TestID', 'TestArtifact', 'CodeRef', 'Status')),
        jsonb_build_object('scope', 'test', 'directory', 'tests', 'idPrefix', 'TEST', 'sections', jsonb_build_array('Stimulus', 'Required observation'))));
$$;

-- Whether a profile is not a valid rsr/wiki-profile@1: exactly the keys schema and scopes; one to three scopes, each
-- with exactly the string keys scope (system, software or test, once), directory (one plain path segment, once) and
-- idPrefix (upper-case words joined by '-', once), and sections (distinct, single-line, non-blank headings). Each
-- guard is its own statement, so no JSON function sees a value of the wrong type.
CREATE FUNCTION wiki_v1.profile_is_invalid(p_profile jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_scope jsonb;
    v_heading jsonb;
BEGIN
    IF jsonb_typeof(p_profile) IS DISTINCT FROM 'object'
        OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(p_profile) AS k) IS DISTINCT FROM ARRAY['schema', 'scopes'] THEN
        RETURN true;
    END IF;

    IF p_profile ->> 'schema' IS DISTINCT FROM 'rsr/wiki-profile@1' OR jsonb_typeof(p_profile -> 'scopes') IS DISTINCT FROM 'array' THEN
        RETURN true;
    END IF;

    IF jsonb_array_length(p_profile -> 'scopes') NOT BETWEEN 1 AND 3 THEN
        RETURN true;
    END IF;

    FOR v_scope IN SELECT value FROM jsonb_array_elements(p_profile -> 'scopes') LOOP
        IF jsonb_typeof(v_scope) IS DISTINCT FROM 'object' THEN
            RETURN true;
        END IF;

        IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(v_scope) AS k) IS DISTINCT FROM ARRAY['directory', 'idPrefix', 'scope', 'sections']
            OR jsonb_typeof(v_scope -> 'scope') <> 'string' OR jsonb_typeof(v_scope -> 'directory') <> 'string'
            OR jsonb_typeof(v_scope -> 'idPrefix') <> 'string' OR jsonb_typeof(v_scope -> 'sections') <> 'array' THEN
            RETURN true;
        END IF;

        IF v_scope ->> 'scope' NOT IN ('system', 'software', 'test')
            OR v_scope ->> 'directory' !~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'
            OR v_scope ->> 'idPrefix' !~ '^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)*$'
            OR jsonb_array_length(v_scope -> 'sections') = 0 THEN
            RETURN true;
        END IF;

        FOR v_heading IN SELECT value FROM jsonb_array_elements(v_scope -> 'sections') LOOP
            IF jsonb_typeof(v_heading) <> 'string' THEN
                RETURN true;
            END IF;

            IF v_heading #>> '{}' !~ '^[^\n]*[^\s][^\n]*$' THEN
                RETURN true;
            END IF;
        END LOOP;

        IF (SELECT count(DISTINCT h) FROM jsonb_array_elements_text(v_scope -> 'sections') AS h) <> jsonb_array_length(v_scope -> 'sections') THEN
            RETURN true;
        END IF;
    END LOOP;

    RETURN (SELECT count(DISTINCT s ->> 'scope') <> count(*) OR count(DISTINCT s ->> 'directory') <> count(*) OR count(DISTINCT s ->> 'idPrefix') <> count(*)
            FROM jsonb_array_elements(p_profile -> 'scopes') AS s);
END;
$$;

-- A page's text, or NULL when its bytes are not UTF-8.
CREATE FUNCTION wiki_v1.utf8_text(p_content bytea) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN convert_from(p_content, 'UTF8');
EXCEPTION WHEN character_not_in_repertoire THEN
    RETURN NULL;
END;
$$;

-- A state's profile, where it came from (default or wiki), and invalid-profile when its `.wiki-profile.json` is not a
-- valid profile, which governs nothing.
CREATE FUNCTION wiki_v1.snapshot_profile(p_snapshot uuid) RETURNS TABLE (profile jsonb, source text, problem text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_content bytea;
    v_profile jsonb;
BEGIN
    SELECT b.content INTO v_content FROM wiki_v1.snapshot_files AS f JOIN wiki_v1.blobs AS b USING (sha256)
    WHERE f.snapshot_uuid = p_snapshot AND f.path = '.wiki-profile.json';
    IF NOT FOUND THEN
        RETURN QUERY SELECT wiki_v1.default_profile(), 'default', NULL::text;
        RETURN;
    END IF;

    BEGIN
        v_profile := wiki_v1.utf8_text(v_content)::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        v_profile := NULL;
    END;

    IF v_profile IS NULL OR wiki_v1.profile_is_invalid(v_profile) THEN
        RETURN QUERY SELECT jsonb_build_object('schema', 'rsr/wiki-profile@1', 'scopes', '[]'::jsonb), 'wiki', 'invalid-profile';
        RETURN;
    END IF;

    RETURN QUERY SELECT v_profile, 'wiki', NULL::text;
END;
$$;

-- The governed directories of a state, from its profile. A requirement scope's marker kind is retired (superseded);
-- the test scope's is stub (active).
CREATE FUNCTION wiki_v1.governed_scopes(p_snapshot uuid)
RETURNS TABLE (directory text, scope text, id_prefix text, headings text[], marker_kind text, status_of_marker text)
LANGUAGE sql STABLE AS $$
    SELECT s ->> 'directory', s ->> 'scope', s ->> 'idPrefix', ARRAY(SELECT jsonb_array_elements_text(s -> 'sections')),
        CASE s ->> 'scope' WHEN 'test' THEN 'stub' ELSE 'retired' END,
        CASE s ->> 'scope' WHEN 'test' THEN 'active' ELSE 'superseded' END
    FROM wiki_v1.snapshot_profile(p_snapshot) AS p CROSS JOIN LATERAL jsonb_array_elements(p.profile -> 'scopes') AS s;
$$;

DROP FUNCTION wiki_v1.governed_scopes();

-- The IDs with one of the profile's prefixes that a text links to as [[ID]], [[ID|label]] or [[ID#section]], once
-- each, in order of first appearance. Prefixes are upper-case words joined by '-', so they need no escaping.
CREATE FUNCTION wiki_v1.page_links(p_text text, p_prefixes text[]) RETURNS TABLE (linked_id text)
LANGUAGE sql IMMUTABLE AS $$
    SELECT l.id
    FROM (
        SELECT m.match[1] AS id, min(m.ordinality) AS first
        FROM regexp_matches(p_text, '\[\[((?:' || array_to_string(p_prefixes, '|') || ')-[0-9]{3,})(?:[|#][^]]*)?\]\]', 'g') WITH ORDINALITY AS m(match, ordinality)
        WHERE cardinality(p_prefixes) > 0
        GROUP BY m.match[1]
    ) AS l
    ORDER BY l.first;
$$;

DROP FUNCTION wiki_v1.page_links(text);

-- Whole-wiki findings over every Markdown page outside dot-prefixed paths, as scripts/Assemble-WikiSpec.py --strict
-- decides them. When the wiki has a hub (a page that is not an ID page, holds a table or sections marker, and is no
-- page's child), every other page must be an ID page, a hub, a child in some page's final sections index, or marked
-- `<!-- page-kind: note -->` directly under its title (page-not-indexed). Every page is at most 16,384 bytes
-- (page-over-budget). The first that applies names the page.
CREATE FUNCTION wiki_v1.wiki_pages(p_snapshot uuid) RETURNS TABLE (path text, problem text)
LANGUAGE sql STABLE AS $$
    WITH files AS (
        -- A page that is not UTF-8 is left to the finding of its own family.
        SELECT f.path, regexp_replace(regexp_replace(f.path, '^.*/', ''), '[.]md$', '') AS name, b.content, t.text
        FROM wiki_v1.snapshot_files AS f JOIN wiki_v1.blobs AS b USING (sha256)
        CROSS JOIN LATERAL (SELECT wiki_v1.utf8_text(b.content) AS text) AS t
        WHERE f.snapshot_uuid = p_snapshot AND f.path ~ '[.]md$' AND f.path !~ '(^|/)[.]' AND t.text IS NOT NULL
    ),
    pages AS (
        SELECT f.*, f.name ~ ('^(?:' || (SELECT string_agg(s.id_prefix, '|') FROM wiki_v1.governed_scopes(p_snapshot) AS s) || ')-[0-9]{3,}$') IS TRUE AS is_id
        FROM files AS f
    ),
    children AS (
        SELECT DISTINCT c.match[1] AS name
        FROM pages AS p
        CROSS JOIN LATERAL regexp_match(p.text, '\n<!-- sections -->\n((?:- \[\[[^]\n]+\]\]\n)*)<!-- /sections -->\n$') AS block
        CROSS JOIN LATERAL regexp_matches(block[1], '- \[\[([^]\n]+)\]\]\n', 'g') AS c(match)
    ),
    roots AS (
        SELECT p.name FROM pages AS p
        WHERE NOT p.is_id AND p.text ~ '<!-- /?(?:table|sections)\y' AND p.name NOT IN (SELECT c.name FROM children AS c)
    )
    SELECT p.path,
        CASE
            WHEN EXISTS (SELECT 1 FROM roots) AND NOT p.is_id AND p.name NOT IN (SELECT r.name FROM roots AS r)
                 AND p.name NOT IN (SELECT c.name FROM children AS c) AND p.text !~ '^[^\n]*\n\n<!-- page-kind: note -->\n'
                THEN 'page-not-indexed'
            WHEN octet_length(p.content) > 16384 THEN 'page-over-budget'
        END
    FROM pages AS p
    ORDER BY p.path COLLATE "C";
$$;

CREATE OR REPLACE FUNCTION wiki_v1.parse_page(p_content bytea)
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

    v_head := regexp_match(v_text, '^# ([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-[0-9]{3,})(?:: ([^\n]+))?\n');
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

CREATE OR REPLACE FUNCTION wiki_v1.governed_pages(p_snapshot uuid)
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
    JOIN wiki_v1.governed_scopes(p_snapshot) AS d ON starts_with(f.path, d.directory || '/')
    JOIN wiki_v1.blobs AS b ON b.sha256 = f.sha256
    CROSS JOIN LATERAL wiki_v1.parse_page(b.content) AS p
    CROSS JOIN LATERAL (SELECT wiki_v1.node_text(p.page_id, p.title, p.page_kind, p.sections) AS text) AS t
    WHERE f.snapshot_uuid = p_snapshot
    ORDER BY f.path COLLATE "C";
$$;

CREATE OR REPLACE FUNCTION wiki_v1.governed_edges(p_snapshot uuid)
RETURNS TABLE (from_id text, edge_type text, holder_scope text, to_scope text, to_id text, field text, path text, problem text)
LANGUAGE sql STABLE AS $$
    WITH scopes AS (SELECT * FROM wiki_v1.governed_scopes(p_snapshot)),
    pages AS (
        SELECT g.* FROM wiki_v1.governed_pages(p_snapshot) AS g WHERE g.problem IS NULL
    ),
    links AS (
        SELECT p.page_id, p.scope, p.page_kind, p.path, s.value ->> 0 AS field, l.linked_id
        FROM pages AS p
        CROSS JOIN LATERAL jsonb_array_elements(p.sections) AS s
        CROSS JOIN LATERAL wiki_v1.page_links(s.value ->> 1, ARRAY(SELECT x.id_prefix FROM scopes AS x)) AS l
    ),
    edges AS (
        SELECT l.linked_id AS from_id, 'verifies' AS edge_type, 'test' AS holder_scope, l.scope AS to_scope, l.page_id AS to_id, l.field, l.path,
               CASE WHEN NOT EXISTS (SELECT 1 FROM scopes AS x WHERE x.scope = 'test' AND starts_with(l.linked_id, x.id_prefix || '-')) THEN 'link-outside-field' END AS problem
        FROM links AS l WHERE l.scope IN ('system', 'software') AND l.page_kind = 'sections' AND l.field = 'TestID'
        UNION ALL
        SELECT l.page_id, 'allocates', 'system', 'software', l.linked_id, l.field, l.path,
               CASE WHEN NOT EXISTS (SELECT 1 FROM scopes AS x WHERE x.scope = 'software' AND starts_with(l.linked_id, x.id_prefix || '-')) THEN 'link-outside-field' END
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

CREATE OR REPLACE FUNCTION wiki_v1.registry_envelope(p_commit text, p_scope text, p_identity jsonb, p_targets jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_snapshot uuid;
    v_needed text[];
    v_report jsonb;
    v_tree text;
    v_envelope jsonb;
    v_profile jsonb;
BEGIN
    v_snapshot := wiki_v1.commit_snapshot(p_commit);
    IF v_snapshot IS NULL THEN
        RAISE EXCEPTION 'commit % is not recorded', p_commit USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_needed := CASE p_scope WHEN 'software' THEN ARRAY[]::text[] WHEN 'system' THEN ARRAY['software'] WHEN 'test' THEN ARRAY['software', 'system'] END;
    IF v_needed IS NULL THEN
        RAISE EXCEPTION 'scope must be system, software or test' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_report := wiki_v1.projection_report(v_snapshot);
    IF (v_report ->> 'problemCount')::integer > 0 THEN
        RAISE EXCEPTION 'the wiki has % pages or links that cannot be exported', v_report ->> 'problemCount'
            USING ERRCODE = 'data_exception', DETAIL = (v_report -> 'problems')::text;
    END IF;

    -- A scope's edges point only into the scopes the wiki's profile holds.
    SELECT p.profile INTO v_profile FROM wiki_v1.snapshot_profile(v_snapshot) AS p;
    IF NOT EXISTS (SELECT 1 FROM wiki_v1.governed_scopes(v_snapshot) AS x WHERE x.scope = p_scope) THEN
        RAISE EXCEPTION 'the wiki profile has no % scope', p_scope USING ERRCODE = 'invalid_parameter_value';
    END IF;

    v_needed := ARRAY(SELECT n FROM unnest(v_needed) AS n WHERE EXISTS (SELECT 1 FROM wiki_v1.governed_scopes(v_snapshot) AS x WHERE x.scope = n) ORDER BY n);

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


    -- The idempotency key's tree digest covers this scope's files and the registry snapshots its edges point into.
    v_tree := 'projectbuddy/wiki-scope-tree@1' || E'\n' || p_scope || E'\n' || wiki_v1.parser_version() || E'\n'
        || 'profile' || E'\t' || encode(sha256(convert_to(v_profile::text, 'UTF8')), 'hex') || E'\n'
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

CREATE OR REPLACE FUNCTION wiki_v1.projection_report(p_snapshot uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH profile AS (SELECT * FROM wiki_v1.snapshot_profile(p_snapshot)),
    pages AS (SELECT * FROM wiki_v1.governed_pages(p_snapshot)),
    wiki AS (SELECT * FROM wiki_v1.wiki_pages(p_snapshot)),
    edges AS (SELECT * FROM wiki_v1.governed_edges(p_snapshot)),
    architecture AS (SELECT * FROM wiki_v1.architecture_pages(p_snapshot)),
    problems AS (
        SELECT '.wiki-profile.json' AS path, NULL::text AS link, f.problem FROM profile AS f WHERE f.problem IS NOT NULL
        UNION ALL
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
                     FROM (SELECT * FROM problems ORDER BY path COLLATE "C", link COLLATE "C" LIMIT 100) AS x),
        'architecture', jsonb_build_object(
            'pages', (SELECT count(*) FROM architecture),
            'hubs', (SELECT count(*) FROM architecture AS a WHERE a.children IS NOT NULL)),
        'profile', (SELECT jsonb_build_object('source', f.source, 'scopes', coalesce(jsonb_agg(x.value ->> 'scope' ORDER BY x.value ->> 'scope') FILTER (WHERE x.value IS NOT NULL), '[]'))
                    FROM profile AS f LEFT JOIN LATERAL jsonb_array_elements(f.profile -> 'scopes') AS x ON true GROUP BY f.source),
        'wikiProblemCount', (SELECT count(*) FROM wiki AS w WHERE w.problem IS NOT NULL),
        'wikiProblems', (SELECT coalesce(jsonb_agg(jsonb_build_object('path', x.path, 'problem', x.problem) ORDER BY x.path COLLATE "C"), '[]')
                         FROM (SELECT w.path, w.problem FROM wiki AS w WHERE w.problem IS NOT NULL ORDER BY w.path COLLATE "C" LIMIT 100) AS x),
        'architectureProblemCount', (SELECT count(*) FROM architecture AS a WHERE a.problem IS NOT NULL),
        'architectureProblems', (SELECT coalesce(jsonb_agg(jsonb_build_object('path', x.path, 'problem', x.problem) ORDER BY x.path COLLATE "C"), '[]')
                                 FROM (SELECT a.path, a.problem FROM architecture AS a WHERE a.problem IS NOT NULL
                                       ORDER BY a.path COLLATE "C" LIMIT 100) AS x));
$$;

-- Realistic row estimates. A set-returning function defaults to 1,000 rows, and these functions nest, so a whole-wiki
-- query was estimated at billions of rows, which made PostgreSQL JIT-compile it: the projection report took 1.7 s
-- instead of 0.14 s.
ALTER FUNCTION wiki_v1.snapshot_profile(uuid) ROWS 1;
ALTER FUNCTION wiki_v1.governed_scopes(uuid) ROWS 3;
ALTER FUNCTION wiki_v1.parse_page(bytea) ROWS 1;
ALTER FUNCTION wiki_v1.parse_architecture_page(bytea) ROWS 1;
ALTER FUNCTION wiki_v1.page_links(text, text[]) ROWS 3;
ALTER FUNCTION wiki_v1.governed_pages(uuid) ROWS 300;
ALTER FUNCTION wiki_v1.governed_edges(uuid) ROWS 400;
ALTER FUNCTION wiki_v1.architecture_pages(uuid) ROWS 50;
ALTER FUNCTION wiki_v1.wiki_pages(uuid) ROWS 400;
ALTER FUNCTION wiki_v1.export_snapshot(uuid) ROWS 400;
ALTER FUNCTION wiki_v1.changed_paths(uuid, uuid) ROWS 10;
