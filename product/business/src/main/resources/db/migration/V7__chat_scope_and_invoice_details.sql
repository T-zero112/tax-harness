ALTER TABLE product_invoice ADD COLUMN amount_cents bigint CHECK (amount_cents >= 0);
ALTER TABLE product_invoice ADD COLUMN seller text;
ALTER TABLE product_invoice ADD COLUMN item_name text;
-- These are explicitly synthetic product fixtures. Existing imported amounts stay unknown.
UPDATE product_invoice SET amount_cents = CASE WHEN tenant_id = 'beta' THEN 870100
    WHEN enterprise_id = 'C002' THEN 259900 ELSE 123456 END,
    seller = '合成销售方', item_name = '合成办公用品'
WHERE invoice_id = 'F001';

CREATE TABLE agent_chat_scope (
    scope_id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES product_account,
    tenant_id text NOT NULL,
    browser_session text NOT NULL,
    enterprise_id text NOT NULL,
    accounting_period date NOT NULL,
    generation bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise
);
ALTER TABLE invoice_proposal ADD COLUMN chat_scope_id uuid REFERENCES agent_chat_scope;
ALTER TABLE invoice_proposal ADD COLUMN chat_generation bigint;
ALTER TABLE invoice_proposal ADD CONSTRAINT proposal_chat_binding CHECK
    ((chat_scope_id IS NULL AND chat_generation IS NULL) OR
     (chat_scope_id IS NOT NULL AND chat_generation IS NOT NULL));
