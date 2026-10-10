"""db.py — SQLite connection & schema.

One get_conn() helper for the whole system (scheduler, CLI, backup, verify):
  - journal_mode=WAL (persistent, set once at init)
  - busy_timeout=10000 PER CONNECTION (not persistent!)
  - foreign_keys=ON PER CONNECTION (SQLite disables FKs by default)
  - synchronous=NORMAL (WAL; worst case loses the last commit, no corruption)
  - every write transaction must use BEGIN IMMEDIATE (isolation_level=None)
Migrations run only via numbered scripts; a connect-time guard refuses to open
a DB whose schema version is older than the code (except with allow_init).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC
from pathlib import Path

SCHEMA_VERSION = 35

SCHEMA_V1 = """
CREATE TABLE series_registry (
  series_id TEXT PRIMARY KEY, name TEXT NOT NULL, block TEXT NOT NULL,
  tier INTEGER NOT NULL, unit TEXT NOT NULL, value_format TEXT NOT NULL,
  freq TEXT NOT NULL, ts_convention TEXT,
  release_schedule TEXT, expected_start TEXT,
  sanity_min REAL, sanity_max REAL,
  primary_source TEXT NOT NULL, secondary_source TEXT, tolerance REAL,
  active INTEGER DEFAULT 1
);

CREATE TABLE raw_observations (
  series_id TEXT NOT NULL REFERENCES series_registry,
  ts TEXT NOT NULL, release_ts TEXT NOT NULL DEFAULT 'na',
  value REAL NOT NULL,
  vintage_ts TEXT NOT NULL DEFAULT 'realtime',
  source TEXT NOT NULL, precision_k INTEGER, fetched_at TEXT NOT NULL,
  PRIMARY KEY (series_id, ts, source, vintage_ts)
) WITHOUT ROWID;
CREATE INDEX idx_raw_series_ts ON raw_observations(series_id, ts DESC);

CREATE TABLE instrument_prices (
  symbol TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  adjusted INTEGER DEFAULT 0,
  PRIMARY KEY (symbol, ts, source)
) WITHOUT ROWID;

CREATE TABLE events (
  event_uid TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, release_ts TEXT,
  country TEXT NOT NULL, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
  importance TEXT,
  consensus REAL, consensus_source TEXT, actual REAL, actual_source TEXT, previous REAL,
  surprise_z REAL, is_curated INTEGER DEFAULT 0
);
CREATE INDEX idx_events_norm_ts ON events(normalized_name, ts_utc);
CREATE TABLE indicator_stats (
  indicator TEXT NOT NULL, as_of TEXT NOT NULL,
  sigma REAL NOT NULL, n_obs INTEGER NOT NULL, window TEXT NOT NULL, low_conf INTEGER DEFAULT 0,
  PRIMARY KEY (indicator, as_of)
);

CREATE TABLE cot_raw (
  report_date TEXT NOT NULL, contract_code TEXT NOT NULL, report_type TEXT NOT NULL,
  release_ts TEXT NOT NULL,
  category TEXT NOT NULL,
  long INTEGER, short INTEGER, spread INTEGER, open_interest_all INTEGER,
  pct_of_oi REAL, conc_top4_long REAL, conc_top4_short REAL,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (report_date, contract_code, report_type, category)
);

CREATE TABLE flows_daily (
  date TEXT PRIMARY KEY, gld_tonnes REAL, slv_shares REAL,
  btc_etf_musd REAL, eth_etf_musd REAL, funding_bps REAL, oi_btc REAL, oi_eth REAL, stablecoin_usd REAL
);
CREATE TABLE flows_periodic (
  period TEXT NOT NULL, kind TEXT NOT NULL,
  value_raw REAL NOT NULL, unit_raw TEXT NOT NULL, factor REAL, value REAL,
  meta_json TEXT, PRIMARY KEY (period, kind)
);

CREATE TABLE cme_settlements (
  trade_date TEXT NOT NULL, product_id INTEGER NOT NULL, month TEXT NOT NULL,
  settle REAL, volume REAL, open_interest REAL, fetched_at TEXT,
  PRIMARY KEY (trade_date, product_id, month)
);
CREATE TABLE cvol_snapshots (
  trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
  cvol REAL, atm REAL, skew REAL, upvar REAL, dnvar REAL, convexity REAL,
  PRIMARY KEY (trade_date, symbol)
);
CREATE TABLE voi_daily (
  trade_date TEXT NOT NULL, product_id INTEGER NOT NULL, report_type TEXT NOT NULL,
  volume REAL, oi REAL, oi_diff REAL,
  PRIMARY KEY (trade_date, product_id, report_type)
);
CREATE TABLE fedwatch_snapshots (
  date TEXT NOT NULL, meeting_date TEXT NOT NULL, source TEXT NOT NULL,
  prob_ease REAL, prob_hold REAL, prob_hike REAL, implied_rate REAL,
  d1 REAL, d7 REAL, d30 REAL,
  raw_json TEXT, PRIMARY KEY (date, meeting_date, source)
);

