-- A test double of the RSR registry's ingest contract, rsr/registry-snapshot@1 (RSR v0.4.2, db/registry-v1 0001 and
-- 0002): only the tables and ingest_snapshot rules the wiki export relies on, with RSR's error messages. RSR is
-- private, so public CI tests against this; the same tests pass against RSR's own migrations when
-- PBW_TEST_REGISTRY_MIGRATIONS names them, and the live export to registry_local proves the real thing.

CREATE SCHEMA registry_v1;

CREATE TABLE registry_v1.sources (
    source_uuid uuid PRIMARY KEY,
    constellation_uuid uuid NOT NULL,
    canonical_remote text NOT NULL,
    UNIQUE (source_uuid, constellation_uuid)
);

CREATE TABLE registry_v1.source_authority_manifests (
    source_uuid uuid NOT NULL,
    constellation_uuid uuid NOT NULL,
    manifest_digest text NOT NULL,
    source_role text NOT NULL,
    allowed_refs jsonb NOT NULL,
    allowed_scopes jsonb NOT NULL,
    accepted_by text NOT NULL,
    PRIMARY KEY (source_uuid, manifest_digest),
    FOREIGN KEY (source_uuid, constellation_uuid) REFERENCES registry_v1.sources (source_uuid, constellation_uuid)
);

CREATE TABLE registry_v1.importer_authorizations (
    principal name NOT NULL,
    constellation_uuid uuid NOT NULL,
    granted_by text NOT NULL,
    revoked_at timestamptz
);

CREATE TABLE registry_v1.snapshots (
    snapshot_uuid uuid PRIMARY KEY,
    source_uuid uuid NOT NULL REFERENCES registry_v1.sources,
    scope text NOT NULL,
    source_ref text NOT NULL,
    source_revision text NOT NULL,
    tree_digest text NOT NULL,
    parser_version text NOT NULL,
    authority_manifest_digest text NOT NULL,
    payload_digest text NOT NULL,
    UNIQUE (source_uuid, scope, tree_digest, parser_version, authority_manifest_digest)
);

CREATE TABLE registry_v1.requirement_nodes (
    snapshot_uuid uuid NOT NULL REFERENCES registry_v1.snapshots,
    local_id text NOT NULL,
    node_kind text NOT NULL,
    lifecycle_status text NOT NULL,
    content_digest text NOT NULL,
    content_text text NOT NULL,
    location text NOT NULL,
    PRIMARY KEY (snapshot_uuid, local_id)
);

CREATE TABLE registry_v1.edges (
    snapshot_uuid uuid NOT NULL,
    from_local_id text NOT NULL,
    edge_type text NOT NULL,
    to_snapshot_uuid uuid NOT NULL,
    to_local_id text NOT NULL,
    metadata jsonb NOT NULL,
    PRIMARY KEY (snapshot_uuid, from_local_id, edge_type, to_snapshot_uuid, to_local_id),
    FOREIGN KEY (snapshot_uuid, from_local_id) REFERENCES registry_v1.requirement_nodes (snapshot_uuid, local_id),
    FOREIGN KEY (to_snapshot_uuid, to_local_id) REFERENCES registry_v1.requirement_nodes (snapshot_uuid, local_id)
);

CREATE PROCEDURE registry_v1.ingest_snapshot(IN p_envelope jsonb, OUT p_snapshot_uuid uuid, OUT p_payload_digest text, OUT p_reused boolean)
LANGUAGE plpgsql AS $$
DECLARE
    v_source uuid;
    v_constellation uuid;
    v_existing text;
    v_target uuid;
    v_node jsonb;
    v_edge jsonb;
