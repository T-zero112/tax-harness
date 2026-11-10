CREATE TABLE invoice_import (
    job_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    file_name text NOT NULL,
    file_sha256 text NOT NULL,
    object_key text NOT NULL,
    attempt uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','ready','invalid','imported')),
    preview jsonb,
    imported_count integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    imported_at timestamptz,
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise,
    UNIQUE (tenant_id, enterprise_id, file_sha256)
);