CREATE TABLE computed_signals (
  signal_id TEXT NOT NULL, ts TEXT NOT NULL, run_id TEXT NOT NULL, computed_at TEXT NOT NULL,
  value REAL, state TEXT, inputs_json TEXT,
  PRIMARY KEY (signal_id, ts)
);
CREATE TABLE fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, fetcher TEXT NOT NULL, target TEXT,
  status TEXT NOT NULL, http_status INTEGER, schema_fp TEXT,
  error TEXT, duration_ms INTEGER, rows INTEGER, quota_used INTEGER
);
CREATE TABLE brief_log (
  date TEXT PRIMARY KEY, markdown TEXT NOT NULL, regime_score REAL,
  quality_flags_json TEXT, generated_at TEXT
);
CREATE TABLE brief_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brief_date TEXT NOT NULL, channel TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  telegram_message_ids TEXT, attempts INTEGER DEFAULT 0,
  last_error TEXT, sent_at TEXT, created_at TEXT NOT NULL
  -- intentionally no FK: brief_log is always written in the same transaction
);
CREATE UNIQUE INDEX uq_brief_delivery ON brief_deliveries(brief_date, channel);
CREATE TABLE alert_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type TEXT NOT NULL, triggered_at TEXT NOT NULL,
  cooldown_key TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'pending', telegram_message_id INTEGER,
  attempts INTEGER DEFAULT 0, last_error TEXT, sent_at TEXT
);
CREATE INDEX idx_alert_cd ON alert_deliveries(cooldown_key, triggered_at DESC);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
"""

MIGRATIONS: dict[int, str] = {
    1: SCHEMA_V1,
    2: """-- v2: full COT — add traders, conc-8, weekly changes
ALTER TABLE cot_raw ADD COLUMN conc_top8_long REAL;
ALTER TABLE cot_raw ADD COLUMN conc_top8_short REAL;
ALTER TABLE cot_raw ADD COLUMN traders_long INTEGER;
ALTER TABLE cot_raw ADD COLUMN traders_short INTEGER;
ALTER TABLE cot_raw ADD COLUMN change_long INTEGER;
ALTER TABLE cot_raw ADD COLUMN change_short INTEGER;
""",
    3: """-- v3: alert retry — alert messages are persisted so failed sends can be retried
ALTER TABLE alert_deliveries ADD COLUMN message TEXT;
""",
    4: """-- v4: indicator family key for σ grouping across releases
-- (month/FINAL suffixes and US prefix stripped)
ALTER TABLE events ADD COLUMN indicator_key TEXT;
CREATE INDEX IF NOT EXISTS idx_events_indkey ON events(indicator_key, ts_utc);
""",
    5: """-- v5: (a) claimed_at so a sending row can be re-claimed after >10 min;
--     (b) drop tables with no writer and no reader (golden_anchors:
--         source is YAML; cot_snapshots: z is computed on-the-fly)
ALTER TABLE brief_deliveries ADD COLUMN claimed_at TEXT;
DROP TABLE IF EXISTS golden_anchors;
DROP TABLE IF EXISTS cot_snapshots;
""",
    6: """-- v6: alert retry pacing — retries also run on each watch cycle, with a
--     minimum 5-minute gap between attempts per row
ALTER TABLE alert_deliveries ADD COLUMN last_attempt TEXT;
""",
    7: """-- v7: SOMA per-CUSIP — Fed balance sheet granularity
-- [RISET: live-verified 2026-09-03 — NY Fed /api/soma/tsy/get/asof/{date}.json;
--  identity check vs official /api/soma/summary.json matched to the dollar]
-- UNITS: par_value/change_week/rolling_off_* are RAW USD; pct_outstanding is
-- PERCENT 0-100 (API returns a fraction: 0.6999 = the 70% Fed ownership cap).
CREATE TABLE IF NOT EXISTS soma_holdings (
  as_of_date TEXT NOT NULL,
  cusip TEXT NOT NULL,
  security_type TEXT,
  maturity_date TEXT,
  par_value REAL,
  pct_outstanding REAL,
  change_week REAL,
  PRIMARY KEY (as_of_date, cusip)
);
CREATE INDEX IF NOT EXISTS idx_soma_cusip ON soma_holdings(cusip, as_of_date);
CREATE TABLE IF NOT EXISTS soma_summary (
  as_of_date TEXT PRIMARY KEY,
  total_par REAL, bills REAL, notes_bonds REAL, tips REAL, frn REAL,
  weekly_change REAL,
  rolling_off_7d REAL, rolling_off_30d REAL, rolling_off_90d REAL,
  n_cusips INTEGER, avg_maturity_years REAL
);
""",
    8: """-- v8: CME options settlements per-strike (PLAN-CME-OPTIONS §2.3)
-- [RISET: live-verified 2026-09-03 — CmeWS /Settlements/Options/{pid}/OOF,
--  7 produk (OG/SO/HXE/PO/ES/NQ/BTC), OG satu expiry = 1.281 baris]
-- NOTE: baris type='' pada respons = baris TOTAL kontrak (settle='-'), BUKAN
-- settle futures underlying — anchor underlying cme_option_underlyings
-- di-join dari strip futures cme_settlements (lihat cme_harvest._save_underlying).
CREATE TABLE IF NOT EXISTS cme_options_settlements (
  trade_date TEXT NOT NULL,
  product_id INTEGER NOT NULL,
  product_code TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  option_type TEXT NOT NULL,
  strike REAL NOT NULL,
  settle REAL,
  volume INTEGER,
  open_interest INTEGER,
  PRIMARY KEY (trade_date, product_id, contract_id, option_type, strike)
);
CREATE TABLE IF NOT EXISTS cme_option_underlyings (
  trade_date TEXT NOT NULL,
  product_id INTEGER NOT NULL,
  contract_id TEXT NOT NULL,
  settle REAL NOT NULL,
  PRIMARY KEY (trade_date, contract_id)
);
""",
    9: """-- v9: NY Fed full utilization (PLAN-NYFED-FULL §2)
