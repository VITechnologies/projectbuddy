-- Workstation hooks (#381): the session registry, the decision queue and the handoff record that the user-level Copilot hooks and
-- `projectbuddy-hooks` write. Hooks reach the database only through the SECURITY DEFINER functions below, as role
-- `hooks_writer` (NOLOGIN here; the installer creates a login role that is a member of it). Every hook entry point is idempotent.

CREATE SCHEMA hooks;

CREATE TABLE hooks.sessions (
    session_id text PRIMARY KEY CHECK (session_id ~ '^[A-Za-z0-9._-]{8,80}$'),
    title text,
    workspace text,
    work_ref text CHECK (work_ref IS NULL OR work_ref ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$'),
    parent_session_id text CHECK (parent_session_id IS NULL OR (parent_session_id ~ '^[A-Za-z0-9._-]{8,80}$' AND parent_session_id <> session_id)),
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'waiting', 'done')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX sessions_parent ON hooks.sessions (parent_session_id) WHERE parent_session_id IS NOT NULL;

CREATE TABLE hooks.decisions (
    id bigserial PRIMARY KEY,
    session_id text NOT NULL,
    work_ref text,
    message text NOT NULL,
    message_id text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
    answer text,
    answered_by text CHECK (answered_by IN ('prompt', 'cli')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    answered_at timestamptz,
    CHECK ((status = 'open') = (answered_at IS NULL))
);
-- A retried agentStop must not queue the same question twice.
CREATE UNIQUE INDEX decisions_one_open_per_session ON hooks.decisions (session_id) WHERE status = 'open';
-- A late or repeated look at the same message must not reopen a decision that was already answered.
CREATE UNIQUE INDEX decisions_once_per_message ON hooks.decisions (session_id, message_id) WHERE message_id IS NOT NULL;

CREATE TABLE hooks.handoffs (
    work_ref text PRIMARY KEY CHECK (work_ref ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$'),
    issue_updated_at timestamptz NOT NULL,
    body_sha256 text NOT NULL,
    writer_session text,
    written_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The payload carries either camelCase (sessionStart) or VS Code style snake_case keys.
CREATE FUNCTION hooks.session_of(p jsonb) RETURNS text LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS
$$ SELECT coalesce(p ->> 'sessionId', p ->> 'session_id') $$;

CREATE FUNCTION hooks.event_of(line text) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS
$$ BEGIN RETURN line::jsonb; EXCEPTION WHEN others THEN RETURN NULL; END $$;

CREATE FUNCTION hooks.message_of(line text) RETURNS text LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, hooks AS
$$ SELECT (event_of(line) -> 'data') ->> 'content' $$;

CREATE FUNCTION hooks.on_start(p jsonb) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, hooks AS $$
BEGIN
    INSERT INTO hooks.sessions (session_id, workspace) VALUES (session_of(p), left(p ->> 'cwd', 1000))
    ON CONFLICT (session_id) DO UPDATE SET workspace = coalesce(EXCLUDED.workspace, hooks.sessions.workspace), status = 'running', last_seen_at = clock_timestamp();
END $$;

CREATE FUNCTION hooks.on_prompt(p jsonb) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, hooks AS $$
BEGIN
    INSERT INTO hooks.sessions (session_id, workspace) VALUES (session_of(p), left(p ->> 'cwd', 1000))
    ON CONFLICT (session_id) DO UPDATE SET status = 'running', last_seen_at = clock_timestamp();
    -- The next prompt in the worker's chat is the orchestrator's reply.
    UPDATE hooks.decisions SET status = 'answered', answer = left(p ->> 'prompt', 2000), answered_by = 'prompt', answered_at = clock_timestamp()
    WHERE session_id = session_of(p) AND status = 'open';
END $$;

-- agentStop runs twice. Inline it is called with no `line` and only records that the session is waiting: the transcript is flushed
-- after the hook fires, so the final message is not in it yet. The detached settle step calls it again with the turn's last
-- assistant.message event line, once the transcript holds it. The second call is ignored if the session has moved on since the stop
-- (a newer prompt), and a message that was already turned into a decision never opens another.
CREATE FUNCTION hooks.on_stop(p jsonb, line text) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, hooks AS $$
DECLARE
    sid text := session_of(p);
    msg text := message_of(line);
    msg_id text := (event_of(line) -> 'data') ->> 'messageId';
    new_status text := 'waiting';
    stopped_at timestamptz;
    seen timestamptz;
BEGIN
    IF coalesce(line, '') = '' THEN
        INSERT INTO hooks.sessions (session_id, workspace, status) VALUES (sid, left(p ->> 'cwd', 1000), 'waiting')
        ON CONFLICT (session_id) DO UPDATE SET status = 'waiting', last_seen_at = clock_timestamp();
        RETURN 'waiting';
    END IF;

    IF p ->> 'timestamp' ~ '^[0-9]{10,16}$' THEN
        stopped_at := to_timestamp((p ->> 'timestamp')::numeric / 1000);
        SELECT last_seen_at INTO seen FROM hooks.sessions WHERE session_id = sid;
        IF seen > stopped_at + interval '2 seconds' THEN
            RETURN 'stale';
        END IF;
    END IF;

    IF msg ~ '^\s*[*_#> ]*DONE\s*:' THEN
        new_status := 'done';
    END IF;
    INSERT INTO hooks.sessions (session_id, workspace, status) VALUES (sid, left(p ->> 'cwd', 1000), new_status)
    ON CONFLICT (session_id) DO UPDATE SET status = new_status;
    IF msg ~ '^\s*[*_#> ]*DECISION NEEDED\s*:'
        AND NOT EXISTS (SELECT FROM hooks.decisions WHERE session_id = sid AND message_id = msg_id) THEN
        INSERT INTO hooks.decisions (session_id, work_ref, message, message_id)
        VALUES (sid, (SELECT work_ref FROM hooks.sessions WHERE session_id = sid), left(msg, 8000), msg_id)
        ON CONFLICT (session_id) WHERE status = 'open' DO UPDATE SET message = EXCLUDED.message, message_id = EXCLUDED.message_id;
    END IF;
    RETURN new_status;
END $$;

-- The work item the KPI auto-mark uses for a session; NULL when none was set.
CREATE FUNCTION hooks.work_ref_of(sid text) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, hooks AS
$$ SELECT work_ref FROM hooks.sessions WHERE session_id = sid $$;

DO $$
BEGIN
    -- Roles are cluster-wide and advisory locks are per database, so two databases migrated at once can both try to create it.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hooks_writer') THEN
        BEGIN
            CREATE ROLE hooks_writer NOLOGIN;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO hooks_writer', current_database());
END $$;
REVOKE ALL ON SCHEMA hooks FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA hooks FROM PUBLIC;
GRANT USAGE ON SCHEMA hooks TO hooks_writer;
GRANT EXECUTE ON FUNCTION hooks.on_start(jsonb), hooks.on_prompt(jsonb), hooks.on_stop(jsonb, text), hooks.work_ref_of(text) TO hooks_writer;
