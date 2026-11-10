ALTER TABLE product_invoice ADD COLUMN accounting_period date NOT NULL DEFAULT DATE '2026-09-01'
    CHECK (extract(day from accounting_period) = 1);
ALTER TABLE invoice_proposal ADD COLUMN rule_ref jsonb;
ALTER TABLE invoice_audit ADD COLUMN rule_ref jsonb;

CREATE TABLE invoice_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    code text NOT NULL,
    version text NOT NULL,
    source text NOT NULL,
    clause text NOT NULL,
    content text NOT NULL,
    required_fields jsonb NOT NULL,
    effective_from date NOT NULL,
    effective_until date,
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','published','retired')),
    embedding vector(384),
    CHECK (extract(day from effective_from) = 1),
    CHECK (effective_until IS NULL OR
           (extract(day from effective_until) = 1 AND effective_until > effective_from)),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise,
    UNIQUE (tenant_id, enterprise_id, code, version, clause)
);
CREATE INDEX invoice_rule_scope ON invoice_rule (tenant_id, enterprise_id, state, effective_from);
INSERT INTO invoice_rule (tenant_id, enterprise_id, code, version, source, clause, content,
                          required_fields, effective_from, state)
SELECT tenant_id, enterprise_id, 'product-invoice-completeness', '1',
       name || '发票资料规范', '1.1', '发票须填写用途和备注，以便核验支出的业务事由。',
       '["note","purpose"]', DATE '2026-09-01', 'published'
FROM enterprise;

CREATE TABLE invoice_rule_review (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    invoice_id text NOT NULL,
    invoice_revision bigint NOT NULL,
    accounting_period date NOT NULL,
    reason text NOT NULL CHECK (reason IN ('rule_missing', 'rule_conflict')),
    candidates jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, enterprise_id, invoice_id) REFERENCES product_invoice
);