-- [RISET: live-verified 2026-09-03 — transport = plain requests (curl_cffi
--  double-encodes %20 → the "agency debts" path 400s); MBS/CMBS rows carry
--  currentFaceValue (parValue is EMPTY for them — the "all" enum mixes
--  schemas, never sum parValue across it); agency-debts rows look like tsy
--  rows (parValue/coupon/maturity) and are normalized into current_face_value]
-- UNITS: soma_agency_* money is RAW USD (like soma_holdings); pd_positions
-- value_musd is NATIVE $millions; fed_operations amount is raw USD as
-- reported (tsy: totalParAmtAccepted; ambs: totalAcceptedOrigFace).
CREATE TABLE IF NOT EXISTS soma_agency_holdings (
  as_of_date TEXT NOT NULL, cusip TEXT NOT NULL, asset_type TEXT NOT NULL,
  security_description TEXT, term TEXT, issuer TEXT,
  current_face_value REAL, change_week REAL,
  PRIMARY KEY (as_of_date, cusip, asset_type)
);
CREATE TABLE IF NOT EXISTS soma_agency_summary (
  as_of_date TEXT PRIMARY KEY, mbs REAL, cmbs REAL, agency_debts REAL, total REAL
);
CREATE TABLE IF NOT EXISTS fed_operations (
  operation_id TEXT NOT NULL, family TEXT NOT NULL,
  operation_date TEXT NOT NULL, settlement_date TEXT,
  operation_type TEXT, direction TEXT, maturity_start TEXT, maturity_end TEXT,
  status TEXT, amount REAL, details_json TEXT,
  PRIMARY KEY (operation_id, family)
);
CREATE TABLE IF NOT EXISTS pd_positions (
  asofdate TEXT NOT NULL, keyid TEXT NOT NULL,
  seriesbreak TEXT, value_musd REAL,
  PRIMARY KEY (asofdate, keyid)
);
CREATE TABLE IF NOT EXISTS soma_wam (
  as_of_date TEXT NOT NULL, wam_type TEXT NOT NULL, years REAL,
  PRIMARY KEY (as_of_date, wam_type)
);
""",
    10: """-- v10: fiscaldata expansion (5 dataset audit 2026-09-04): auctions,
--     DTS public debt transactions, monthly interest expense + avg rates.
--     debt_to_penny does NOT get a table — FISCAL:DEBT_PUBLIC/INTRAGOV/TOTAL
--     are registry series (scalar-series pattern, harvested at 06:00).
-- [RISET: live-verified 2026-09-04 — fd_debt_transactions.amount_today is
--  $ MILLIONS (DTS convention), NOT raw USD: FYTD identity Σ(issues−
--  redemptions) = Δdebt_to_penny 2025-09-30→2026-09-02 matched to the DOLLAR
--  (2,479,492 $M); fd_auctions units: price_per100 = per $100 face,
--  avg_med_yield = %, bid_to_cover = ratio, allocation_pct = %.]
-- NOTE: DTS rows carry a 5th dimension security_type_desc (Bills 'Regular
-- Series' vs 'Cash Management Series'; savings 'Cash Issue Price' vs
-- 'Interest Increment') that is NOT in this locked PK — the fetcher folds it
-- into security_type ('Bills (Cash Management Series)') so no API row is
-- collapsed. fd_interest_expense nullable PK legs are stored as '' (SQLite
-- treats NULL PK values as DISTINCT — '' keeps the upsert deduplicating).
CREATE TABLE IF NOT EXISTS fd_auctions (
  auction_date TEXT NOT NULL, cusip TEXT NOT NULL,
  security_type TEXT, security_term TEXT,
  issue_date TEXT, maturity_date TEXT,
  price_per100 REAL, avg_med_yield REAL, bid_to_cover REAL,
  allocation_pct REAL, auction_format TEXT,
  PRIMARY KEY (auction_date, cusip)
);
CREATE TABLE IF NOT EXISTS fd_debt_transactions (
  record_date TEXT NOT NULL, transaction_type TEXT NOT NULL,
  security_market TEXT NOT NULL, security_type TEXT NOT NULL,
  amount_today REAL,
  PRIMARY KEY (record_date, transaction_type, security_market, security_type)
);
CREATE TABLE IF NOT EXISTS fd_interest_expense (
  record_date TEXT NOT NULL, expense_catg_desc TEXT NOT NULL,
  expense_group_desc TEXT, expense_type_desc TEXT,
  month_amt REAL, fytd_amt REAL,
  PRIMARY KEY (record_date, expense_catg_desc, expense_group_desc, expense_type_desc)
);
CREATE TABLE IF NOT EXISTS fd_avg_rates (
  record_date TEXT NOT NULL, security_desc TEXT NOT NULL,
  security_type_desc TEXT, avg_interest_rate REAL,
  PRIMARY KEY (record_date, security_desc)
);
""",
    11: """-- v11: hot-path indexes for the growing tables.
-- [RISET: scale review 2026-09-08 — cme_options_settlements grows ~23.7k
--  rows/day and the watcher scans it PER PRODUCT every 60s (options PCR
--  trigger) with product_code in NO index; at ~6M rows (12mo) that is
--  seconds per cycle, forever. The covering index turns _pcr_history into
--  an index-only range scan and MAX(trade_date) into a seek.
--  cot_raw(contract_code,...) same class: _cot_zscore full-scans every
--  cycle (bounded history, but 3 contracts x 1440 cycles/day).]
CREATE INDEX IF NOT EXISTS idx_opt_prod ON cme_options_settlements(
  product_code, trade_date, contract_id, option_type, open_interest
);
CREATE INDEX IF NOT EXISTS idx_cot_contract ON cot_raw(
  contract_code, report_type, category, report_date
);
""",
    12: """-- v12: Bybit positioning extras (audit sumber 2026-09-08).
-- [RISET: /v5/market/account-ratio + taker-volume + open-interest history —
--  public, no key; connectivity from this network is intermittent at TCP
--  level, so harvest is NULL-tolerant per the funding convention.]
CREATE TABLE IF NOT EXISTS bybit_positioning (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  ls_ratio REAL,            -- long-account SHARE 0..1 (Bybit accountLongRatio; neutral 0.5)
  taker_buy_ratio REAL,     -- taker buy share of volume (>0.5 = net aggressive buying)
  oi REAL,                  -- open interest (contracts) from the history endpoint
  PRIMARY KEY (symbol, date)
);
""",
    13: """-- v13: Farside per-issuer ETF flows (audit sumber 2026-09-10).