BEGIN
    p_reused := false;
    IF jsonb_typeof(p_envelope) IS DISTINCT FROM 'object'
        OR (SELECT count(*) FROM jsonb_object_keys(p_envelope)) <> 11
        OR NOT (p_envelope ?& ARRAY['schema', 'source_uuid', 'constellation_uuid', 'manifest_digest', 'scope', 'source_ref',
                                    'source_revision', 'tree_digest', 'parser_version', 'nodes', 'edges'])
        OR p_envelope ->> 'schema' <> 'rsr/registry-snapshot@1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_snapshot_envelope';
    END IF;

    IF jsonb_array_length(p_envelope -> 'nodes') > 10000 OR jsonb_array_length(p_envelope -> 'edges') > 50000
        OR octet_length(p_envelope::text) > 16777216 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'snapshot_graph_limit_exceeded';
    END IF;

    v_source := (p_envelope ->> 'source_uuid')::uuid;
    v_constellation := (p_envelope ->> 'constellation_uuid')::uuid;
    p_payload_digest := encode(sha256(convert_to(p_envelope::text, 'UTF8')), 'hex');
    IF NOT EXISTS (SELECT 1 FROM registry_v1.importer_authorizations AS a
                   WHERE a.principal = session_user AND a.constellation_uuid = v_constellation AND a.revoked_at IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'constellation_import_not_authorized';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM registry_v1.source_authority_manifests AS m JOIN registry_v1.sources AS s USING (source_uuid, constellation_uuid)
                   WHERE m.source_uuid = v_source AND m.constellation_uuid = v_constellation AND m.manifest_digest = p_envelope ->> 'manifest_digest') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source_authority_not_admitted';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM registry_v1.source_authority_manifests AS m
                   WHERE m.source_uuid = v_source AND m.manifest_digest = p_envelope ->> 'manifest_digest'
                     AND m.allowed_scopes ? (p_envelope ->> 'scope') AND m.allowed_refs ? (p_envelope ->> 'source_ref')) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'snapshot_outside_admitted_scope_or_ref';
    END IF;

    SELECT s.snapshot_uuid, s.payload_digest INTO p_snapshot_uuid, v_existing
    FROM registry_v1.snapshots AS s
    WHERE s.source_uuid = v_source AND s.scope = p_envelope ->> 'scope' AND s.tree_digest = p_envelope ->> 'tree_digest'
      AND s.parser_version = p_envelope ->> 'parser_version' AND s.authority_manifest_digest = p_envelope ->> 'manifest_digest';
    IF FOUND THEN
        IF v_existing <> p_payload_digest THEN
            RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'snapshot_payload_changed_for_idempotency_key';
        END IF;
        p_reused := true;
        RETURN;
    END IF;

    p_snapshot_uuid := gen_random_uuid();
    INSERT INTO registry_v1.snapshots
    VALUES (p_snapshot_uuid, v_source, p_envelope ->> 'scope', p_envelope ->> 'source_ref', p_envelope ->> 'source_revision',
            p_envelope ->> 'tree_digest', p_envelope ->> 'parser_version', p_envelope ->> 'manifest_digest', p_payload_digest);

    FOR v_node IN SELECT value FROM jsonb_array_elements(p_envelope -> 'nodes') LOOP
        IF (SELECT count(*) FROM jsonb_object_keys(v_node)) <> 6
            OR NOT (v_node ?& ARRAY['id', 'kind', 'status', 'content_digest', 'text', 'location'])
            OR char_length(v_node ->> 'id') NOT BETWEEN 1 AND 128
            OR v_node ->> 'kind' NOT IN ('system', 'software', 'test', 'dependency')
            OR v_node ->> 'status' NOT IN ('active', 'prototype', 'superseded')
            OR char_length(v_node ->> 'text') > 16384 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_snapshot_node';
        END IF;
        IF v_node ->> 'content_digest' <> encode(sha256(convert_to(v_node ->> 'text', 'UTF8')), 'hex') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'snapshot_node_digest_mismatch';
        END IF;
        INSERT INTO registry_v1.requirement_nodes
        VALUES (p_snapshot_uuid, v_node ->> 'id', v_node ->> 'kind', v_node ->> 'status', v_node ->> 'content_digest', v_node ->> 'text', v_node ->> 'location');
    END LOOP;

    FOR v_edge IN SELECT value FROM jsonb_array_elements(p_envelope -> 'edges') LOOP
        IF (SELECT count(*) FROM jsonb_object_keys(v_edge)) <> 5
            OR NOT (v_edge ?& ARRAY['from_id', 'type', 'to_snapshot_uuid', 'to_id', 'metadata'])
            OR jsonb_typeof(v_edge -> 'metadata') <> 'object'
            OR v_edge ->> 'type' NOT IN ('introduces', 'allocates', 'verifies', 'depends-on', 'mirrors', 'supersedes', 'contradicts') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_snapshot_edge';
        END IF;
        v_target := coalesce((v_edge ->> 'to_snapshot_uuid')::uuid, p_snapshot_uuid);
        IF v_target <> p_snapshot_uuid AND NOT EXISTS (
            SELECT 1 FROM registry_v1.snapshots AS t JOIN registry_v1.sources AS s USING (source_uuid)
            WHERE t.snapshot_uuid = v_target AND s.constellation_uuid = v_constellation) THEN
            RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'snapshot_edge_target_not_authorized';
        END IF;
        INSERT INTO registry_v1.edges
        VALUES (p_snapshot_uuid, v_edge ->> 'from_id', v_edge ->> 'type', v_target, v_edge ->> 'to_id', v_edge -> 'metadata');
    END LOOP;
END;
$$;
