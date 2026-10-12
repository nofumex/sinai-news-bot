CREATE TABLE clients (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL,
 segment TEXT NOT NULL CHECK(segment IN ('active','completed')), subscribed_at TEXT NOT NULL,
 preferred_platform TEXT NOT NULL, opted_out INTEGER NOT NULL DEFAULT 0,
 segment_source TEXT NOT NULL, last_crm_check TEXT, created_at TEXT NOT NULL
);
CREATE TABLE subscriptions (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL CHECK(platform IN ('telegram','max')),
 user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, client_id INTEGER REFERENCES clients(id),
 phone TEXT, name TEXT, selected_segment TEXT, state TEXT NOT NULL DEFAULT 'new',
 subscribed_at TEXT, created_at TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0,
 blocked INTEGER NOT NULL DEFAULT 0, deep_token_hash TEXT, UNIQUE(platform,user_id)
);
CREATE TABLE bindings (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), contact_id INTEGER NOT NULL,
 lead_id INTEGER NOT NULL, method TEXT NOT NULL, verified_by TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(subscription_id,contact_id,lead_id)
);
CREATE TABLE leads (
 id INTEGER PRIMARY KEY, pipeline_id INTEGER NOT NULL, status_id INTEGER NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE reviews (
 id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
 reason TEXT NOT NULL, candidates TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT
);
CREATE UNIQUE INDEX pending_review ON reviews(subscription_id) WHERE status='pending';
CREATE TABLE links (
 token_hash TEXT PRIMARY KEY, platform TEXT NOT NULL, lead_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
 expires_at TEXT NOT NULL, issued_by TEXT NOT NULL, claimed_by INTEGER, consumed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE events (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subscription_id INTEGER REFERENCES subscriptions(id),
 client_id INTEGER REFERENCES clients(id), detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX events_time ON events(created_at,kind);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at TEXT NOT NULL, created_at TEXT NOT NULL, last_error TEXT
);
CREATE INDEX runnable_jobs ON jobs(state,available_at,id);
CREATE TABLE deliveries (
 id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), cycle INTEGER NOT NULL,
 segment TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 due_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, external_id TEXT, error TEXT,
 UNIQUE(client_id,cycle)
);
CREATE TABLE crm_notes (
 key TEXT PRIMARY KEY, lead_id INTEGER NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL, external_id INTEGER
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE contact_index (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE errors (
 id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id), kind TEXT NOT NULL, message TEXT NOT NULL,
 created_at TEXT NOT NULL, resolved_at TEXT
);
