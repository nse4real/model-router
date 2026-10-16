CREATE TABLE IF NOT EXISTS spend_lock (id INTEGER PRIMARY KEY);
INSERT INTO spend_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, outcome TEXT NOT NULL DEFAULT 'pending', tier TEXT,
 final_model TEXT, inference_cost NUMERIC NOT NULL DEFAULT 0,
 evaluation_cost NUMERIC NOT NULL DEFAULT 0, latency_ms NUMERIC DEFAULT 0,
 cache_hit INTEGER DEFAULT 0, fallback_count INTEGER DEFAULT 0,
 quality_status TEXT DEFAULT 'not_sampled', run_id TEXT, created_at NUMERIC NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, parent_request_id TEXT NOT NULL REFERENCES requests(request_id),
 model TEXT NOT NULL, provider TEXT NOT NULL, purpose TEXT NOT NULL,
 input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, price_version TEXT NOT NULL,
 reserved NUMERIC NOT NULL, cost NUMERIC NOT NULL, state TEXT NOT NULL,
 cost_status TEXT NOT NULL, error TEXT, created_at NUMERIC NOT NULL,
 CHECK (state IN ('reserved','dispatched','settled','unknown')),
 CHECK (purpose IN ('inference','evaluation'))
);
CREATE TABLE IF NOT EXISTS judge_jobs (
 attempt_id TEXT PRIMARY KEY, parent_request_id TEXT NOT NULL REFERENCES requests(request_id),
 state TEXT NOT NULL, score NUMERIC, created_at NUMERIC NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attempts_parent ON attempts(parent_request_id);
CREATE INDEX IF NOT EXISTS requests_run ON requests(run_id);
