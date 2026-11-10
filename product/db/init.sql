CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE tenant (
    tenant_id text PRIMARY KEY
);
CREATE TABLE enterprise (
    tenant_id text NOT NULL REFERENCES tenant,
    enterprise_id text NOT NULL,
    name text NOT NULL,
    PRIMARY KEY (tenant_id, enterprise_id)
);
INSERT INTO tenant VALUES ('alpha'), ('beta');
INSERT INTO enterprise VALUES
    ('alpha', 'C001', '甲租户示例企业'),
    ('beta', 'C001', '乙租户示例企业');
