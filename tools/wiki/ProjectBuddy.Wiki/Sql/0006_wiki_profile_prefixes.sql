-- wiki_profile_prefixes (#348): `rsr/wiki-profile@2` lets one scope declare several ID prefixes, so a member such as
-- mprr can verify its requirements with two test-ID families (TEST-UNIT-NNN and TEST-DOC-NNN). `rsr/wiki-profile@1`,
-- the default profile, the finding catalogues, the layouts and the registry projection are unchanged. Wherever 0005
-- used a scope's single prefix, these functions use its set of prefixes.

-- A scope's ID prefixes: its idPrefixes, or its one idPrefix. Only for a scope whose prefixes are already checked.
CREATE FUNCTION wiki_v1.scope_id_prefixes(p_scope jsonb) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY(SELECT jsonb_array_elements_text(coalesce(p_scope -> 'idPrefixes', jsonb_build_array(p_scope -> 'idPrefix'))));
$$;

-- Whether a profile is not a valid rsr/wiki-profile@1 or rsr/wiki-profile@2: exactly the keys schema and scopes; one to
-- three scopes, each with exactly the string keys scope (system, software or test, once) and directory (one plain path
-- segment, once), sections (distinct, single-line, non-blank headings other than the registry text's own keys ID,
-- Title, Page kind and Body), and its prefixes. In @1 the prefixes are the string idPrefix; in @2 they are either
-- idPrefix or idPrefixes, a non-empty array of strings, but not both. Every prefix is upper-case words joined by '-',
-- and no prefix appears twice in the profile, within a scope or across scopes. Each guard is its own statement, so no
-- JSON function sees a value of the wrong type.
CREATE OR REPLACE FUNCTION wiki_v1.profile_is_invalid(p_profile jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_scope jsonb;
    v_keys text[];
    v_prefixes jsonb;
    v_prefix jsonb;
    v_heading jsonb;
BEGIN
    IF jsonb_typeof(p_profile) IS DISTINCT FROM 'object'
        OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(p_profile) AS k) IS DISTINCT FROM ARRAY['schema', 'scopes'] THEN
        RETURN true;
    END IF;

    IF coalesce(p_profile ->> 'schema', '') NOT IN ('rsr/wiki-profile@1', 'rsr/wiki-profile@2')
        OR jsonb_typeof(p_profile -> 'scopes') IS DISTINCT FROM 'array' THEN
        RETURN true;
    END IF;

    IF jsonb_array_length(p_profile -> 'scopes') NOT BETWEEN 1 AND 3 THEN
        RETURN true;
    END IF;

    FOR v_scope IN SELECT value FROM jsonb_array_elements(p_profile -> 'scopes') LOOP
        IF jsonb_typeof(v_scope) IS DISTINCT FROM 'object' THEN
            RETURN true;
        END IF;

        -- idPrefixes exists only in @2, and a scope has exactly one of idPrefix and idPrefixes.
        v_keys := ARRAY(SELECT k FROM jsonb_object_keys(v_scope) AS k ORDER BY k);
        IF NOT (v_keys = ARRAY['directory', 'idPrefix', 'scope', 'sections']
                OR (p_profile ->> 'schema' = 'rsr/wiki-profile@2' AND v_keys = ARRAY['directory', 'idPrefixes', 'scope', 'sections'])) THEN
            RETURN true;
        END IF;

        IF jsonb_typeof(v_scope -> 'scope') <> 'string' OR jsonb_typeof(v_scope -> 'directory') <> 'string'
            OR jsonb_typeof(v_scope -> 'sections') <> 'array' THEN
            RETURN true;
        END IF;

        IF v_scope ->> 'scope' NOT IN ('system', 'software', 'test')
            OR v_scope ->> 'directory' !~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'
            OR jsonb_array_length(v_scope -> 'sections') = 0 THEN
            RETURN true;
        END IF;

        -- The one idPrefix is held as a one-element list, so both keys follow the same rules.
        v_prefixes := coalesce(v_scope -> 'idPrefixes', jsonb_build_array(v_scope -> 'idPrefix'));
        IF jsonb_typeof(v_prefixes) <> 'array' THEN
            RETURN true;
        END IF;

        IF jsonb_array_length(v_prefixes) = 0 THEN
            RETURN true;
        END IF;

        FOR v_prefix IN SELECT value FROM jsonb_array_elements(v_prefixes) LOOP
            IF jsonb_typeof(v_prefix) <> 'string' THEN
                RETURN true;
            END IF;

            IF v_prefix #>> '{}' !~ '^[A-Z][A-Z0-9]*(-[A-Z][A-Z0-9]*)*$' THEN
                RETURN true;
            END IF;
        END LOOP;

        FOR v_heading IN SELECT value FROM jsonb_array_elements(v_scope -> 'sections') LOOP
            IF jsonb_typeof(v_heading) <> 'string' THEN
                RETURN true;
            END IF;

            -- ID, Title, Page kind and Body are the other keys of a page's registry text, so no section may share them.
            IF v_heading #>> '{}' !~ '^[^\n]*[^\s][^\n]*$' OR v_heading #>> '{}' IN ('ID', 'Title', 'Page kind', 'Body') THEN
                RETURN true;
            END IF;
        END LOOP;

        IF (SELECT count(DISTINCT h) FROM jsonb_array_elements_text(v_scope -> 'sections') AS h) <> jsonb_array_length(v_scope -> 'sections') THEN
            RETURN true;
        END IF;
    END LOOP;

    -- No two scopes share a scope or a directory, and no prefix appears twice among all the profile's prefixes: two
    -- scopes never share one, and one scope never lists one twice.
    RETURN (SELECT count(DISTINCT s ->> 'scope') <> count(*) OR count(DISTINCT s ->> 'directory') <> count(*)
            FROM jsonb_array_elements(p_profile -> 'scopes') AS s)
        OR (SELECT count(DISTINCT x) <> count(*)
            FROM jsonb_array_elements(p_profile -> 'scopes') AS s CROSS JOIN LATERAL unnest(wiki_v1.scope_id_prefixes(s)) AS x);
END;
$$;

-- The governed directories of a state, from its profile, each with its set of ID prefixes. Its result columns change
-- (id_prefix becomes id_prefixes), which CREATE OR REPLACE cannot do, so it is dropped and created again; the functions
-- that call it are replaced below.
DROP FUNCTION wiki_v1.governed_scopes(uuid);

CREATE FUNCTION wiki_v1.governed_scopes(p_snapshot uuid)
RETURNS TABLE (directory text, scope text, id_prefixes text[], headings text[], marker_kind text, status_of_marker text)
LANGUAGE sql STABLE AS $$
    SELECT s ->> 'directory', s ->> 'scope', wiki_v1.scope_id_prefixes(s), ARRAY(SELECT jsonb_array_elements_text(s -> 'sections')),
        CASE s ->> 'scope' WHEN 'test' THEN 'stub' ELSE 'retired' END,
        CASE s ->> 'scope' WHEN 'test' THEN 'active' ELSE 'superseded' END
    FROM wiki_v1.snapshot_profile(p_snapshot) AS p CROSS JOIN LATERAL jsonb_array_elements(p.profile -> 'scopes') AS s;
$$;

-- As in 0005, except that a page is an ID page when its name is any prefix of any scope followed by three or more
-- digits.
CREATE OR REPLACE FUNCTION wiki_v1.wiki_pages(p_snapshot uuid) RETURNS TABLE (path text, problem text)
LANGUAGE sql STABLE AS $$
    WITH files AS (
        SELECT f.path, regexp_replace(regexp_replace(f.path, '^.*/', ''), '[.]md$', '') AS name, b.content, t.text,
               f.path = 'Architecture-Packet.md' OR starts_with(f.path, 'architecture/')
                   OR EXISTS (SELECT 1 FROM wiki_v1.governed_scopes(p_snapshot) AS s WHERE starts_with(f.path, s.directory || '/')) AS in_family
        FROM wiki_v1.snapshot_files AS f JOIN wiki_v1.blobs AS b USING (sha256)
        CROSS JOIN LATERAL (SELECT wiki_v1.utf8_text(b.content) AS text) AS t
        WHERE f.snapshot_uuid = p_snapshot AND f.path ~ '[.]md$' AND f.path !~ '(^|/)[.]'
    ),
    unreadable AS (
        SELECT f.path FROM files AS f WHERE f.text IS NULL AND NOT f.in_family
    ),
    pages AS (
        SELECT f.*, f.name ~ ('^(?:' || (SELECT string_agg(array_to_string(s.id_prefixes, '|'), '|') FROM wiki_v1.governed_scopes(p_snapshot) AS s)
                              || ')-[0-9]{3,}$') IS TRUE AS is_id
        FROM files AS f WHERE f.text IS NOT NULL
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
    SELECT x.path, x.problem FROM (
    SELECT u.path, 'not-utf8' AS problem FROM unreadable AS u
    UNION ALL
    SELECT p.path,
        CASE
            WHEN EXISTS (SELECT 1 FROM roots) AND NOT p.is_id AND p.name NOT IN (SELECT r.name FROM roots AS r)
                 AND p.name NOT IN (SELECT c.name FROM children AS c) AND p.text !~ '^[^\n]*\n\n<!-- page-kind: note -->\n'
                THEN 'page-not-indexed'
            WHEN octet_length(p.content) > 16384 THEN 'page-over-budget'
        END AS problem
    FROM pages AS p
    ) AS x
    ORDER BY x.path COLLATE "C";
$$;

-- As in 0005, except that a page's ID is in its scope when it is any of the scope's prefixes followed by three or more
-- digits.
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
            WHEN p.page_id !~ ('^(?:' || array_to_string(d.id_prefixes, '|') || ')-[0-9]{3,}$') THEN 'id-outside-scope'
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

-- As in 0005, except that links count with every prefix of every scope, and a TestID (CodeRef) link must be an ID with
-- any of the test (software) scope's prefixes.
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
        CROSS JOIN LATERAL wiki_v1.page_links(s.value ->> 1, ARRAY(SELECT x.prefix FROM scopes AS c CROSS JOIN LATERAL unnest(c.id_prefixes) AS x(prefix))) AS l
    ),
    edges AS (
        SELECT l.linked_id AS from_id, 'verifies' AS edge_type, 'test' AS holder_scope, l.scope AS to_scope, l.page_id AS to_id, l.field, l.path,
               CASE WHEN NOT EXISTS (SELECT 1 FROM scopes AS x WHERE x.scope = 'test' AND l.linked_id ~ ('^(?:' || array_to_string(x.id_prefixes, '|') || ')-[0-9]{3,}$')) THEN 'link-outside-field' END AS problem
        FROM links AS l WHERE l.scope IN ('system', 'software') AND l.page_kind = 'sections' AND l.field = 'TestID'
        UNION ALL
        SELECT l.page_id, 'allocates', 'system', 'software', l.linked_id, l.field, l.path,
               CASE WHEN NOT EXISTS (SELECT 1 FROM scopes AS x WHERE x.scope = 'software' AND l.linked_id ~ ('^(?:' || array_to_string(x.id_prefixes, '|') || ')-[0-9]{3,}$')) THEN 'link-outside-field' END
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

-- CREATE OR REPLACE resets a function's row estimate, and governed_scopes is new, so 0005's estimates are set again.
ALTER FUNCTION wiki_v1.governed_scopes(uuid) ROWS 3;
ALTER FUNCTION wiki_v1.governed_pages(uuid) ROWS 300;
ALTER FUNCTION wiki_v1.governed_edges(uuid) ROWS 400;
ALTER FUNCTION wiki_v1.wiki_pages(uuid) ROWS 400;
