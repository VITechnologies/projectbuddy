-- Least privilege for the hooks schema (#393): the schema, its tables and its SECURITY DEFINER functions move from the migrating
-- (super)user to a dedicated role `hooks_owner`: NOLOGIN, no superuser, no CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS. A SECURITY
-- DEFINER function runs with its owner's rights, so a hooks function can now reach only the hooks schema (and pg_catalog), never the
-- rest of the database. Every function also gets a fixed search_path that ends in pg_temp, so no temporary object can shadow one.
--
-- Rollback (as a superuser, per database; the hooks data stays):
--   ALTER SCHEMA hooks OWNER TO postgres;  -- then ALTER TABLE/FUNCTION ... OWNER TO postgres for every object, see tools/hooks/README.md
-- and `DELETE FROM public.kpi_schema_migrations WHERE version = 7` so the migration can run again.

DO $$
BEGIN
    -- Roles are cluster-wide and advisory locks are per database, so two databases migrated at once can both try to create it.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hooks_owner') THEN
        BEGIN
            CREATE ROLE hooks_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END;
    END IF;
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION 'migration 0007 changes object owners and must run as a superuser';
    END IF;
END $$;

ALTER SCHEMA hooks OWNER TO hooks_owner;

DO $$
DECLARE
    item record;
BEGIN
    -- Owning a table also moves the sequence of its bigserial column.
    FOR item IN SELECT c.oid::regclass AS name FROM pg_class c WHERE c.relnamespace = 'hooks'::regnamespace AND c.relkind IN ('r', 'p') LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO hooks_owner', item.name);
    END LOOP;
    FOR item IN SELECT p.oid::regprocedure AS name FROM pg_proc p WHERE p.pronamespace = 'hooks'::regnamespace LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO hooks_owner', item.name);
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, hooks, pg_temp', item.name);
    END LOOP;
END $$;

-- hooks_writer keeps exactly the grants of 0006: usage on the schema and execute on the four entry points.
