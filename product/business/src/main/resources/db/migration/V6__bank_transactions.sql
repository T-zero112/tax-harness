CREATE TABLE product_bank_transaction (
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    transaction_id text NOT NULL,
    accounting_period date NOT NULL,
    transaction_date date NOT NULL,
    direction text NOT NULL CHECK (direction IN ('income', 'expense')),
    amount_cents bigint NOT NULL CHECK (amount_cents >= 0),
    counterparty text NOT NULL,
    note text,
    PRIMARY KEY (tenant_id, enterprise_id, transaction_id),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise,
    CHECK (accounting_period = date_trunc('month', transaction_date::timestamp)::date)
);
CREATE INDEX product_bank_transaction_period
    ON product_bank_transaction (tenant_id, enterprise_id, accounting_period, transaction_date, transaction_id);

INSERT INTO product_bank_transaction VALUES
    ('alpha', 'C001', 'T001', '2026-09-01', '2026-09-03', 'income', 123456, '合成客户甲', '合成收款'),
    ('alpha', 'C001', 'T002', '2026-08-01', '2026-08-22', 'expense', 19900, '合成供方甲', '合成采购'),
    ('alpha', 'C002', 'T001', '2026-09-01', '2026-09-04', 'expense', 259900, '合成供方乙', '合成付款'),
    ('beta',  'C001', 'T001', '2026-09-01', '2026-09-08', 'income', 870100, '合成客户乙', '合成收款');

CREATE FUNCTION reject_bank_transaction_write() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Bank transactions are read-only' USING ERRCODE = '25006';
END;
$$;

CREATE TRIGGER product_bank_transaction_read_only
    BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON product_bank_transaction
    FOR EACH STATEMENT EXECUTE FUNCTION reject_bank_transaction_write();
