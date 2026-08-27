-- Privacy-preserving MCP adoption monitoring. One row is a daily aggregate
-- for one normalized client/tool/pseudonymous actor combination; raw request
-- metadata, tool arguments, prompts, IP addresses, and identifiers never land
-- in this table.
CREATE TABLE mcp_usage_daily (
    usage_date        date NOT NULL,
    client_family     text NOT NULL CHECK (
        client_family IN ('chatgpt', 'claude', 'codex', 'other', 'unknown')
    ),
    tool_name         text NOT NULL CHECK (
        tool_name ~ '^[a-z_][a-z0-9_]{0,63}$'
    ),
    subject_digest    text NOT NULL DEFAULT '' CHECK (
        subject_digest = '' OR subject_digest ~ '^[0-9a-f]{64}$'
    ),
    session_digest    text NOT NULL DEFAULT '' CHECK (
        session_digest = '' OR session_digest ~ '^[0-9a-f]{64}$'
    ),
    call_count        bigint NOT NULL DEFAULT 1 CHECK (call_count > 0),
    failure_count     bigint NOT NULL DEFAULT 0 CHECK (
        failure_count >= 0 AND failure_count <= call_count
    ),
    PRIMARY KEY (
        usage_date, client_family, tool_name, subject_digest, session_digest
    )
);