-- [RISET: the aggregate net flow hides the structural GBTC-outflow vs
--  IBIT-inflow divergence — the actual ETF-flow story. Issuer cells were
--  already in the fetched HTML and dropped by the old 1-number parser.]
CREATE TABLE IF NOT EXISTS etf_flows_issuer (
  date TEXT NOT NULL,
  etf TEXT NOT NULL,        -- 'BTC' | 'ETH'
  issuer TEXT NOT NULL,     -- IBIT, FBTC, ..., GBTC / ETHA, ..., ETHE
  flow_musd REAL,
  PRIMARY KEY (date, etf, issuer)
);
""",
    14: """-- v14: FMP earnings calendar (vendor-api audit NICE #7, owner GO
-- 2026-09-13). Data-first phase: the table is the deliverable — the
-- weekly heavy-weight share lands in computed_signals; no brief line
-- (delivery paused).
CREATE TABLE IF NOT EXISTS earnings_calendar (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,             -- announcement date
  eps_estimated REAL,
  eps_actual REAL,
  revenue_estimated REAL,
  revenue_actual REAL,
  last_updated TEXT,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);
""",
    15: """-- v15: ETH funding leg (ronde-6 P2-7) — computed every run since the
-- flows job existed but never stored (flows_daily had only the BTC column).
ALTER TABLE flows_daily ADD COLUMN funding_eth REAL;
""",
    16: """-- v16: calendar_family (round-3 queue) — the events-table family a
-- series covers, so the HOUST/PERMIT gap class is structurally visible
-- (qa/coverage.py: calendar families with actuals but no covering series).
ALTER TABLE series_registry ADD COLUMN calendar_family TEXT;
""",
    17: """-- v17: price_quarantine (round-10) — tombstones for poison price
