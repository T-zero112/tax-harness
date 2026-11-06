BEGIN IMMEDIATE;
CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE enterprises (enterprise_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE permissions (
    user_id TEXT NOT NULL REFERENCES users,
    enterprise_id TEXT NOT NULL REFERENCES enterprises,
    can_write INTEGER NOT NULL CHECK (can_write IN (0,1)),
    PRIMARY KEY (user_id, enterprise_id)
);
CREATE TABLE invoices (
    enterprise_id TEXT NOT NULL REFERENCES enterprises,
    invoice_id TEXT NOT NULL,
    invoice_date TEXT NOT NULL CHECK (length(invoice_date)=10),
    seller TEXT NOT NULL,
    item_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents)='integer' AND amount_cents>=0),
    purpose TEXT,
    note TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
    PRIMARY KEY (enterprise_id, invoice_id)
);
CREATE TABLE bank_transactions (
    enterprise_id TEXT NOT NULL REFERENCES enterprises,
    transaction_id TEXT NOT NULL,
    transaction_date TEXT NOT NULL CHECK (length(transaction_date)=10),
    direction TEXT NOT NULL CHECK (direction IN ('income','expense')),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents)='integer' AND amount_cents>=0),
    counterparty TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (enterprise_id, transaction_id)
);
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    enterprise_id TEXT NOT NULL,
    month TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' AND substr(month,6,2) BETWEEN '01' AND '12'),
    status TEXT NOT NULL CHECK (status IN ('running','waiting_user','completed','blocked','cancelled')),
    object_id TEXT,
    state_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
    FOREIGN KEY (user_id, enterprise_id) REFERENCES permissions,
    UNIQUE (task_id, user_id, enterprise_id, month)
);
CREATE UNIQUE INDEX one_unfinished_task ON tasks(user_id, enterprise_id)
WHERE status IN ('running','waiting_user','blocked');
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks,
    session_id TEXT NOT NULL,
    request_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','tool')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE requests (
    user_id TEXT NOT NULL REFERENCES users,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    task_id TEXT REFERENCES tasks,
    status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
    response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
    PRIMARY KEY(user_id,request_id)
);
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users,
    enterprise_id TEXT REFERENCES enterprises,
    content TEXT NOT NULL,
    source_message_id TEXT NOT NULL REFERENCES messages,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);
CREATE TABLE memory_invalidations (
    memory_id TEXT NOT NULL REFERENCES memories,
    message_id TEXT NOT NULL REFERENCES messages,
    revision INTEGER NOT NULL CHECK (revision>=1),
    PRIMARY KEY(memory_id,message_id,revision)
);
CREATE TABLE rules (
    doc_id TEXT NOT NULL,
    version TEXT NOT NULL,
    enterprise_id TEXT NOT NULL REFERENCES enterprises,
    entry TEXT NOT NULL,
    name TEXT NOT NULL,
    effective_from TEXT NOT NULL CHECK (length(effective_from)=10),
    content TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision>=1),
    PRIMARY KEY(doc_id,version,enterprise_id,entry)
);
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    enterprise_id TEXT NOT NULL,
    month TEXT NOT NULL,
    object_id TEXT NOT NULL,
    patch_json TEXT NOT NULL CHECK (json_valid(patch_json)),
    status TEXT NOT NULL CHECK (status IN ('prepared','dispatched','confirmed','failed','unknown')),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    verification_json TEXT CHECK (verification_json IS NULL OR json_valid(verification_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_id,user_id,enterprise_id,month) REFERENCES tasks(task_id,user_id,enterprise_id,month),
    FOREIGN KEY(user_id,request_id) REFERENCES requests(user_id,request_id),
    FOREIGN KEY(enterprise_id,object_id) REFERENCES invoices(enterprise_id,invoice_id)
);
CREATE UNIQUE INDEX one_unresolved_write ON operations(enterprise_id,object_id)
WHERE status IN ('prepared','dispatched','unknown');
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER fixed_task_scope BEFORE UPDATE OF user_id,enterprise_id,month ON tasks
WHEN NEW.user_id!=OLD.user_id OR NEW.enterprise_id!=OLD.enterprise_id OR NEW.month!=OLD.month
BEGIN SELECT RAISE(ABORT,'task scope is immutable'); END;
CREATE TRIGGER pending_write_before_task_close BEFORE UPDATE OF status ON tasks
WHEN NEW.status IN ('completed','cancelled') AND EXISTS (
    SELECT 1 FROM operations WHERE task_id=OLD.task_id AND status IN ('prepared','dispatched','unknown')
)
BEGIN SELECT RAISE(ABORT,'task has unresolved write'); END;
CREATE TRIGGER immutable_invoice_fields BEFORE UPDATE OF enterprise_id,invoice_id,invoice_date,seller,item_name,amount_cents ON invoices
BEGIN SELECT RAISE(ABORT,'invoice business fields are read-only'); END;
CREATE TRIGGER immutable_bank_update BEFORE UPDATE ON bank_transactions
BEGIN SELECT RAISE(ABORT,'bank records are read-only'); END;
CREATE TRIGGER immutable_bank_delete BEFORE DELETE ON bank_transactions
BEGIN SELECT RAISE(ABORT,'bank records are read-only'); END;
INSERT INTO users VALUES ('U001','测试用户一'),('U002','测试用户二');
INSERT INTO enterprises VALUES ('C001','甲企业'),('C002','乙企业');
INSERT INTO permissions VALUES ('U001','C001',1),('U001','C002',1),('U002','C001',0);
PRAGMA user_version=1;
COMMIT;
