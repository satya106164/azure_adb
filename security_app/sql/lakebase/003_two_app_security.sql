-- =============================================================================
-- Migration 003: support two apps (request app + admin app) on one schema with
-- separate, least-privilege Postgres roles.
--
-- The audit chain trigger reads the previous audit row. Running it with the
-- schema owner's rights lets the request app INSERT audit events without being
-- able to SELECT the audit trail.
-- =============================================================================
SET search_path = cdp_security, public;

ALTER FUNCTION audit_event_chain() SECURITY DEFINER;
ALTER FUNCTION audit_event_chain() SET search_path = cdp_security, pg_catalog, pg_temp;
