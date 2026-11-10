INSERT INTO enterprise VALUES ('alpha', 'C002', '甲租户第二企业') ON CONFLICT DO NOTHING;
CREATE TABLE product_invoice (
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    invoice_id text NOT NULL,
    purpose text,
    note text,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    PRIMARY KEY (tenant_id, enterprise_id, invoice_id),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise
);
INSERT INTO product_invoice (tenant_id, enterprise_id, invoice_id, purpose, note) VALUES
    ('alpha','C001','F001',NULL,NULL),
    ('alpha','C002','F001','第二企业用途','第二企业备注'),
    ('beta','C001','F001','乙租户用途','乙租户备注');

CREATE TABLE invoice_proposal (
    proposal_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    invoice_id text NOT NULL,
    expected_revision bigint NOT NULL,
    changes jsonb NOT NULL,
    rule_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, enterprise_id, invoice_id) REFERENCES product_invoice,
    UNIQUE (tenant_id, enterprise_id, invoice_id, proposal_id)
);

CREATE TABLE invoice_audit (
    tenant_id text NOT NULL,
    idempotency_key uuid NOT NULL,
    enterprise_id text NOT NULL,
    invoice_id text NOT NULL,
    proposal_id uuid NOT NULL UNIQUE,
    actor text NOT NULL,
    rule_version text NOT NULL,
    before_record jsonb NOT NULL,
    after_record jsonb NOT NULL,
    saved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, enterprise_id, invoice_id, proposal_id)
        REFERENCES invoice_proposal (tenant_id, enterprise_id, invoice_id, proposal_id)
);