-- bars: the persistent-wedge repair NULLs a vendor-wrong close, but the
-- vendor keeps serving it and the COALESCE upsert refills NULLs; the
-- tombstone makes insert_prices skip the (symbol, ts, source) forever.
CREATE TABLE price_quarantine (
  symbol TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (symbol, ts, source)
);
""",
    18: """CREATE TABLE IF NOT EXISTS intraday_bars (
  symbol TEXT NOT NULL, bar_ts_utc TEXT NOT NULL, interval TEXT NOT NULL,
  source TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
  fetched_at TEXT NOT NULL, PRIMARY KEY (symbol, bar_ts_utc, interval, source)
);
CREATE INDEX IF NOT EXISTS idx_intraday_symbol_ts ON intraday_bars(symbol, bar_ts_utc DESC);
CREATE TABLE IF NOT EXISTS market_breadth (
  ts_utc TEXT NOT NULL, universe TEXT NOT NULL, source TEXT NOT NULL,
  advances INTEGER NOT NULL, declines INTEGER NOT NULL, unchanged INTEGER NOT NULL,
  pct_advancing REAL NOT NULL, equal_weight_return REAL, cap_weight_return REAL,
  details_json TEXT NOT NULL, PRIMARY KEY (ts_utc, universe, source)
);
""",
    19: """CREATE TABLE IF NOT EXISTS crypto_derivatives (
  ts_utc TEXT NOT NULL, source TEXT NOT NULL, instrument TEXT NOT NULL,
  metric TEXT NOT NULL, value REAL NOT NULL, raw_json TEXT NOT NULL,
  PRIMARY KEY (ts_utc, source, instrument, metric)
);
""",
    20: """CREATE TABLE IF NOT EXISTS market_news (
  news_id TEXT PRIMARY KEY, published_at_utc TEXT NOT NULL, source TEXT NOT NULL,
  title TEXT NOT NULL, url TEXT, summary TEXT, symbols_json TEXT NOT NULL,
  cluster_id TEXT NOT NULL, relevance REAL NOT NULL, novelty REAL NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_news_time ON market_news(published_at_utc DESC);
CREATE TABLE IF NOT EXISTS equity_breadth_components (
  ts_utc TEXT NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL,
  change_pct REAL, price REAL, market_cap REAL, PRIMARY KEY (ts_utc, symbol, source)
);
""",
    21: """CREATE TABLE IF NOT EXISTS crypto_liquidations (
  event_uid TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, source TEXT NOT NULL,
  instrument TEXT NOT NULL, position_side TEXT, price REAL, size REAL,
  notional_usd REAL, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liquidations_ts ON crypto_liquidations(ts_utc DESC);
""",
    22: """CREATE TABLE IF NOT EXISTS gdelt_events (
  event_id TEXT PRIMARY KEY, event_date TEXT NOT NULL, added_at_utc TEXT NOT NULL,
  actor1 TEXT, actor2 TEXT, event_code TEXT, quad_class INTEGER,
  goldstein_scale REAL, mentions INTEGER, sources INTEGER, articles INTEGER,
  avg_tone REAL, action_country TEXT, action_lat REAL, action_lon REAL,
  source_url TEXT, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gdelt_added ON gdelt_events(added_at_utc DESC);
""",
    23: """ALTER TABLE gdelt_events ADD COLUMN raw_record_json TEXT NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS market_news_payloads (
  observation_id TEXT PRIMARY KEY, news_id TEXT NOT NULL, source TEXT NOT NULL,
  fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_payload_news ON market_news_payloads(news_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_payload_source ON market_news_payloads(source, fetched_at DESC);
""",
    24: """CREATE TABLE IF NOT EXISTS gdelt_mentions (
  observation_id TEXT PRIMARY KEY, event_id TEXT, event_time TEXT, mention_time TEXT,
  mention_type TEXT, source_name TEXT, document_url TEXT, raw_record_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_event ON gdelt_mentions(event_id, mention_time);
CREATE INDEX IF NOT EXISTS idx_gdelt_mentions_source_time ON gdelt_mentions(source_name, mention_time DESC);
CREATE TABLE IF NOT EXISTS gdelt_gkg (
  record_id TEXT PRIMARY KEY, record_time TEXT NOT NULL, source_name TEXT,
  document_url TEXT, themes_json TEXT NOT NULL, entities_json TEXT NOT NULL,
  locations_json TEXT NOT NULL, tone_json TEXT NOT NULL, raw_record_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gdelt_gkg_time ON gdelt_gkg(record_time DESC);
""",
    25: """CREATE TABLE IF NOT EXISTS crypto_instruments (
  instrument TEXT NOT NULL, source TEXT NOT NULL, observed_at_utc TEXT NOT NULL,
  raw_json TEXT NOT NULL, PRIMARY KEY (instrument, source, observed_at_utc)
);
CREATE INDEX IF NOT EXISTS idx_crypto_instruments_latest ON crypto_instruments(instrument, observed_at_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_trade_events (
  event_uid TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, source TEXT NOT NULL,
  instrument TEXT NOT NULL, trade_id TEXT NOT NULL, aggressor_side TEXT,
  price REAL, size_contracts REAL, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crypto_trades_ts ON crypto_trade_events(instrument, ts_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_orderbook_snapshots (
  ts_utc TEXT NOT NULL, source TEXT NOT NULL, instrument TEXT NOT NULL,
  bid_size_top5 REAL, ask_size_top5 REAL, imbalance_top5 REAL,
  raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (ts_utc, source, instrument)
);
CREATE INDEX IF NOT EXISTS idx_crypto_books_ts ON crypto_orderbook_snapshots(instrument, ts_utc DESC);
""",
    26: """ALTER TABLE crypto_liquidations ADD COLUMN size_asset REAL;
ALTER TABLE crypto_liquidations ADD COLUMN notional_usd_calculated REAL;
ALTER TABLE crypto_liquidations ADD COLUMN sizing_basis TEXT;
ALTER TABLE crypto_trade_events ADD COLUMN size_asset REAL;
ALTER TABLE crypto_trade_events ADD COLUMN notional_usd REAL;
ALTER TABLE crypto_orderbook_snapshots ADD COLUMN bid_notional_usd_top5 REAL;
ALTER TABLE crypto_orderbook_snapshots ADD COLUMN ask_notional_usd_top5 REAL;
ALTER TABLE crypto_orderbook_snapshots ADD COLUMN imbalance_notional_usd_top5 REAL;
""",
    27: """CREATE TABLE IF NOT EXISTS crypto_trade_raw_batches (
  batch_id TEXT PRIMARY KEY, batch_ts_utc TEXT NOT NULL, source TEXT NOT NULL,
  instrument TEXT NOT NULL, trade_count INTEGER NOT NULL,
  first_trade_ts TEXT NOT NULL, last_trade_ts TEXT NOT NULL,
  first_trade_id TEXT NOT NULL, last_trade_id TEXT NOT NULL,
  payload_gzip BLOB NOT NULL, payload_sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crypto_trade_batches_ts ON crypto_trade_raw_batches(batch_ts_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_trade_flow_1m (
  minute_utc TEXT NOT NULL, instrument TEXT NOT NULL, source TEXT NOT NULL,
  trade_count INTEGER NOT NULL, buy_count INTEGER NOT NULL, sell_count INTEGER NOT NULL,
  buy_contracts REAL NOT NULL, sell_contracts REAL NOT NULL,
  buy_asset REAL, sell_asset REAL, buy_notional_usd REAL, sell_notional_usd REAL,
  buy_normalized_count INTEGER NOT NULL, sell_normalized_count INTEGER NOT NULL,
  first_trade_ts TEXT NOT NULL, last_trade_ts TEXT NOT NULL,
  first_trade_id TEXT NOT NULL, last_trade_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (minute_utc, instrument, source)
);
CREATE INDEX IF NOT EXISTS idx_crypto_trade_flow_ts ON crypto_trade_flow_1m(instrument, minute_utc DESC);
CREATE TABLE IF NOT EXISTS crypto_trade_flow_state (
  instrument TEXT PRIMARY KEY, last_trade_id TEXT NOT NULL, updated_at TEXT NOT NULL
);
""",
    28: """ALTER TABLE gdelt_events ADD COLUMN raw_record_gzip BLOB;
ALTER TABLE gdelt_mentions ADD COLUMN raw_record_gzip BLOB;
ALTER TABLE gdelt_gkg ADD COLUMN raw_record_gzip BLOB;
""",
    29: """CREATE TABLE IF NOT EXISTS gdelt_feed_state (
  feed TEXT PRIMARY KEY CHECK (feed IN ('export','mentions','gkg')),
  last_window_ts_utc TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
""",
    30: """ALTER TABLE events ADD COLUMN sigma_vintage REAL;
ALTER TABLE events ADD COLUMN sigma_snapshot_id TEXT;
ALTER TABLE events ADD COLUMN sigma_n_obs INTEGER;
ALTER TABLE events ADD COLUMN sigma_window TEXT;
ALTER TABLE events ADD COLUMN sigma_low_conf INTEGER;
CREATE TABLE indicator_sigma_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  as_of TEXT NOT NULL,
  calculated_at TEXT NOT NULL,
  n_indicators INTEGER NOT NULL,
  method TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL
);
CREATE TABLE indicator_sigma_vintages (
  snapshot_id TEXT NOT NULL REFERENCES indicator_sigma_snapshots(snapshot_id),
  indicator TEXT NOT NULL,
  sigma REAL NOT NULL,
  n_obs INTEGER NOT NULL,
  window TEXT NOT NULL,
  low_conf INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, indicator)
);
CREATE INDEX idx_sigma_snapshots_calculated ON indicator_sigma_snapshots(calculated_at DESC);
CREATE TRIGGER immutable_sigma_snapshots_update BEFORE UPDATE ON indicator_sigma_snapshots
BEGIN SELECT RAISE(ABORT, 'sigma snapshots are immutable'); END;
CREATE TRIGGER immutable_sigma_snapshots_delete BEFORE DELETE ON indicator_sigma_snapshots
BEGIN SELECT RAISE(ABORT, 'sigma snapshots are immutable'); END;
CREATE TRIGGER immutable_sigma_vintages_update BEFORE UPDATE ON indicator_sigma_vintages
BEGIN SELECT RAISE(ABORT, 'sigma vintages are immutable'); END;
CREATE TRIGGER immutable_sigma_vintages_delete BEFORE DELETE ON indicator_sigma_vintages
BEGIN SELECT RAISE(ABORT, 'sigma vintages are immutable'); END;
INSERT OR IGNORE INTO indicator_sigma_snapshots
  (snapshot_id,as_of,calculated_at,n_indicators,method,payload_sha256)
SELECT s.run_id, i.as_of, s.computed_at, COUNT(*), 'legacy-daily-sigma', ''
FROM indicator_stats i JOIN computed_signals s
  ON s.signal_id='esi' AND s.ts=i.as_of
WHERE s.run_id IS NOT NULL AND s.computed_at IS NOT NULL
GROUP BY s.run_id,i.as_of,s.computed_at;
INSERT OR IGNORE INTO indicator_sigma_vintages
  (snapshot_id,indicator,sigma,n_obs,window,low_conf)
SELECT s.run_id,i.indicator,i.sigma,i.n_obs,i.window,i.low_conf
FROM indicator_stats i JOIN computed_signals s
  ON s.signal_id='esi' AND s.ts=i.as_of
WHERE s.run_id IS NOT NULL AND s.computed_at IS NOT NULL;
UPDATE events SET
  sigma_vintage=(SELECT v.sigma FROM indicator_sigma_vintages v
                 WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots)
                   AND v.indicator=events.indicator_key),
  sigma_snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots),
  sigma_n_obs=(SELECT v.n_obs FROM indicator_sigma_vintages v
               WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots)
                 AND v.indicator=events.indicator_key),
  sigma_window=(SELECT v.window FROM indicator_sigma_vintages v
                WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots)
                  AND v.indicator=events.indicator_key),
  sigma_low_conf=(SELECT v.low_conf FROM indicator_sigma_vintages v
                  WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots)
                    AND v.indicator=events.indicator_key)
WHERE events.surprise_z IS NOT NULL
  AND events.release_ts IS NOT NULL AND events.release_ts NOT IN ('', 'na')
  AND events.release_ts <= (SELECT calculated_at FROM indicator_sigma_snapshots
                            WHERE snapshot_id=(SELECT MAX(snapshot_id)
                                               FROM indicator_sigma_snapshots))
  AND EXISTS (SELECT 1 FROM indicator_sigma_vintages v
              WHERE v.snapshot_id=(SELECT MAX(snapshot_id) FROM indicator_sigma_snapshots)
                AND v.indicator=events.indicator_key);
""",
    31: """CREATE TABLE IF NOT EXISTS news_intelligence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  news_id TEXT NOT NULL REFERENCES market_news(news_id),
  asset TEXT NOT NULL,
  stance TEXT NOT NULL CHECK(stance IN ('BULLISH', 'BEARISH', 'NEUTRAL')),
  magnitude REAL NOT NULL,
  confidence REAL NOT NULL,
  macro_channel TEXT NOT NULL,
  impact_horizon TEXT NOT NULL,
  evidence_level TEXT NOT NULL CHECK(evidence_level IN ('OBSERVED', 'SOURCED', 'INFERRED')),
  evidence_quote TEXT NOT NULL,
  transmission_rationale TEXT NOT NULL,
  created_at TEXT NOT NULL,
  published_at_utc TEXT NOT NULL,
  UNIQUE(news_id, asset)
);
CREATE INDEX IF NOT EXISTS idx_news_intel_asset_time ON news_intelligence(asset, published_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_news_intel_channel ON news_intelligence(macro_channel);
CREATE INDEX IF NOT EXISTS idx_news_intel_news_id ON news_intelligence(news_id);
""",
    32: """CREATE TABLE IF NOT EXISTS playbook_scenarios (
  scenario_uid TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL CHECK(horizon IN ('INTRADAY', 'SWING')),
  direction TEXT NOT NULL CHECK(direction IN ('LONG', 'SHORT', 'NEUTRAL_RANGE')),
  scenario_id TEXT NOT NULL,
  title TEXT NOT NULL,
  trigger_condition TEXT NOT NULL,
  trigger_price REAL,
  target_profit REAL NOT NULL,
  invalidation_level REAL NOT NULL,
  risk_reward_ratio REAL NOT NULL,
  created_at_utc TEXT NOT NULL,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING_TRIGGER', 'ACTIVE', 'HIT_TARGET_WIN', 'HIT_STOP_LOSS', 'CANCELLED_EXPIRED')),
  triggered_at_utc TEXT,
  resolved_at_utc TEXT,
  entry_price REAL,
  exit_price REAL,
  mfe_points REAL DEFAULT 0.0,
  mae_points REAL DEFAULT 0.0,
  pnl_points REAL DEFAULT 0.0,
  r_multiple REAL DEFAULT 0.0,
  cfd_basis_offset REAL DEFAULT 0.0,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playbook_symbol_state ON playbook_scenarios(symbol, state);
CREATE INDEX IF NOT EXISTS idx_playbook_created ON playbook_scenarios(created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_playbook_session ON playbook_scenarios(session_id);
""",
    33: """-- v33: API server foundation. Hot-path indexes for the read API, an
-- async on-demand job queue (the daemon executes queued jobs; the API only
-- enqueues), an audit trail for every API write, a UI lock so the YAML
-- registry sync does not revert an operator's active toggle, and the
-- CANCELLED_MANUAL playbook state (a CHECK change = table rebuild).
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_brief_deliveries_status ON brief_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_status ON alert_deliveries(status);
ALTER TABLE series_registry ADD COLUMN locked_by_ui INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued', 'running', 'done', 'failed')),
  requested_by TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE playbook_scenarios_v33 (
  scenario_uid TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  horizon TEXT NOT NULL CHECK(horizon IN ('INTRADAY', 'SWING')),
  direction TEXT NOT NULL CHECK(direction IN ('LONG', 'SHORT', 'NEUTRAL_RANGE')),
  scenario_id TEXT NOT NULL,
  title TEXT NOT NULL,
  trigger_condition TEXT NOT NULL,
  trigger_price REAL,
  target_profit REAL NOT NULL,
  invalidation_level REAL NOT NULL,
  risk_reward_ratio REAL NOT NULL,
  created_at_utc TEXT NOT NULL,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING_TRIGGER', 'ACTIVE', 'HIT_TARGET_WIN',
    'HIT_STOP_LOSS', 'CANCELLED_EXPIRED', 'CANCELLED_MANUAL')),
  triggered_at_utc TEXT,
  resolved_at_utc TEXT,
  entry_price REAL,
  exit_price REAL,
  mfe_points REAL DEFAULT 0.0,
  mae_points REAL DEFAULT 0.0,
  pnl_points REAL DEFAULT 0.0,
  r_multiple REAL DEFAULT 0.0,
  cfd_basis_offset REAL DEFAULT 0.0,
  payload_json TEXT NOT NULL,
  note TEXT
);
INSERT INTO playbook_scenarios_v33 SELECT *, NULL FROM playbook_scenarios;
DROP TABLE playbook_scenarios;
ALTER TABLE playbook_scenarios_v33 RENAME TO playbook_scenarios;
CREATE INDEX IF NOT EXISTS idx_playbook_symbol_state ON playbook_scenarios(symbol, state);
CREATE INDEX IF NOT EXISTS idx_playbook_created ON playbook_scenarios(created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_playbook_session ON playbook_scenarios(session_id);
CREATE INDEX IF NOT EXISTS idx_playbook_state_created
  ON playbook_scenarios(state, created_at_utc);
""",
    34: """-- v34: read-path indexes (EXPLAIN-checked on a prod copy).
-- fetch_log had none: the freshness/brief per-target lookups and the 180-day
-- prune all full-scanned it. intraday_bars readers always pin interval, which
-- the PK (symbol, bar_ts_utc, ...) cannot seek on; the old (symbol, ts) index
-- was a strict prefix of that PK. raw_observations is WITHOUT ROWID with PK
-- (series_id, ts, ...), so idx_raw_series_ts only duplicated it.
CREATE INDEX IF NOT EXISTS idx_fetch_log_target_id ON fetch_log(target, id DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_log_ts ON fetch_log(ts);
CREATE INDEX IF NOT EXISTS idx_intraday_sym_int_ts
  ON intraday_bars(symbol, interval, bar_ts_utc DESC, source);
DROP INDEX IF EXISTS idx_intraday_symbol_ts;
DROP INDEX IF EXISTS idx_raw_series_ts;
""",
    35: """-- v35: per-series freshness ceiling for sources that publish slower than
-- their observation frequency suggests (daily data released weekly, annual
-- data a year late, the monthly LME file). NULL = the per-freq default.
ALTER TABLE series_registry ADD COLUMN max_age_days INTEGER;
-- the advance goods release arrives as '... ADV' (alias added in qa/calendar.py);
-- upserts never rewrite indicator_key, so relabel the rows already stored
UPDATE events SET indicator_key = 'GOODS TRADE BALANCE'
  WHERE indicator_key = 'GOODS TRADE BALANCE ADV';
""",
}


REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def data_dir() -> Path:
    """Runtime data dir (DB/state/logs): env ARKWATCH_DATA_DIR, else repo data/."""
    return Path(os.environ.get("ARKWATCH_DATA_DIR") or REPO_DATA_DIR)


def default_db_path() -> Path:
    """env ARKWATCH_DB, else <data_dir>/arkwatch.db — resolved at call time."""
    return Path(os.environ.get("ARKWATCH_DB") or data_dir() / "arkwatch.db")


def _cache_pragmas(conn: sqlite3.Connection) -> None:
    # sorts/temp B-trees (GROUP BY, ORDER BY) stay in RAM; 16 MB page cache
    # (default 2 MB) holds the hot indexes. No mmap: WAL + several processes.
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16384")


def _open_read_only(path: Path) -> sqlite3.Connection:
    """URI mode=ro + query_only: can neither create, migrate nor write the DB.
    Rows come back as sqlite3.Row (index AND name access)."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist (read-only connections never create)")
    conn = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,  # FastAPI may run a dependency and its endpoint on 2 threads
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    _cache_pragmas(conn)
    current = _schema_version(conn)
    if current != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"{path}: schema v{current} != code v{SCHEMA_VERSION} — a read-only "
            "connection never migrates; run any CLI job (writer) first."
        )
    return conn


def get_conn(
    path: str | Path, *, read_only: bool = False, allow_init: bool = False
) -> sqlite3.Connection:
    """Connection with the standard PRAGMA pack. isolation_level=None → writes use explicit BEGIN IMMEDIATE.

    read_only=True: URI mode=ro + PRAGMA query_only=ON + busy_timeout=5000;
    never creates or migrates (FileNotFoundError / RuntimeError instead)."""
    path = Path(path)
    if read_only:
        return _open_read_only(path)
    fresh = not path.exists()
    if fresh or allow_init:
        path.parent.mkdir(parents=True, exist_ok=True)  # sqlite does not create parent dirs
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # cap the WAL file left behind after a checkpoint (it is reused, never
    # shrunk, without a limit) and checkpoint every ~16 MB of 4 KB pages
    # instead of the 1000-page default — fewer stalls for batch writers
    conn.execute("PRAGMA journal_size_limit=67108864")
    conn.execute("PRAGMA wal_autocheckpoint=4000")
    _cache_pragmas(conn)
    # ROUND-7: a DB restored from a VACUUM INTO backup permanently carries
    # journal_mode=delete (the backup file has no WAL) — every connection
    # re-asserts WAL so the restore path cannot silently lose the invariant
    # (idempotent; a already-WAL db is a no-op read)
    try:
        _jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(_jm).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # read-only connections cannot set PRAGMAs — harmless
    current = _schema_version(conn)
    if current is None:
        if not (allow_init or fresh):
            conn.close()
            raise RuntimeError(
                f"{path} is not an ark-watch DB (schema_migrations table missing); "
                "use allow_init=True to initialize a new file."
            )
        # A typo'd path with allow_init=True would silently initialize a
        # parallel DB (data vanishing from the DB the brief reads) — warn
        # loudly when initializing over an existing file
        if not fresh:
            print(
                f"⚠⚠ WARNING: initializing a NEW file at {path} — make sure the path is correct (not a typo?)."
            )
        _apply_migrations(conn)
    elif current < SCHEMA_VERSION:
        if allow_init:
            _apply_migrations(conn, from_version=current)
        else:
            conn.close()
            raise RuntimeError(
                f"DB schema v{current} < code v{SCHEMA_VERSION} — run any CLI "
                "job once (e.g. `python -m arkwatch harvest`) to auto-apply "
                "pending migrations before continuing."
            )
    elif current > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"DB schema v{current} > code v{SCHEMA_VERSION} — code downgrade is not supported."
        )
    return conn


def _schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return None
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else None


def _apply_migrations(conn: sqlite3.Connection, from_version: int = 0) -> None:
    from datetime import datetime

    conn.execute("PRAGMA journal_mode=WAL")  # persistent — once per DB lifetime
    for version in sorted(MIGRATIONS):
        if version <= from_version:
            continue
        # executescript() issues an implicit COMMIT first (stdlib behavior) —
        # hence BEGIN/COMMIT are embedded INSIDE the script so the migration
        # stays atomic.
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        script = (
            "BEGIN IMMEDIATE;\n"
            + MIGRATIONS[version]
            + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{ts}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.DatabaseError:
            # two writers (daemon, okxws, api) can start on the same old version;
            # the loser blocks at BEGIN IMMEDIATE, then trips on the winner's work
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if (_schema_version(conn) or 0) < version:
                raise


def insert_observations(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Append-only batch insert into raw_observations.

    rows: (series_id, ts, value, source[, release_ts, vintage_ts, precision_k])
    — element 5 is release_ts; fetched_at is filled automatically.
    One BEGIN IMMEDIATE per batch. Returns the number of new rows
    (duplicates skipped).
    """
    from datetime import datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    payload = []
    for r in rows:
        series_id, ts, value, source = r[0], r[1], r[2], r[3]
        release_ts = r[4] if len(r) > 4 and r[4] else "na"
        vintage_ts = r[5] if len(r) > 5 and r[5] else "realtime"
        precision_k = r[6] if len(r) > 6 else None
        payload.append((series_id, ts, release_ts, value, vintage_ts, source, precision_k, now))
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO raw_observations"
            "(series_id, ts, release_ts, value, vintage_ts, source, precision_k, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


def apply_realtime_revisions(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """In-place revision handling for revisable sources (FRED, NY Fed research).

    raw_observations is INSERT OR IGNORE (first print wins); for sources that
    revise values in place, realtime must hold the source's LATEST value with
    the previous one snapshotted as a vintage row dated today. Extracted from
    the harvest's FRED branch (2026-09-19) so the NY Fed research datasets —
    HHDC/MCT/LW/GSCPI/HPW rewrite whole histories — share the same contract.

    rows: same shape insert_observations takes — 4-tuples (series_id, ts,
    value, source) or 5-tuples with a trailing release_ts (the harvest's FRED
    path carries realtime_start as element 5; AUDIT 2026-09-20: strict
    4-name unpacking crashed on those — every FRED series would have errored
    at harvest, silently killing the revision contract. Element access, not
    unpack — exactly like the pre-extraction inline block).
    Returns the number of revision snapshots written (0 = nothing changed).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    today = _dt.now(_UTC).date().isoformat()
    now = _dt.now(_UTC).isoformat(timespec="seconds")
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            sid, ts, val, src = r[0], r[1], r[2], r[3]
            cur_v = conn.execute(
                "SELECT value FROM raw_observations WHERE series_id=? "
                "AND ts=? AND source=? AND vintage_ts='realtime'",
                (sid, ts, src),
            ).fetchone()
            if cur_v is not None and abs(cur_v[0] - val) > 1e-12:
                # Preserve the OLD value as a vintage row dated today
                conn.execute(
                    "INSERT OR IGNORE INTO raw_observations"
                    "(series_id,ts,release_ts,value,vintage_ts,source,"
                    "precision_k,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, ts, "na", cur_v[0], today, src, None, now),
                )
                conn.execute(
                    "UPDATE raw_observations SET value=?, fetched_at=? "
                    "WHERE series_id=? AND ts=? AND source=? AND vintage_ts='realtime'",
                    (val, now, sid, ts, src),
                )
                n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n
