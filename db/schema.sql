-- RingMind database. Applied on every start; every statement is idempotent.
-- Business tables (plans .. callbacks) stand in for your real systems; replace the CRM adapter
-- in app/tools/crm.py when you connect your own. Call tables (calls, call_events) belong to the bot.

CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    monthly_price NUMERIC(8, 2) NOT NULL,
    summary       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id             SERIAL PRIMARY KEY,
    full_name      TEXT NOT NULL,
    phone          TEXT NOT NULL UNIQUE,          -- E.164, e.g. +15551234567
    email          TEXT,
    plan_id        TEXT REFERENCES plans(id),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed')),
    balance_due    NUMERIC(10, 2) NOT NULL DEFAULT 0,
    next_bill_date DATE,
    autopay        BOOLEAN NOT NULL DEFAULT FALSE,
    date_of_birth  DATE,
    pin_hash       TEXT,                           -- pbkdf2-sha256$iterations$salt$hash
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    amount      NUMERIC(10, 2) NOT NULL,
    method      TEXT NOT NULL,
    paid_on     DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,              -- e.g. ORD-1042
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    item            TEXT NOT NULL,
    total           NUMERIC(10, 2) NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'delayed', 'cancelled')),
    carrier         TEXT,
    tracking_number TEXT,
    ordered_on      DATE NOT NULL,
    shipped_on      DATE,
    eta             DATE,
    delivered_on    DATE
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id, ordered_on DESC);

CREATE TABLE IF NOT EXISTS stores (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    address   TEXT NOT NULL,
    opens_at  TIME NOT NULL,
    closes_at TIME NOT NULL,
    open_days INTEGER[] NOT NULL                   -- ISO weekdays, 1 = Monday
);

CREATE TABLE IF NOT EXISTS appointments (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    store_id    TEXT NOT NULL REFERENCES stores(id),
    starts_at   TIMESTAMP NOT NULL,                -- store-local time
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One booking per store per slot.
CREATE UNIQUE INDEX IF NOT EXISTS appointments_slot_uniq ON appointments (store_id, starts_at) WHERE status = 'booked';

CREATE TABLE IF NOT EXISTS tickets (
    id           SERIAL PRIMARY KEY,
    customer_id  INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    caller_phone TEXT,
    call_id      TEXT,
    subject      TEXT NOT NULL,
    description  TEXT NOT NULL,
    priority     TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS callbacks (
    id             SERIAL PRIMARY KEY,
    customer_id    INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    phone          TEXT NOT NULL,
    call_id        TEXT,
    preferred_time TEXT NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Bot-owned: one row per call, one row per transcript line / tool call / metric ----

CREATE TABLE IF NOT EXISTS calls (
    id              TEXT PRIMARY KEY,
    transport       TEXT NOT NULL,
    caller_phone    TEXT,
    customer_id     INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    outcome         TEXT,
    verified        BOOLEAN NOT NULL DEFAULT FALSE,
    turns           INTEGER NOT NULL DEFAULT 0,
    avg_response_ms INTEGER,
    models_used     TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS calls_started_idx ON calls (started_at DESC);

CREATE TABLE IF NOT EXISTS call_events (
    id      BIGSERIAL PRIMARY KEY,
    call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind    TEXT NOT NULL,                         -- caller | bot | tool | system | metric
    text    TEXT,
    data    JSONB
);
CREATE INDEX IF NOT EXISTS call_events_call_idx ON call_events (call_id, id);
