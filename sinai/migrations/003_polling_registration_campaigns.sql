-- Rebuild clients with nullable CRM identity, keeping every existing ID and FK.
CREATE TABLE clients_v3 (
 id INTEGER PRIMARY KEY, contact_id INTEGER UNIQUE, name TEXT NOT NULL,
 segment TEXT NOT NULL CHECK(segment IN ('active','completed')), subscribed_at TEXT NOT NULL,
 preferred_platform TEXT NOT NULL, opted_out INTEGER NOT NULL DEFAULT 0,
 segment_source TEXT NOT NULL, last_crm_check TEXT, created_at TEXT NOT NULL,
 identity_verified INTEGER NOT NULL DEFAULT 0, merged_into INTEGER REFERENCES clients(id),
 local_subscription_id INTEGER UNIQUE REFERENCES subscriptions(id)
);
INSERT INTO clients_v3(id,contact_id,name,segment,subscribed_at,preferred_platform,opted_out,segment_source,last_crm_check,created_at,identity_verified)
 SELECT id,contact_id,name,segment,subscribed_at,preferred_platform,opted_out,segment_source,last_crm_check,created_at,
 EXISTS(SELECT 1 FROM bindings b JOIN subscriptions s ON s.id=b.subscription_id WHERE s.client_id=clients.id AND b.method IN ('phone','deeplink_phone')) FROM clients;
DROP TABLE clients;
ALTER TABLE clients_v3 RENAME TO clients;
ALTER TABLE subscriptions ADD COLUMN claimed_lead_id INTEGER;
ALTER TABLE subscriptions ADD COLUMN crm_method TEXT;
ALTER TABLE subscriptions ADD COLUMN identity_verified INTEGER NOT NULL DEFAULT 0;
UPDATE subscriptions SET identity_verified=1 WHERE id IN (SELECT subscription_id FROM bindings WHERE method IN ('phone','deeplink_phone'));
CREATE TABLE lead_claims (
 lead_id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
 client_id INTEGER REFERENCES clients(id), method TEXT NOT NULL,
 pipeline_id INTEGER NOT NULL, status_id INTEGER NOT NULL, claimed_at TEXT NOT NULL,
 checked_at TEXT NOT NULL, UNIQUE(subscription_id)
);
INSERT OR IGNORE INTO lead_claims SELECT b.lead_id,b.subscription_id,s.client_id,b.method,
 COALESCE(l.pipeline_id,0),COALESCE(l.status_id,0),b.created_at,COALESCE(l.checked_at,b.created_at)
 FROM bindings b JOIN subscriptions s ON s.id=b.subscription_id LEFT JOIN leads l ON l.id=b.lead_id
 ORDER BY b.created_at,b.subscription_id,b.lead_id;
UPDATE subscriptions SET claimed_lead_id=(SELECT lead_id FROM lead_claims WHERE subscription_id=subscriptions.id),
 crm_method=(SELECT method FROM lead_claims WHERE subscription_id=subscriptions.id);
UPDATE subscriptions SET claimed_lead_id=(SELECT lead_id FROM bindings WHERE subscription_id=subscriptions.id ORDER BY created_at LIMIT 1)
 WHERE claimed_lead_id IS NULL AND client_id IS NOT NULL;
INSERT INTO clients(name,segment,subscribed_at,preferred_platform,opted_out,segment_source,created_at,local_subscription_id)
 SELECT COALESCE(name,''),COALESCE(selected_segment,'active'),subscribed_at,platform,stopped,'selection',created_at,id
 FROM subscriptions WHERE client_id IS NULL AND subscribed_at IS NOT NULL;
UPDATE subscriptions SET client_id=(SELECT id FROM clients WHERE local_subscription_id=subscriptions.id),state='registered'
 WHERE client_id IS NULL AND subscribed_at IS NOT NULL;
CREATE TABLE rate_windows(key TEXT PRIMARY KEY, started_at TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE admin_sessions(platform TEXT NOT NULL,user_id INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(platform,user_id));
CREATE TABLE campaigns (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL, admin_id INTEGER NOT NULL,
 audience TEXT NOT NULL CHECK(audience IN ('active','completed','all')), state TEXT NOT NULL DEFAULT 'draft',
 revision INTEGER NOT NULL DEFAULT 1, preview_revision INTEGER, preview_test_mode INTEGER,
 preview_recipients TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
);
CREATE UNIQUE INDEX one_draft ON campaigns(platform,admin_id) WHERE state='draft';
CREATE TABLE campaign_items (
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id), source_key TEXT NOT NULL,
 content TEXT NOT NULL, UNIQUE(campaign_id,source_key)
);
CREATE TABLE campaign_variants (
 item_id INTEGER NOT NULL REFERENCES campaign_items(id), platform TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(item_id,platform)
);
CREATE TABLE campaign_deliveries (
 id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL REFERENCES campaigns(id), client_id INTEGER NOT NULL REFERENCES clients(id),
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), state TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL, sent_at TEXT, error TEXT, UNIQUE(campaign_id,client_id)
);
CREATE TABLE campaign_parts (
 delivery_id INTEGER NOT NULL REFERENCES campaign_deliveries(id), item_id INTEGER NOT NULL REFERENCES campaign_items(id),
 state TEXT NOT NULL DEFAULT 'pending', external_id TEXT, error TEXT, PRIMARY KEY(delivery_id,item_id)
);
-- Retire old matching work, preserve historical reviews and links for audit.
UPDATE reviews SET status='retired' WHERE status='pending';
UPDATE jobs SET state='retired' WHERE kind='sync' AND json_extract(payload,'$.by_name')=1 AND state IN ('pending','running');
DELETE FROM contact_index;
DELETE FROM settings WHERE key='contact_index_at';
