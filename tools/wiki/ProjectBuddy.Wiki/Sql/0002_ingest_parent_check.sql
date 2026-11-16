-- ingest_parent_check (#309): ingest_snapshot unchanged except for its check that no file shares its name with a
-- directory. 0001 joined the paths with their parent directories, which PostgreSQL planned as a nested loop: seeding
-- 10,001 files took 25 seconds. An applied migration is never edited, so this one replaces the function.

-- Records the state of one wiki commit. Contents are stored once per distinct byte sequence and a state once per digest;
-- recording a commit again changes nothing. Serialized, so concurrent seeds cannot interleave.
CREATE OR REPLACE FUNCTION wiki_v1.ingest_snapshot(p_commit text, p_paths text[], p_contents bytea[])
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
    -- INTERSECT is a set operation (hashed or sorted); a join between two array parameters was planned as a nested
    -- loop, quadratic in the number of paths.
    IF EXISTS (
        SELECT f.path FROM unnest(p_paths) AS f(path)
        INTERSECT
        SELECT array_to_string((string_to_array(f.path, '/'))[1:i], '/')
        FROM unnest(p_paths) AS f(path)
        CROSS JOIN LATERAL generate_series(1, cardinality(string_to_array(f.path, '/')) - 1) AS i) THEN
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

