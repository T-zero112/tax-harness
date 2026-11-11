CREATE TABLE agent_task (
    task_id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES product_account,
    tenant_id text NOT NULL,
    enterprise_id text NOT NULL,
    accounting_period date NOT NULL,
    status text NOT NULL CHECK (status IN ('running','waiting_user','blocked','succeeded','cancelled')),
    revision bigint NOT NULL DEFAULT 1,
    object_id text,
    question text,
    proposal_id uuid REFERENCES invoice_proposal,
    bound_scope_id uuid REFERENCES agent_chat_scope,
    active_turn_id uuid,
    lease_until timestamptz,
    context_since timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, enterprise_id) REFERENCES enterprise,
    CHECK (extract(day from accounting_period)=1)
);
CREATE UNIQUE INDEX agent_task_one_active ON agent_task(tenant_id,account_id,enterprise_id)
    WHERE status IN ('running','waiting_user','blocked');
ALTER TABLE invoice_proposal ADD COLUMN task_id uuid REFERENCES agent_task;
ALTER TABLE invoice_proposal ADD COLUMN before_record jsonb;

CREATE TABLE agent_message (
    message_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES agent_task,
    turn_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('user','assistant')),
    content text NOT NULL,
    scope_generation bigint NOT NULL,
    accounting_period date NOT NULL,
    task_context boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (turn_id,role)
);
CREATE INDEX agent_message_task ON agent_message(task_id,created_at,message_id);

CREATE TABLE agent_trace (
    turn_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES agent_task,
    status text NOT NULL CHECK (status IN ('running','succeeded','needs_input','pending_confirmation','incomplete')),
    steps integer NOT NULL DEFAULT 0,
    summary jsonb NOT NULL DEFAULT '{}',
    model_name text NOT NULL DEFAULT '',
    duration_ms bigint NOT NULL DEFAULT 0,
    context_bytes integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE agent_memory (
    memory_id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES product_account,
    tenant_id text NOT NULL REFERENCES tenant,
    enterprise_id text,
    content text NOT NULL,
    source_message_id uuid NOT NULL REFERENCES agent_message,
    revision bigint NOT NULL DEFAULT 1,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,enterprise_id) REFERENCES enterprise
);
CREATE TABLE agent_memory_invalidation (
    memory_id uuid NOT NULL REFERENCES agent_memory,
    message_id uuid NOT NULL REFERENCES agent_message,
    revision bigint NOT NULL,
    PRIMARY KEY (memory_id,message_id,revision)
);
CREATE TABLE agent_memory_audit (
    event_id uuid PRIMARY KEY,
    memory_id uuid NOT NULL REFERENCES agent_memory,
    actor_id uuid NOT NULL REFERENCES product_account,
    action text NOT NULL CHECK (action IN ('created','corrected','deleted')),
    revision bigint NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_operation (
    operation_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES agent_task,
    turn_id uuid,
    kind text NOT NULL CHECK (kind IN ('proposal','confirmation')),
    state text NOT NULL CHECK (state IN ('pending','confirmed','failed','unknown')),
    object_id text NOT NULL,
    proposal_id uuid REFERENCES invoice_proposal,
    idempotency_key uuid,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX agent_operation_proposal_turn ON agent_operation(task_id,turn_id) WHERE kind='proposal';
CREATE UNIQUE INDEX agent_operation_confirmation_key ON agent_operation(task_id,idempotency_key)
    WHERE kind='confirmation';
ALTER TABLE invoice_audit ADD COLUMN operation_id uuid REFERENCES agent_operation;
