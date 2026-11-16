-- wiki_v1 (#307, #308): a local, append-only, content-addressed copy of a GitHub wiki. The wiki stays canonical; this
-- database is a working copy that can always be rebuilt from it, and renders it back byte for byte.

CREATE SCHEMA wiki_v1;

-- File contents, stored once per distinct byte sequence and keyed by their SHA-256, which the database itself checks.
CREATE TABLE wiki_v1.blobs (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content bytea NOT NULL,
    byte_count integer NOT NULL CHECK (byte_count = octet_length(content)),
    CHECK (encode(sha256(content), 'hex') = sha256)
);

-- One complete state of the wiki. The digest covers every path and file hash, so one state is stored once.
CREATE TABLE wiki_v1.snapshots (
    snapshot_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    digest text NOT NULL UNIQUE CHECK (digest ~ '^[0-9a-f]{64}$'),
    file_count integer NOT NULL CHECK (file_count > 0),
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Paths are relative, '/'-separated, without empty, '.' or '..' segments, a '.git' segment in any case, control
-- characters or backslashes, so every path renders inside the target and never into a clone's git directory.
CREATE TABLE wiki_v1.snapshot_files (
    snapshot_uuid uuid NOT NULL REFERENCES wiki_v1.snapshots,
    path text NOT NULL CHECK (
        octet_length(path) BETWEEN 1 AND 512
        AND path !~ '(^|/)\.{1,2}(/|$)'
        AND path !~ '(^/|//|/$)'
        AND lower(path) !~ '(^|/)\.git(/|$)'
        AND path !~ '[[:cntrl:]\\]'),
    sha256 text NOT NULL REFERENCES wiki_v1.blobs,
    PRIMARY KEY (snapshot_uuid, path)
);

-- Every wiki commit seen, in order, and the state it holds; a later commit may return to an earlier state.
CREATE TABLE wiki_v1.snapshot_commits (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wiki_commit text NOT NULL UNIQUE CHECK (wiki_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    snapshot_uuid uuid NOT NULL REFERENCES wiki_v1.snapshots,
    seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Append-only: nothing is ever changed or removed (history is the audit trail).
CREATE FUNCTION wiki_v1.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'wiki_v1.% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER blobs_append_only BEFORE UPDATE OR DELETE ON wiki_v1.blobs FOR EACH ROW EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshots_append_only BEFORE UPDATE OR DELETE ON wiki_v1.snapshots FOR EACH ROW EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshot_files_append_only BEFORE UPDATE OR DELETE ON wiki_v1.snapshot_files FOR EACH ROW EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshot_commits_append_only BEFORE UPDATE OR DELETE ON wiki_v1.snapshot_commits FOR EACH ROW EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER blobs_no_truncate BEFORE TRUNCATE ON wiki_v1.blobs EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshots_no_truncate BEFORE TRUNCATE ON wiki_v1.snapshots EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshot_files_no_truncate BEFORE TRUNCATE ON wiki_v1.snapshot_files EXECUTE FUNCTION wiki_v1.refuse_change();
CREATE TRIGGER snapshot_commits_no_truncate BEFORE TRUNCATE ON wiki_v1.snapshot_commits EXECUTE FUNCTION wiki_v1.refuse_change();

-- The digest of a state: SHA-256 over "path TAB sha256 LF" for every file, in byte order of the path.
CREATE FUNCTION wiki_v1.state_digest(p_paths text[], p_hashes text[]) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(string_agg(f.path || E'\t' || f.hash || E'\n', '' ORDER BY f.path COLLATE "C"), 'UTF8')), 'hex')
    FROM unnest(p_paths, p_hashes) AS f(path, hash);
$$;

-- Records the state of one wiki commit. Contents are stored once per distinct byte sequence and a state once per digest;
-- recording a commit again changes nothing. Serialized, so concurrent seeds cannot interleave.
CREATE FUNCTION wiki_v1.ingest_snapshot(p_commit text, p_paths text[], p_contents bytea[])
RETURNS TABLE (snapshot_uuid uuid, digest text, reused boolean, new_blobs integer)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_hashes text[];
    v_digest text;
    v_snapshot uuid;
    v_known uuid;
    v_new integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('wiki_v1.ingest_snapshot'));
    IF p_paths IS NULL OR p_contents IS NULL OR cardinality(p_paths) = 0 OR cardinality(p_paths) <> cardinality(p_contents) THEN
        RAISE EXCEPTION 'a snapshot needs one content per path and at least one file' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF (SELECT count(DISTINCT p) FROM unnest(p_paths) AS p) <> cardinality(p_paths) THEN
        RAISE EXCEPTION 'a snapshot cannot repeat a path' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- A file and a directory cannot share a name, or the state could not be written as files.
    IF EXISTS (
        SELECT 1
        FROM unnest(p_paths) AS f(path)
        CROSS JOIN LATERAL generate_series(1, cardinality(string_to_array(f.path, '/')) - 1) AS i
        JOIN unnest(p_paths) AS d(path) ON d.path = array_to_string((string_to_array(f.path, '/'))[1:i], '/')) THEN
        RAISE EXCEPTION 'a snapshot cannot hold a file where it holds a directory' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT c.snapshot_uuid INTO v_known FROM wiki_v1.snapshot_commits AS c WHERE c.wiki_commit = p_commit;
    v_hashes := ARRAY(SELECT encode(sha256(c), 'hex') FROM unnest(p_contents) WITH ORDINALITY AS t(c, i) ORDER BY i);
    v_digest := wiki_v1.state_digest(p_paths, v_hashes);
    IF v_known IS NOT NULL THEN
        IF (SELECT s.digest FROM wiki_v1.snapshots AS s WHERE s.snapshot_uuid = v_known) <> v_digest THEN
            RAISE EXCEPTION 'commit % was recorded with different contents', p_commit USING ERRCODE = 'unique_violation';
        END IF;

        RETURN QUERY SELECT v_known, v_digest, true, 0;
        RETURN;
    END IF;

    WITH inserted AS (
        INSERT INTO wiki_v1.blobs (sha256, content, byte_count)
        SELECT DISTINCT ON (h) h, c, octet_length(c) FROM unnest(v_hashes, p_contents) AS t(h, c)
        ON CONFLICT (sha256) DO NOTHING
        RETURNING 1)
    SELECT count(*) INTO v_new FROM inserted;

    SELECT s.snapshot_uuid INTO v_snapshot FROM wiki_v1.snapshots AS s WHERE s.digest = v_digest;
    IF v_snapshot IS NULL THEN
        INSERT INTO wiki_v1.snapshots (digest, file_count, byte_count)
        VALUES (v_digest, cardinality(p_paths), (SELECT coalesce(sum(octet_length(c)), 0) FROM unnest(p_contents) AS c))
        RETURNING snapshots.snapshot_uuid INTO v_snapshot;
        INSERT INTO wiki_v1.snapshot_files (snapshot_uuid, path, sha256)
        SELECT v_snapshot, p, h FROM unnest(p_paths, v_hashes) AS t(p, h);
        INSERT INTO wiki_v1.snapshot_commits (wiki_commit, snapshot_uuid) VALUES (p_commit, v_snapshot);
        RETURN QUERY SELECT v_snapshot, v_digest, false, v_new;
        RETURN;
    END IF;

    INSERT INTO wiki_v1.snapshot_commits (wiki_commit, snapshot_uuid) VALUES (p_commit, v_snapshot);
    RETURN QUERY SELECT v_snapshot, v_digest, true, v_new;
END;
$$;

-- The state of the most recently recorded commit, or no row when nothing was recorded yet.
CREATE FUNCTION wiki_v1.latest_snapshot()
RETURNS TABLE (snapshot_uuid uuid, digest text, wiki_commit text, file_count integer, byte_count bigint)
LANGUAGE sql STABLE AS $$
    SELECT s.snapshot_uuid, s.digest, c.wiki_commit, s.file_count, s.byte_count
    FROM wiki_v1.snapshot_commits AS c JOIN wiki_v1.snapshots AS s USING (snapshot_uuid)
    ORDER BY c.sequence DESC LIMIT 1;
$$;

-- The state recorded for one commit, or no row.
CREATE FUNCTION wiki_v1.commit_snapshot(p_commit text) RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT c.snapshot_uuid FROM wiki_v1.snapshot_commits AS c WHERE c.wiki_commit = p_commit;
$$;

-- Every file of a state with its bytes, in byte order of the path.
CREATE FUNCTION wiki_v1.export_snapshot(p_snapshot uuid)
RETURNS TABLE (path text, content bytea, sha256 text)
LANGUAGE sql STABLE AS $$
    SELECT f.path, b.content, f.sha256
    FROM wiki_v1.snapshot_files AS f JOIN wiki_v1.blobs AS b USING (sha256)
    WHERE f.snapshot_uuid = p_snapshot
    ORDER BY f.path COLLATE "C";
$$;

-- What changed between two states: added, modified or deleted paths, in byte order of the path.
CREATE FUNCTION wiki_v1.changed_paths(p_from uuid, p_to uuid)
RETURNS TABLE (path text, change text)
LANGUAGE sql STABLE AS $$
    SELECT coalesce(t.path, f.path),
           CASE WHEN f.path IS NULL THEN 'added' WHEN t.path IS NULL THEN 'deleted' ELSE 'modified' END
    FROM (SELECT x.path, x.sha256 FROM wiki_v1.snapshot_files AS x WHERE x.snapshot_uuid = p_from) AS f
    FULL JOIN (SELECT y.path, y.sha256 FROM wiki_v1.snapshot_files AS y WHERE y.snapshot_uuid = p_to) AS t USING (path)
    WHERE f.sha256 IS DISTINCT FROM t.sha256
    ORDER BY coalesce(t.path, f.path) COLLATE "C";
$$;
