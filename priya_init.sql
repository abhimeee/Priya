PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS vendors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  persona TEXT NOT NULL,
  category TEXT NOT NULL,
  upi_id TEXT,
  bank_account TEXT,
  ifsc TEXT,
  preferred_rail TEXT NOT NULL DEFAULT 'upi',
  credit_days INTEGER DEFAULT 0,
  vendor_type TEXT NOT NULL DEFAULT 'established',
  drug_schedule TEXT,
  is_compliant INTEGER DEFAULT 1,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  persona TEXT NOT NULL,
  instruction TEXT NOT NULL,
  invoice_source TEXT NOT NULL,
  pine_token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  total_vendors INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  paid_amount REAL DEFAULT 0,
  deferred_amount REAL DEFAULT 0,
  float_saved REAL DEFAULT 0,
  started_at DATETIME NOT NULL,
  completed_at DATETIME,
  approved_at DATETIME
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  pine_order_id TEXT NOT NULL,
  merchant_order_reference TEXT NOT NULL,
  amount REAL NOT NULL,
  priority_score INTEGER NOT NULL,
  priority_reason TEXT NOT NULL,
  action TEXT NOT NULL,
  pre_auth INTEGER DEFAULT 0,
  pine_status TEXT NOT NULL DEFAULT 'CREATED',
  escalation_flag TEXT,
  defer_reason TEXT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  order_id TEXT NOT NULL REFERENCES orders(id),
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  pine_order_id TEXT NOT NULL,
  pine_payment_id TEXT,
  merchant_payment_reference TEXT NOT NULL,
  amount REAL NOT NULL,
  rail TEXT NOT NULL,
  attempt_number INTEGER DEFAULT 1,
  pine_status TEXT NOT NULL DEFAULT 'PENDING',
  failure_reason TEXT,
  recovery_action TEXT,
  webhook_event TEXT,
  request_id TEXT NOT NULL,
  initiated_at DATETIME NOT NULL,
  confirmed_at DATETIME
);

CREATE TABLE IF NOT EXISTS settlements (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  pine_order_id TEXT NOT NULL,
  pine_settlement_id TEXT,
  utr_number TEXT NOT NULL,
  bank_account TEXT,
  last_processed_date DATETIME NOT NULL,
  expected_amount REAL NOT NULL,
  settled_amount REAL NOT NULL,
  platform_fee REAL DEFAULT 0,
  total_deduction_amount REAL DEFAULT 0,
  refund_debit REAL DEFAULT 0,
  fee_flagged INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  settled_at DATETIME,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  order_id TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  settlement_id TEXT,
  vendor_id TEXT NOT NULL,
  pine_order_id TEXT NOT NULL,
  merchant_order_reference TEXT NOT NULL,
  utr_number TEXT,
  persona TEXT NOT NULL,
  invoice_amount REAL NOT NULL,
  paid_amount REAL DEFAULT 0,
  settled_amount REAL DEFAULT 0,
  variance REAL DEFAULT 0,
  mdr_rate_actual REAL,
  mdr_rate_contracted REAL,
  mdr_drift_flagged INTEGER DEFAULT 0,
  rail_used TEXT,
  retries INTEGER DEFAULT 0,
  outcome TEXT NOT NULL,
  pre_auth_used INTEGER DEFAULT 0,
  agent_reasoning TEXT NOT NULL,
  ca_notes TEXT,
  created_at DATETIME NOT NULL
);

-- Core indexes
CREATE INDEX IF NOT EXISTS idx_orders_pine    ON orders(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_mor     ON orders(merchant_order_reference);
CREATE INDEX IF NOT EXISTS idx_orders_run     ON orders(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(pine_status);
CREATE INDEX IF NOT EXISTS idx_pay_pine       ON payments(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_pay_run        ON payments(run_id);
CREATE INDEX IF NOT EXISTS idx_pay_status     ON payments(pine_status);
CREATE INDEX IF NOT EXISTS idx_set_utr        ON settlements(utr_number);
CREATE INDEX IF NOT EXISTS idx_set_pine       ON settlements(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_set_date       ON settlements(last_processed_date);
CREATE INDEX IF NOT EXISTS idx_rec_run        ON reconciliations(run_id);
CREATE INDEX IF NOT EXISTS idx_rec_persona    ON reconciliations(persona);
CREATE INDEX IF NOT EXISTS idx_rec_date       ON reconciliations(created_at);
CREATE INDEX IF NOT EXISTS idx_rec_outcome    ON reconciliations(outcome);
