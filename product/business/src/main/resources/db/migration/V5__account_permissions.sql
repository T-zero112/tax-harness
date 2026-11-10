CREATE TABLE product_account (
    account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE tenant_membership (
    tenant_id text NOT NULL REFERENCES tenant,
    account_id uuid NOT NULL REFERENCES product_account,
    PRIMARY KEY (tenant_id, account_id)
);
CREATE TABLE enterprise_grant (
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    account_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    PRIMARY KEY (tenant_id, enterprise_id, account_id),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise,
    FOREIGN KEY (tenant_id, account_id) REFERENCES tenant_membership
);
