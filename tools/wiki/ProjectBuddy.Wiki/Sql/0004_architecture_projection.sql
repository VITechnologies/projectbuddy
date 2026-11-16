-- architecture_projection (#307, #315): the Architecture Packet pages, `Architecture-Packet.md` and `architecture/*.md`,
-- in one canonical layout, so drafts (#310) can edit their fields and render them deterministically. They have no
-- registry scope: their problems are reported apart and never keep the requirement and test pages out of the registry.

-- The canonical layout: "# <title>\n\n<body>\n", or, for a hub, "# <title>\n\n[<body>\n\n]<!-- sections -->\n" followed
-- by "- [[<child>]]\n" per child and "<!-- /sections -->\n" (the index block scripts/Assemble-WikiSpec.py fills). A hub
-- without a body has a NULL body.
CREATE FUNCTION wiki_v1.render_architecture_page(p_title text, p_body text, p_children text[]) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '# ' || p_title || E'\n\n' ||
        CASE WHEN p_children IS NULL THEN p_body || E'\n'
             ELSE coalesce(p_body || E'\n\n', '') || E'<!-- sections -->\n'
                  || coalesce((SELECT string_agg('- [[' || c.child || E']]\n', '' ORDER BY c.ordinality)
                               FROM unnest(p_children) WITH ORDINALITY AS c(child, ordinality)), '')
                  || E'<!-- /sections -->\n'
        END;
$$;

CREATE FUNCTION wiki_v1.parse_architecture_page(p_content bytea)
RETURNS TABLE (title text, body text, children text[], reason text)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    v_text text;
    v_head text[];
    v_rest text;
    v_hub text[];
BEGIN
    BEGIN
        v_text := convert_from(p_content, 'UTF8');
    EXCEPTION WHEN character_not_in_repertoire THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text[], 'not-utf8';
        RETURN;
    END;

    v_head := regexp_match(v_text, '^# ([^\n]+)\n\n');
    IF v_head IS NULL THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text[], 'no-title';
        RETURN;
    END IF;

    IF right(v_text, 1) <> E'\n' THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text[], 'no-final-newline';
        RETURN;
    END IF;

    v_rest := substr(v_text, char_length(v_head[1]) + 5);
    IF v_rest = '' THEN
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text[], 'not-canonical';
        RETURN;
    END IF;

    v_hub := regexp_match(v_rest, '^(?:(.*)\n\n)?<!-- sections -->\n((?:- \[\[[^]\n]+\]\]\n)*)<!-- /sections -->\n$');
    IF v_hub IS NULL THEN
        RETURN QUERY SELECT v_head[1], left(v_rest, -1), NULL::text[], NULL::text;
        RETURN;
    END IF;

    RETURN QUERY SELECT v_head[1], v_hub[1],
        ARRAY(SELECT m.match[1] FROM regexp_matches(v_hub[2], '- \[\[([^]\n]+)\]\]\n', 'g') WITH ORDINALITY AS m(match, ordinality) ORDER BY m.ordinality),
        NULL::text;
END;
$$;

-- Every Architecture Packet page of a state, with its parts or why it cannot be governed. A hub's child must name a
-- page of the state by file name, as scripts/Assemble-WikiSpec.py resolves it (Markdown files outside dot
-- directories). A sections marker anywhere but in a hub's final index block is a problem, so a page without problems
-- has the hub structure the assembler accepts.
CREATE FUNCTION wiki_v1.architecture_pages(p_snapshot uuid)
RETURNS TABLE (path text, title text, body text, children text[], problem text)
LANGUAGE sql STABLE AS $$
    SELECT f.path, p.title, p.body, p.children,
        CASE
            WHEN f.path <> 'Architecture-Packet.md' AND f.path !~ '^architecture/[^/]+[.]md$' THEN 'unexpected-file'
            WHEN p.reason IS NOT NULL THEN p.reason
            WHEN p.body ~ '<!-- /?sections\y' THEN 'misplaced-index-marker'
            WHEN EXISTS (
                SELECT 1 FROM unnest(p.children) AS c(child)
                WHERE NOT EXISTS (SELECT 1 FROM wiki_v1.snapshot_files AS g
                                  WHERE g.snapshot_uuid = p_snapshot AND g.path !~ '(^|/)[.]'
                                    AND regexp_replace(g.path, '^.*/', '') = c.child || '.md'))
                THEN 'dangling-child'
        END
    FROM wiki_v1.snapshot_files AS f
    JOIN wiki_v1.blobs AS b ON b.sha256 = f.sha256
    CROSS JOIN LATERAL wiki_v1.parse_architecture_page(b.content) AS p
    WHERE f.snapshot_uuid = p_snapshot AND (f.path = 'Architecture-Packet.md' OR starts_with(f.path, 'architecture/'))
    ORDER BY f.path COLLATE "C";
$$;

-- projection_report, now with the Architecture Packet: its pages and hubs, and its problems apart from the problems
-- that keep the wiki out of the registry.
CREATE OR REPLACE FUNCTION wiki_v1.projection_report(p_snapshot uuid) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH pages AS (SELECT * FROM wiki_v1.governed_pages(p_snapshot)),
    edges AS (SELECT * FROM wiki_v1.governed_edges(p_snapshot)),
    architecture AS (SELECT * FROM wiki_v1.architecture_pages(p_snapshot)),
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
                     FROM (SELECT * FROM problems ORDER BY path COLLATE "C", link COLLATE "C" LIMIT 100) AS x),
        'architecture', jsonb_build_object(
            'pages', (SELECT count(*) FROM architecture),
            'hubs', (SELECT count(*) FROM architecture AS a WHERE a.children IS NOT NULL)),
        'architectureProblemCount', (SELECT count(*) FROM architecture AS a WHERE a.problem IS NOT NULL),
        'architectureProblems', (SELECT coalesce(jsonb_agg(jsonb_build_object('path', x.path, 'problem', x.problem) ORDER BY x.path COLLATE "C"), '[]')
                                 FROM (SELECT a.path, a.problem FROM architecture AS a WHERE a.problem IS NOT NULL
                                       ORDER BY a.path COLLATE "C" LIMIT 100) AS x));
$$;
