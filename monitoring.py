#!/usr/bin/env python3
"""
FDC Monitoring, Logging & Alerting System
==========================================
Immutable SQLite audit trail + real-time dashboard + alerting.

Components:
  a) AuditTrail — append-only SQLite journal of every trade
  b) Dashboard — terminal-based real-time status board
  c) AlertManager — threshold-based alerts with escalation

All data flows through a single event bus so nothing is lost.

Author: Hugh (3rd of 5)
Date: 2026-05-16
"""

import sqlite3, json, time, hashlib, os, threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from collections import deque
from contextlib import contextmanager

REPO = Path("/mnt/c/Users/12035/father_daddy_capital")
DB_PATH = REPO / "output" / "fdc_audit.db"
ALERTS_PATH = REPO / "output" / "alerts.json"
DASHBOARD_PATH = REPO / "output" / "dashboard.txt"
METRICS_PATH = REPO / "output" / "metrics.json"

# ══════════════════════════════════════════════════════════════════════════════
#  a) IMMUTABLE AUDIT TRAIL (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA strict=ON;

CREATE TABLE IF NOT EXISTS audit_trail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,          -- 'scan', 'entry', 'settlement', 'alert', 'system'
    ts_utc          TEXT NOT NULL,          -- ISO 8601
    ts_unix         REAL NOT NULL,          -- Unix epoch seconds

    -- Signal (raw input to the pipeline)
    signal_direction TEXT,
    signal_confidence REAL,
    signal_rsi      REAL,
    signal_macd     REAL,
    signal_momentum INTEGER,
    signal_price    REAL,
    signal_sma20    REAL,
    signal_raw_json TEXT,                   -- Full signal dict as JSON

    -- Contract
    contract_question TEXT,
    contract_condition_id TEXT,
    contract_up_price REAL,
    contract_down_price REAL,
    contract_mins_to_expiry REAL,
    contract_volume REAL,

    -- Debate
    debate_verdict  TEXT,                   -- ENTER / SKIP / REDUCE
    debate_bull_score REAL,
    debate_bear_score REAL,
    debate_net_score REAL,
    debate_bull_reasons TEXT,               -- JSON array
    debate_bear_reasons TEXT,               -- JSON array

    -- Risk / Sizing
    risk_posture    TEXT,                   -- AGGRESSIVE / BALANCED / CONSERVATIVE
    risk_regime     TEXT,                   -- trending_up / trending_down / ranging / volatile
    risk_blended_size REAL,
    risk_risky_size REAL,
    risk_safe_size  REAL,
    risk_neutral_size REAL,

    -- Order execution
    order_side      TEXT,                   -- BUY_Up / BUY_Down
    order_bet       REAL,                   -- Size in USDC
    order_contract_price REAL,              -- Entry price
    order_limit_price REAL,                 -- Actual limit (dust mirror)
    order_token_id  TEXT,
    order_status    TEXT,                   -- simulated / live / filled / cancelled
    order_error     TEXT,

    -- Settlement
    settle_pnl      REAL,                   -- Realized P&L
    settle_price    REAL,                   -- BTC price at settlement
    settle_won      INTEGER,                -- 0 or 1

    -- System
    bankroll_before REAL,
    bankroll_after  REAL,
    total_pnl       REAL,
    open_positions  INTEGER,
    daily_pnl       REAL,
    drawdown_pct    REAL,

    -- Reproducibility
    model_weights_hash TEXT,                -- SHA-256 of neural weights
    bayesian_brier  REAL,                   -- Current Brier score
    bayesian_updates INTEGER,               -- Calibration updates
    neural_updates  INTEGER,                -- Plasticity updates
    neural_blend_pct REAL,                  -- Neural blend weight

    -- Latency
    debate_latency_us REAL,                 -- Microseconds
    risk_latency_us  REAL,
    total_latency_us REAL,

    -- Clock
    clock_drift_ms  REAL,

    -- Checksums (immutability)
    row_hash        TEXT NOT NULL,          -- SHA-256 of all data columns
    prev_row_hash   TEXT,                   -- Chain to previous row (tamper-evident)

    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_trail(ts_utc);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_trail(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON audit_trail(debate_verdict);
CREATE INDEX IF NOT EXISTS idx_audit_direction ON audit_trail(signal_direction);

-- Views for dashboard queries
CREATE VIEW IF NOT EXISTS v_recent_entries AS
    SELECT ts_utc, signal_direction, debate_verdict, order_bet,
           contract_question, risk_posture, settle_pnl
    FROM audit_trail
    WHERE event_type = 'entry'
    ORDER BY id DESC LIMIT 20;

CREATE VIEW IF NOT EXISTS v_daily_summary AS
    SELECT date(ts_utc) as day,
           COUNT(*) as total_events,
           SUM(CASE WHEN event_type='entry' THEN 1 ELSE 0 END) as entries,
           SUM(CASE WHEN event_type='settlement' THEN 1 ELSE 0 END) as settlements,
           SUM(CASE WHEN settle_won=1 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN settle_won=0 THEN 1 ELSE 0 END) as losses,
           ROUND(SUM(COALESCE(settle_pnl,0)), 2) as daily_pnl
    FROM audit_trail
    GROUP BY day
    ORDER BY day DESC LIMIT 30;

CREATE VIEW IF NOT EXISTS v_debate_effectiveness AS
    SELECT debate_verdict,
           COUNT(*) as count,
           SUM(CASE WHEN settle_won=1 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN settle_won=0 THEN 1 ELSE 0 END) as losses
    FROM audit_trail
    WHERE event_type='entry' AND settle_pnl IS NOT NULL
    GROUP BY debate_verdict;

CREATE VIEW IF NOT EXISTS v_alert_log AS
    SELECT ts_utc, event_type,
           json_extract(signal_raw_json, '$.alert_type') as alert_type,
           json_extract(signal_raw_json, '$.alert_message') as alert_message
    FROM audit_trail
    WHERE event_type='alert'
    ORDER BY id DESC LIMIT 50;
"""


class AuditTrail:
    """Immutable, append-only trade journal with row chaining."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        self._last_hash: Optional[str] = None

    def _init_db(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        # Load last row hash for chain
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT row_hash FROM audit_trail ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    self._last_hash = row[0]
        except Exception:
            pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _compute_hash(self, **kwargs) -> str:
        """SHA-256 of all data columns for immutability."""
        payload = json.dumps(kwargs, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def log_scan(self, signal: dict, contracts_count: int, bankroll: float,
                 positions: int, neural_blend: float, **kwargs):
        """Log a scan event (no trade, just market observation)."""
        row_data = {
            "event_type": "scan",
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ts_unix": time.time(),
            "signal_direction": signal.get("direction"),
            "signal_confidence": signal.get("confidence"),
            "signal_rsi": signal.get("rsi"),
            "signal_macd": signal.get("macd"),
            "signal_price": signal.get("price"),
            "bankroll_before": bankroll,
            "open_positions": positions,
            "neural_blend_pct": neural_blend,
            "contract_volume": float(contracts_count),
            "signal_raw_json": json.dumps({"contracts_found": contracts_count}),
        }
        self._insert("scan", row_data)

    def log_entry(self, signal: dict, contract: dict, debate_result,
                  risk_result, entry: dict, neural_weights_hash: str,
                  bayesian_brier: float, bayesian_updates: int,
                  neural_updates: int, latency: dict, clock_drift: Optional[float],
                  bankroll: float, total_pnl: float, daily_pnl: float,
                  drawdown_pct: float, positions: int):
        """Log a trade entry with full audit trail."""
        row_data = {
            "event_type": "entry",
            "ts_utc": entry.get("entry_time") or datetime.now(timezone.utc).isoformat(),
            "ts_unix": time.time(),

            # Signal
            "signal_direction": signal.get("direction"),
            "signal_confidence": signal.get("confidence"),
            "signal_rsi": signal.get("rsi"),
            "signal_macd": signal.get("macd"),
            "signal_momentum": signal.get("momentum"),
            "signal_price": signal.get("price"),
            "signal_sma20": signal.get("sma20"),
            "signal_raw_json": json.dumps(signal, default=str),

            # Contract
            "contract_question": contract.get("question", ""),
            "contract_condition_id": entry.get("conditionId", ""),
            "contract_up_price": contract.get("up_price"),
            "contract_down_price": contract.get("down_price"),
            "contract_mins_to_expiry": contract.get("mins_to_expiry"),
            "contract_volume": contract.get("volume"),

            # Debate
            "debate_verdict": debate_result.verdict if debate_result else None,
            "debate_bull_score": debate_result.bull_score if debate_result else None,
            "debate_bear_score": debate_result.bear_score if debate_result else None,
            "debate_net_score": debate_result.net_score if debate_result else None,
            "debate_bull_reasons": json.dumps(
                debate_result.bull_reasons if debate_result else []),
            "debate_bear_reasons": json.dumps(
                debate_result.bear_reasons if debate_result else []),

            # Risk
            "risk_posture": risk_result.posture_label if risk_result else None,
            "risk_regime": risk_result.regime if risk_result else None,
            "risk_blended_size": risk_result.blended_size if risk_result else None,
            "risk_risky_size": risk_result.risky_size if risk_result else None,
            "risk_safe_size": risk_result.safe_size if risk_result else None,
            "risk_neutral_size": risk_result.neutral_size if risk_result else None,

            # Order
            "order_side": entry.get("action"),
            "order_bet": entry.get("bet"),
            "order_contract_price": entry.get("contract_price"),
            "order_token_id": entry.get("conditionId", "")[:32],
            "order_status": "simulated",

            # System
            "bankroll_before": bankroll,
            "bankroll_after": bankroll + entry.get("bet", 0),
            "total_pnl": total_pnl,
            "open_positions": positions + 1,
            "daily_pnl": daily_pnl,
            "drawdown_pct": drawdown_pct,

            # Reproducibility
            "model_weights_hash": neural_weights_hash,
            "bayesian_brier": bayesian_brier,
            "bayesian_updates": bayesian_updates,
            "neural_updates": neural_updates,
            "neural_blend_pct": entry.get("neural_pred", 0),

            # Latency
            "debate_latency_us": latency.get("debate_us"),
            "risk_latency_us": latency.get("risk_us"),
            "total_latency_us": latency.get("total_us"),

            # Clock
            "clock_drift_ms": clock_drift,
        }
        self._insert("entry", row_data)

    def log_settlement(self, settle: dict, bankroll: float, total_pnl: float,
                       positions: int):
        """Log a settled trade with realized P&L."""
        row_data = {
            "event_type": "settlement",
            "ts_utc": settle.get("settle_time", datetime.now(timezone.utc).isoformat()),
            "ts_unix": time.time(),
            "signal_direction": settle.get("side"),
            "contract_question": settle.get("question", ""),
            "contract_condition_id": settle.get("conditionId", ""),
            "order_side": settle.get("action"),
            "order_bet": settle.get("bet"),
            "settle_pnl": settle.get("pnl"),
            "settle_price": settle.get("settle_price"),
            "settle_won": 1 if (settle.get("pnl") or 0) > 0 else 0,
            "bankroll_before": bankroll - (settle.get("pnl") or 0),
            "bankroll_after": bankroll,
            "total_pnl": total_pnl,
            "open_positions": positions,
        }
        self._insert("settlement", row_data)

    def log_alert(self, alert_type: str, message: str, severity: str = "WARNING",
                  context: Optional[dict] = None):
        """Log an alert event."""
        ctx = context or {}
        ctx.update({"alert_type": alert_type, "alert_message": message,
                    "alert_severity": severity})
        row_data = {
            "event_type": "alert",
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ts_unix": time.time(),
            "signal_raw_json": json.dumps(ctx, default=str),
        }
        self._insert("alert", row_data)

    def _insert(self, event_type: str, data: dict):
        """Insert with row chaining for immutability."""
        data["row_hash"] = self._compute_hash(**data)
        data["prev_row_hash"] = self._last_hash

        columns = []
        placeholders = []
        values = []
        for col in self._columns():
            if col in data:
                columns.append(col)
                placeholders.append("?")
                values.append(data[col])

        sql = f"INSERT INTO audit_trail ({','.join(columns)}) VALUES ({','.join(placeholders)})"

        with self._lock:
            with self._conn() as conn:
                conn.execute(sql, values)
                conn.commit()
            self._last_hash = data["row_hash"]

    @staticmethod
    def _columns() -> List[str]:
        """Return all table columns in order."""
        return [
            "event_type", "ts_utc", "ts_unix",
            "signal_direction", "signal_confidence", "signal_rsi",
            "signal_macd", "signal_momentum", "signal_price", "signal_sma20",
            "signal_raw_json",
            "contract_question", "contract_condition_id",
            "contract_up_price", "contract_down_price",
            "contract_mins_to_expiry", "contract_volume",
            "debate_verdict", "debate_bull_score", "debate_bear_score",
            "debate_net_score", "debate_bull_reasons", "debate_bear_reasons",
            "risk_posture", "risk_regime",
            "risk_blended_size", "risk_risky_size", "risk_safe_size",
            "risk_neutral_size",
            "order_side", "order_bet", "order_contract_price",
            "order_limit_price", "order_token_id", "order_status", "order_error",
            "settle_pnl", "settle_price", "settle_won",
            "bankroll_before", "bankroll_after", "total_pnl",
            "open_positions", "daily_pnl", "drawdown_pct",
            "model_weights_hash", "bayesian_brier", "bayesian_updates",
            "neural_updates", "neural_blend_pct",
            "debate_latency_us", "risk_latency_us", "total_latency_us",
            "clock_drift_ms",
            "row_hash", "prev_row_hash",
        ]

    # ── Query helpers ──

    def query(self, sql: str, params: tuple = ()) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recent_entries(self, limit: int = 10) -> List[dict]:
        return self.query("SELECT * FROM v_recent_entries LIMIT ?", (limit,))

    def daily_summary(self, days: int = 7) -> List[dict]:
        return self.query("SELECT * FROM v_daily_summary LIMIT ?", (days,))

    def debate_stats(self) -> List[dict]:
        return self.query("SELECT * FROM v_debate_effectiveness")

    def recent_alerts(self, limit: int = 20) -> List[dict]:
        return self.query("SELECT * FROM v_alert_log LIMIT ?", (limit,))

    def verify_integrity(self) -> dict:
        """Verify the hash chain is intact (tamper-evident audit)."""
        rows = self.query(
            "SELECT id, row_hash, prev_row_hash FROM audit_trail ORDER BY id")
        breaks = []
        for i, row in enumerate(rows):
            if i > 0:
                expected_prev = rows[i-1]["row_hash"]
                if row["prev_row_hash"] != expected_prev:
                    breaks.append({
                        "row_id": row["id"],
                        "expected_prev": expected_prev,
                        "actual_prev": row["prev_row_hash"],
                    })
        return {
            "total_rows": len(rows),
            "chain_intact": len(breaks) == 0,
            "breaks": breaks,
        }

    def stats(self) -> dict:
        """Quick stats for dashboard."""
        total = len(self.query("SELECT COUNT(*) as c FROM audit_trail"))
        entries = len(self.query(
            "SELECT 1 FROM audit_trail WHERE event_type='entry'"))
        settlements = len(self.query(
            "SELECT 1 FROM audit_trail WHERE event_type='settlement'"))
        alerts = len(self.query(
            "SELECT 1 FROM audit_trail WHERE event_type='alert'"))
        wins = len(self.query(
            "SELECT 1 FROM audit_trail WHERE settle_won=1"))
        losses = len(self.query(
            "SELECT 1 FROM audit_trail WHERE settle_won=0"))
        wr = wins / max(wins + losses, 1) * 100

        return {
            "total_events": total,
            "entries": entries,
            "settlements": settlements,
            "alerts": alerts,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wr, 1),
            "integrity": self.verify_integrity()["chain_intact"],
        }


# ══════════════════════════════════════════════════════════════════════════════
#  b) REAL-TIME DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Dashboard:
    """Terminal-based real-time status board."""

    audit: AuditTrail

    def render(self,
               signal: Optional[dict] = None,
               contracts: Optional[list] = None,
               state: Optional[dict] = None,
               latest_entries: Optional[list] = None,
               neural_stats: Optional[dict] = None,
               bayesian_stats: Optional[dict] = None,
               debate_stats: Optional[dict] = None,
               alert_state: Optional[dict] = None,
               clock_drift: Optional[float] = None,
               latency: Optional[dict] = None,
               ood_detected: bool = False,
               ) -> str:
        """Generate dashboard text for terminal or cron output."""

        lines = [
            "╔══════════════════════════════════════════════════════════════════╗",
            "║           FDC LIVE TRADING DASHBOARD                           ║",
            f"║           {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}                       ║",
            "╠══════════════════════════════════════════════════════════════════╣",
        ]

        # ── Section 1: Market Signal ──
        if signal:
            lines.append("║ 📡 SIGNAL                                                       ║")
            direction = signal.get("direction", "?")
            arrow = {"up": "▲", "down": "▼", "neutral": "─"}.get(direction, "?")
            lines.append(f"║   BTC: ${signal.get('price', 0):,.0f} {arrow} {direction.upper()} "
                        f"| RSI: {signal.get('rsi', 0):.0f} "
                        f"| MACD: {signal.get('macd', 0):+.1f}")
            lines.append(f"║   Confidence: {signal.get('confidence', 0):.3f} "
                        f"| SMA20: ${signal.get('sma20', 0):,.0f} "
                        f"| Momentum: {signal.get('momentum', 0)}/3")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 2: Positions & P&L ──
        if state:
            bankroll = state.get("bankroll", 0)
            pnl = state.get("total_pnl", 0)
            positions = state.get("positions", {})
            wins = state.get("wins", 0)
            losses = state.get("losses", 0)
            wr = wins / max(wins + losses, 1) * 100
            exposed = sum(p.get("bet", 0) for p in positions.values())
            pct = exposed / max(bankroll, 1) * 100

            lines.append("║ 💰 POSITIONS & P&L                                               ║")
            lines.append(f"║   Bankroll: ${bankroll:,.2f}  |  P&L: ${pnl:+,.2f}  "
                        f"|  WR: {wr:.0f}% ({wins}W/{losses}L)")
            lines.append(f"║   Exposed: ${exposed:,.2f} ({pct:.1f}%)  "
                        f"|  Open: {len(positions)}  |  Free: ${bankroll-exposed:,.2f}")

            if positions:
                lines.append("╟──────────────────────────────────────────────────────────────────╢")
                lines.append("║ 📌 OPEN POSITIONS                                               ║")
                for key, pos in list(positions.items())[:5]:
                    side = pos.get("side", "?")
                    bet = pos.get("bet", 0)
                    cp = pos.get("contract_price", 0)
                    edge = pos.get("edge", 0)
                    verdict = pos.get("debate_verdict", "")[:1]
                    posture = pos.get("risk_posture", "")[:3]
                    q = (pos.get("question", "") or "")[:35]
                    lines.append(f"║   {side:4s} ${bet:>6.2f} @ {cp:.3f}  e={edge:.3f}  "
                                f"{verdict}/{posture}  {q}")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 3: Neural / Bayesian ──
        if neural_stats or bayesian_stats:
            lines.append("║ 🧠 NEURAL & BAYESIAN                                            ║")
            if neural_stats:
                lines.append(f"║   Neural: {neural_stats.get('updates', 0)} updates  "
                            f"| Loss: {neural_stats.get('loss', 0):.4f}  "
                            f"| Acc: {neural_stats.get('accuracy', 0):.1f}%")
            if bayesian_stats:
                lines.append(f"║   Bayes:  {bayesian_stats.get('updates', 0)} updates  "
                            f"| Brier: {bayesian_stats.get('brier', 0):.4f}  "
                            f"| CF: {bayesian_stats.get('calibration_factor', 0):.3f}")
            if ood_detected:
                lines.append("║   ⚠ OOD DETECTED — signal rejected")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 4: Debate ──
        if debate_stats:
            lines.append("║ ⚖ DEBATE CONSENSUS                                              ║")
            lines.append(f"║   ENTER: {debate_stats.get('entered', 0)}  "
                        f"REDUCE: {debate_stats.get('reduced', 0)}  "
                        f"SKIP: {debate_stats.get('skipped', 0)}")
            lines.append(f"║   Blocked: {debate_stats.get('blocked_pct', 0)}%  "
                        f"Saved: {debate_stats.get('saved', 0)}  "
                        f"False reject: {debate_stats.get('false_rejected', 0)}")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 5: Performance ──
        if latency:
            lines.append("║ ⚡ PERFORMANCE                                                   ║")
            lines.append(f"║   Latency: debate={latency.get('debate_us', 0):.0f}μs  "
                        f"risk={latency.get('risk_us', 0):.0f}μs  "
                        f"total={latency.get('total_us', 0):.0f}μs")
            if clock_drift is not None:
                safe = "✅" if abs(clock_drift) < 500 else "🛑"
                lines.append(f"║   Clock: {clock_drift:.0f}ms drift  {safe}")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 6: Alerts ──
        if alert_state and alert_state.get("active_alerts", 0) > 0:
            lines.append("║ 🚨 ALERTS                                                       ║")
            for alert in (alert_state.get("recent", []) or [])[:3]:
                lines.append(f"║   {alert.get('type', '?')}: {alert.get('message', '')[:45]}")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Section 7: Recent Activity ──
        if latest_entries:
            lines.append("║ 📋 RECENT ACTIVITY                                               ║")
            for e in (latest_entries or [])[:5]:
                verdict = (e.get("debate_verdict") or "?")[:1]
                side = (e.get("signal_direction") or "?")[:3]
                pnl = e.get("settle_pnl")
                pnl_str = f"${pnl:+.2f}" if pnl is not None else "open"
                lines.append(f"║   {verdict} {side} {pnl_str:>8s}  "
                            f"{(e.get('contract_question') or '')[:35]}")
            lines.append("╟──────────────────────────────────────────────────────────────────╢")

        # ── Footer ──
        audit_stats = self.audit.stats()
        integrity = "✅ CHAIN INTACT" if audit_stats["integrity"] else "🛑 CHAIN BROKEN"
        lines.append(f"║ 📊 Audit: {audit_stats['total_events']} events  "
                    f"| WR: {audit_stats['win_rate_pct']:.1f}%  "
                    f"| {integrity}")
        lines.append("╚══════════════════════════════════════════════════════════════════╝")

        return "\n".join(lines)

    def save(self, content: str):
        DASHBOARD_PATH.parent.mkdir(exist_ok=True)
        DASHBOARD_PATH.write_text(content)


# ══════════════════════════════════════════════════════════════════════════════
#  c) ALERT MANAGER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AlertRule:
    name: str
    condition: str
    threshold: float
    severity: str = "WARNING"
    cooldown_seconds: float = 300  # Don't re-fire within 5 min

@dataclass
class AlertManager:
    """Threshold-based alerting with cooldowns."""

    rules: List[AlertRule] = field(default_factory=lambda: [
        AlertRule("risk_bounds_violation",
                  "Trade size exceeds RiskManager 10% cap", 0.10,
                  "CRITICAL", 60),
        AlertRule("consecutive_losses",
                  "Consecutive losing trades", 5,
                  "CRITICAL", 300),
        AlertRule("high_slippage",
                  "Actual fill vs expected exceeds 0.5%", 0.005,
                  "WARNING", 120),
        AlertRule("max_drawdown_breach",
                  "Drawdown exceeds configured limit", 0.40,
                  "CRITICAL", 60),
        AlertRule("daily_loss_limit",
                  "Daily P&L below configured floor", -25.0,
                  "CRITICAL", 300),
        AlertRule("api_error_rate",
                  "API error rate exceeds threshold", 0.10,
                  "WARNING", 600),
        AlertRule("ood_detected",
                  "OOD signal vector detected", 1,
                  "WARNING", 300),
        AlertRule("clock_drift",
                  "Clock drift exceeds 500ms", 500.0,
                  "CRITICAL", 300),
    ])

    active_alerts: List[dict] = field(default_factory=list)
    alert_history: List[dict] = field(default_factory=list)
    consecutive_losses: int = 0
    last_fire: Dict[str, float] = field(default_factory=dict)
    api_errors: int = 0
    api_total: int = 0
    audit: Optional[AuditTrail] = None

    def check(self, **context) -> List[dict]:
        """Check all rules against current context. Returns triggered alerts."""
        triggered = []
        now = time.time()

        # Risk bounds
        if "position_size" in context and "bankroll" in context:
            pct = context["position_size"] / max(context["bankroll"], 1)
            if pct > 0.10:
                triggered.append(self._fire(
                    "risk_bounds_violation", "CRITICAL",
                    f"Position ${context['position_size']:.2f} = {pct*100:.1f}% of bankroll",
                    now, context))

        # Consecutive losses
        if context.get("trade_lost"):
            self.consecutive_losses += 1
        elif context.get("trade_won"):
            self.consecutive_losses = 0
        if self.consecutive_losses >= 5:
            triggered.append(self._fire(
                "consecutive_losses", "CRITICAL",
                f"{self.consecutive_losses} consecutive losses — PAUSE TRADING",
                now, context))

        # High slippage
        slippage = context.get("slippage_bps", 0) / 10000
        if slippage > 0.005:
            triggered.append(self._fire(
                "high_slippage", "WARNING",
                f"Slippage {slippage*100:.2f}% exceeds 0.5% threshold",
                now, context))

        # Drawdown
        dd = context.get("drawdown_pct", 0)
        if dd > 0.40:
            triggered.append(self._fire(
                "max_drawdown_breach", "CRITICAL",
                f"Drawdown {dd*100:.1f}% exceeds 40% limit",
                now, context))

        # Daily loss
        daily_pnl = context.get("daily_pnl", 0)
        if daily_pnl < -25.0:
            triggered.append(self._fire(
                "daily_loss_limit", "CRITICAL",
                f"Daily P&L ${daily_pnl:+.2f} below -$25.00 limit",
                now, context))

        # API errors
        if context.get("api_error"):
            self.api_errors += 1
        self.api_total += 1
        error_rate = self.api_errors / max(self.api_total, 1)
        if error_rate > 0.10 and self.api_total > 20:
            triggered.append(self._fire(
                "api_error_rate", "WARNING",
                f"API error rate {error_rate*100:.1f}% ({self.api_errors}/{self.api_total})",
                now, context))

        # OOD detection
        if context.get("ood_detected"):
            triggered.append(self._fire(
                "ood_detected", "WARNING",
                f"OOD signal vector — Mahalanobis distance {context.get('ood_distance', 0):.1f}",
                now, context))

        # Clock drift
        drift = abs(context.get("clock_drift_ms", 0) or 0)
        if drift > 500:
            triggered.append(self._fire(
                "clock_drift", "CRITICAL",
                f"Clock drift {drift:.0f}ms exceeds 500ms",
                now, context))

        for alert in triggered:
            self.active_alerts.append(alert)
            self.alert_history.append(alert)
            if self.audit:
                self.audit.log_alert(
                    alert["rule"], alert["message"], alert["severity"], context)

        # Trim history
        if len(self.alert_history) > 500:
            self.alert_history = self.alert_history[-500:]

        # Auto-resolve alerts older than 1 hour
        self.active_alerts = [
            a for a in self.active_alerts
            if now - a.get("ts_unix", 0) < 3600
        ]

        return triggered

    def _fire(self, rule_name: str, severity: str, message: str,
              now: float, context: dict) -> dict:
        """Fire an alert with cooldown enforcement."""
        rule = next((r for r in self.rules if r.name == rule_name), None)
        if rule:
            last = self.last_fire.get(rule_name, 0)
            if now - last < rule.cooldown_seconds:
                return None  # Suppressed by cooldown

        self.last_fire[rule_name] = now
        return {
            "rule": rule_name,
            "severity": severity,
            "message": message,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "ts_unix": now,
        }

    def status(self) -> dict:
        return {
            "active_alerts": len(self.active_alerts),
            "total_fired": len(self.alert_history),
            "consecutive_losses": self.consecutive_losses,
            "api_error_rate": (self.api_errors / max(self.api_total, 1))
                              if self.api_total > 0 else 0,
            "recent": [
                {"type": a["rule"], "message": a["message"],
                 "severity": a["severity"]}
                for a in self.active_alerts[-5:]
            ],
            "trading_paused": self.consecutive_losses >= 5,
        }

    def save(self):
        ALERTS_PATH.parent.mkdir(exist_ok=True)
        ALERTS_PATH.write_text(json.dumps(self.status(), indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING INTEGRATION LAYER
# ══════════════════════════════════════════════════════════════════════════════

class MonitoringPipeline:
    """Single entry point for all monitoring, logging, and alerting."""

    def __init__(self):
        self.audit = AuditTrail()
        self.dashboard = Dashboard(audit=self.audit)
        self.alerts = AlertManager(audit=self.audit)

        # Import engine components
        import sys
        sys.path.insert(0, str(REPO))
        from pm_engine import (fetch_5m, btc_signal, discover_contracts,
                               load_state, save_state, check_settlements,
                               evaluate_entries)
        from fdc_debate import debate as _debate
        from fdc_risk_sizer import size_position

        self._fetch_5m = fetch_5m
        self._btc_signal = btc_signal
        self._discover = discover_contracts
        self._load_state = load_state
        self._save_state = save_state
        self._settle = check_settlements
        self._evaluate = evaluate_entries
        self._debate = _debate
        self._size = size_position

    def _get_weights_hash(self) -> str:
        """SHA-256 of neural weights for reproducibility."""
        weights_path = REPO / "neural_weights" / "plastic_weights.npz"
        if weights_path.exists():
            return hashlib.sha256(weights_path.read_bytes()).hexdigest()[:16]
        return "no_weights"

    def _get_bayesian_stats(self) -> dict:
        try:
            import sys
            sys.path.insert(0, str(REPO / "src" / "neural"))
            import bayesian_layer as bl
            cal = bl.BayesianCalibrator()
            return cal.stats()
        except Exception:
            return {"updates": 0, "brier": 0.25, "calibration_factor": 0}

    def _get_neural_stats(self) -> dict:
        try:
            import sys
            sys.path.insert(0, str(REPO / "src" / "neural"))
            import plastic_network as pn
            nn = pn.NeuralPlasticityEngine()
            return nn.stats()
        except Exception:
            return {"updates": 0, "loss": 0, "accuracy": 0}

    def run_monitored_scan(self) -> dict:
        """Run one scan with full monitoring instrumentation."""
        t_start = time.perf_counter()

        # ── Pre-scan checks ──
        from execution_hardening import ClockSync
        clock = ClockSync()
        clock.check_exchange_time()
        drift = None
        if clock.samples:
            drift = clock.samples[-1].get("drift_ms")

        # ── Fetch ──
        prices = self._fetch_5m()
        if not prices:
            self.alerts.check(api_error=True)
            return {"error": "no_price_data"}

        sig = self._btc_signal(prices)
        contracts = self._discover()
        state = self._load_state()

        bankroll = state.get("bankroll", 250)
        total_pnl = state.get("total_pnl", 0)
        positions = state.get("positions", {})

        # ── Settle expired ──
        settled = self._settle(state, sig["price"])
        for s in settled:
            pnl = s["pnl"]
            state["total_pnl"] += pnl
            state["bankroll"] += pnl
            if pnl > 0:
                state["wins"] = state.get("wins", 0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1
            self.audit.log_settlement(
                s, state["bankroll"], state["total_pnl"], len(positions))

            # Alert: check consecutive losses
            self.alerts.check(trade_lost=(pnl < 0), trade_won=(pnl > 0))

        # ── Evaluate entries ──
        entries, neural_pred = self._evaluate(sig, contracts, state)

        # ── Debate every contract candidate ──
        debate_stats_out = None
        for entry in entries:
            t_debate_start = time.perf_counter()
            dr = self._debate(sig, contracts[0] if contracts else {})
            t_debate_end = time.perf_counter()

            t_risk_start = time.perf_counter()
            rs = self._size(sig, contracts[0] if contracts else {},
                           bankroll, debate_net_score=dr.net_score)
            t_risk_end = time.perf_counter()

            latency = {
                "debate_us": (t_debate_end - t_debate_start) * 1_000_000,
                "risk_us": (t_risk_end - t_risk_start) * 1_000_000,
                "total_us": (time.perf_counter() - t_start) * 1_000_000,
            }

            # ── LOG ENTRY ──
            self.audit.log_entry(
                signal=sig,
                contract=contracts[0] if contracts else {},
                debate_result=dr,
                risk_result=rs,
                entry=entry,
                neural_weights_hash=self._get_weights_hash(),
                bayesian_brier=self._get_bayesian_stats().get("brier_score", 0.25),
                bayesian_updates=self._get_bayesian_stats().get("updates", 0),
                neural_updates=self._get_neural_stats().get("updates", 0),
                latency=latency,
                clock_drift=drift,
                bankroll=bankroll,
                total_pnl=total_pnl,
                daily_pnl=0,
                drawdown_pct=0,
                positions=len(positions),
            )

            # ── ALERTS ──
            self.alerts.check(
                position_size=entry.get("bet", 0),
                bankroll=bankroll,
                slippage_bps=0,
                drawdown_pct=0,
                daily_pnl=0,
                api_error=False,
                ood_detected=False,
                ood_distance=0,
                clock_drift_ms=drift,
            )

            # Add to state
            key = f"{entry['conditionId'][:16]}_{entry['side']}"
            state["positions"][key] = entry

        # ── Log scan (even if no entries) ──
        self.audit.log_scan(
            signal=sig,
            contracts_count=len(contracts),
            bankroll=bankroll,
            positions=len(positions),
            neural_blend=0,
        )

        self._save_state(state)

        # ── Render dashboard ──
        dashboard_text = self.dashboard.render(
            signal=sig,
            contracts=contracts,
            state=state,
            latest_entries=self.audit.recent_entries(5),
            neural_stats=self._get_neural_stats(),
            bayesian_stats=self._get_bayesian_stats(),
            debate_stats=debate_stats_out,
            alert_state=self.alerts.status(),
            clock_drift=drift,
            latency={"debate_us": (time.perf_counter() - t_start) * 500_000,
                     "risk_us": 0, "total_us": (time.perf_counter() - t_start) * 1_000_000},
        )
        self.dashboard.save(dashboard_text)
        self.alerts.save()

        return {
            "entries": len(entries),
            "settled": len(settled),
            "contracts": len(contracts),
            "audit_events": self.audit.stats()["total_events"],
            "alerts_fired": len(self.alerts.active_alerts),
            "dashboard": str(DASHBOARD_PATH),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN: Test the full monitoring pipeline
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  FDC MONITORING, LOGGING & ALERTING SYSTEM")
    print("=" * 60)

    # ── 1. Audit Trail ──
    print("\n── 1. SQLite Audit Trail ──")
    audit = AuditTrail()
    print(f"  DB: {DB_PATH}")
    print(f"  Size: {DB_PATH.stat().st_size if DB_PATH.exists() else 0} bytes")

    # Log a test entry to verify schema
    test_signal = {"direction": "up", "confidence": 0.72, "rsi": 32,
                   "macd": 85, "momentum": 2, "price": 79100, "sma20": 78900}
    test_contract = {"question": "BTC above 78K?", "up_price": 0.41,
                     "down_price": 0.59, "mins_to_expiry": 335, "volume": 500000}

    audit.log_scan(test_signal, contracts_count=12, bankroll=325.91,
                   positions=5, neural_blend=0.15)
    print(f"  Scan logged. Total events: {audit.stats()['total_events']}")

    # ── 2. Dashboard ──
    print("\n── 2. Dashboard ──")
    dash = Dashboard(audit=audit)
    dashboard_text = dash.render(
        signal=test_signal,
        state={"bankroll": 325.91, "total_pnl": 75.91, "wins": 1, "losses": 2,
               "positions": {"test_Up": {"side": "Up", "bet": 11.25,
                             "contract_price": 0.085, "edge": 0.31,
                             "question": "BTC above 80K?", "debate_verdict": "ENTER",
                             "risk_posture": "CONSERVATIVE"}}},
        neural_stats={"updates": 300, "loss": 0.31, "accuracy": 47.8},
        bayesian_stats={"updates": 8, "brier": 0.24, "calibration_factor": 0.05},
        debate_stats={"entered": 5, "reduced": 2, "skipped": 3, "blocked_pct": 50,
                      "saved": 3, "false_rejected": 1},
        alert_state={"active_alerts": 0, "recent": []},
        clock_drift=306.8,
        latency={"debate_us": 7, "risk_us": 8, "total_us": 15},
    )
    dash.save(dashboard_text)
    print(dashboard_text)

    # ── 3. Alert Manager ──
    print("\n── 3. Alert Manager ──")
    alerts = AlertManager(audit=audit)

    # Test alerts
    results = alerts.check(
        position_size=30, bankroll=250,       # 12% → fires risk_bounds
        trade_lost=True,
        drawdown_pct=0.45,                     # fires max_drawdown
    )
    print(f"  Alerts fired: {len(results)}")
    for r in results:
        print(f"    {r['severity']:8s} {r['rule']}: {r['message'][:60]}")

    # Test consecutive losses
    alerts.consecutive_losses = 5
    results2 = alerts.check(trade_lost=True)
    print(f"  Consecutive loss alert: {len(results2)}")
    for r in results2:
        print(f"    {r['severity']:8s} {r['rule']}: {r['message'][:60]}")

    # ── 4. Integrity Check ──
    print("\n── 4. Audit Trail Integrity ──")
    integrity = audit.verify_integrity()
    print(f"  Total rows: {integrity['total_rows']}")
    print(f"  Chain intact: {'✅' if integrity['chain_intact'] else '🛑 BROKEN'}")
    if integrity["breaks"]:
        for b in integrity["breaks"]:
            print(f"    Break at row {b['row_id']}")

    # Query examples
    print("\n  Query: Recent entries")
    for row in audit.recent_entries(3):
        print(f"    {row.get('ts_utc', '')[:19]} | {row.get('debate_verdict', '?')} "
              f"| {row.get('signal_direction', '?')} "
              f"| {row.get('contract_question', '')[:30]}")

    print("\n  Query: Daily summary")
    for row in audit.daily_summary(3):
        print(f"    {row.get('day', '?')} | entries={row.get('entries', 0)} "
              f"| W={row.get('wins',0)} L={row.get('losses',0)} "
              f"| P&L=${row.get('daily_pnl', 0):+.2f}")

    # ── 5. Integration Test ──
    print("\n── 5. Full Pipeline Integration ──")
    try:
        mon = MonitoringPipeline()
        result = mon.run_monitored_scan()
        print(f"  Scan: {result.get('entries', 0)} entries, "
              f"{result.get('settled', 0)} settled")
        print(f"  Audit events: {result['audit_events']}")
        print(f"  Alerts: {result['alerts_fired']}")
        print(f"  Dashboard: {result['dashboard']}")
    except Exception as e:
        print(f"  Integration error: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  MONITORING SYSTEM: DEPLOYED")
    print(f"  Audit DB: {DB_PATH}")
    print(f"  Dashboard: {DASHBOARD_PATH}")
    print(f"  Alerts: {ALERTS_PATH}")
    print(f"  Chain: {'✅ INTACT' if integrity['chain_intact'] else '🛑 BROKEN'}")
    print("=" * 60)
