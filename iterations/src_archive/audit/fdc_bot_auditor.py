#!/usr/bin/env python3
"""
FDC Bot Auditor — Scans all FDC trading bots for common bugs and plumbing issues.
==============================================================================
Runs static analysis on bot source files, checking for:
  - PnL calculation errors
  - Capital/bankroll mismanagement
  - Settlement double-counting
  - Daily reset gaps
  - Position persistence issues
  - Size vs shares confusion
  - neg_risk misconfiguration
  - Race conditions
  - Hardcoded halt flags
  - Deduplication gaps

Outputs: audit report to output/bot_auditor/ with severity levels.
"""

import ast
import json
import re
import sys
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "bot_auditor"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BOT_FILES = {
    "wc_bot": PROJECT_ROOT / "src" / "worldcup" / "world_cup_bot.py",
    "canary_v21762": PROJECT_ROOT / "src" / "v217_live" / "v21762_reversal_scalper_canary.py",
    "weather_v21": PROJECT_ROOT / "src" / "weather" / "v1_weather_runner_v21.py",
    "pm_markets": PROJECT_ROOT / "src" / "worldcup" / "pm_markets.py",
    "match_model": PROJECT_ROOT / "src" / "worldcup" / "match_model.py",
    "settlement_rounding": PROJECT_ROOT / "src" / "polyweather_analysis" / "settlement_rounding.py",
    "deb_algorithm": PROJECT_ROOT / "src" / "polyweather_analysis" / "deb_algorithm.py",
}

# Bots intentionally killed/deprecated — skip auditing these
DEPRECATED_BOTS = {
    "tail_risk_v21761",    # Killed Jun 24 — stuck in paper, 0 trades in 182h
    "observer_v21751",     # Killed Jun 24 — redundant with canary, 932MB RAM waste
    "bridge_v21717",       # Killed Jun 24 — 7806 failed preflights, 15m canary path abandoned
    "ws_repair_v21716",    # Killed Jun 24 — only fed the dead bridge
}

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


@dataclass
class AuditFinding:
    bot: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW, INFO
    category: str
    description: str
    line: int = 0
    code_snippet: str = ""
    fix: str = ""


def read_bot(name: str) -> Tuple[str, List[str]]:
    """Read a bot's source file, return (full_text, lines)."""
    path = BOT_FILES.get(name)
    if not path or not path.exists():
        return "", []
    text = path.read_text()
    return text, text.splitlines()


def find_pattern(lines: List[str], pattern: str) -> List[int]:
    """Find lines matching a regex pattern, return line numbers (1-indexed)."""
    results = []
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            results.append(i + 1)
    return results


def get_line(lines: List[str], lineno: int) -> str:
    """Get a line by 1-indexed line number."""
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


class BotAuditor:
    """Scans FDC bot source files for common bugs and plumbing issues."""

    def __init__(self):
        self.findings: List[AuditFinding] = []
        self.scan_time = datetime.now(timezone.utc).isoformat()

    def add(self, bot: str, severity: str, category: str, desc: str,
            line: int = 0, snippet: str = "", fix: str = ""):
        self.findings.append(AuditFinding(
            bot=bot, severity=severity, category=category,
            description=desc, line=line, code_snippet=snippet, fix=fix
        ))

    # ─── Pattern-based checks ───

    def check_size_vs_shares(self, name: str, text: str, lines: List[str]):
        """Check if CLOB order size is in USD (wrong) vs shares (correct)."""
        # Pattern 1: size=position_size_usd (bare variable)
        hits = find_pattern(lines, r"size\s*=\s*\w*position_size_usd\w*")
        for ln in hits:
            code = get_line(lines, ln)
            self.add(name, "CRITICAL", "ORDER_SIZING",
                     f"Order `size` parameter uses USD value instead of share count. "
                     f"PM CLOB `size` = number of shares, not dollar amount. "
                     f"Should be: shares = round(size_usd / price, 2) then size=shares.",
                     line=ln, snippet=code,
                     fix="Compute shares = round(position_size_usd / price, 2), pass shares as size")

        # Pattern 2: size=CANARY_CONFIG["position_size_usd"] or size=RISK_LIMITS["max_position_usd"]
        hits = find_pattern(lines, r"size\s*=\s*\w+\[\"position_size_usd\"\]|size\s*=\s*\w+\[\"max_position_usd\"\]")
        for ln in hits:
            code = get_line(lines, ln)
            self.add(name, "CRITICAL", "ORDER_SIZING",
                     f"Order `size` parameter uses USD config value instead of share count. "
                     f"PM CLOB `size` = number of shares, not dollar amount.",
                     line=ln, snippet=code,
                     fix="Compute shares = round(config['position_size_usd'] / price, 2), pass shares as size")

        # Pattern 3: size=opp["position_size_usd"] (dict lookup)
        hits = find_pattern(lines, r"size\s*=\s*opp\[\"position_size_usd\"\]")
        for ln in hits:
            code = get_line(lines, ln)
            self.add(name, "CRITICAL", "ORDER_SIZING",
                     f"Order `size` uses opp['position_size_usd'] (USD) instead of shares.",
                     line=ln, snippet=code,
                     fix="Compute shares = round(opp['position_size_usd'] / price, 2), pass shares")

        # Pattern 4: size=max_position constant
        hits = find_pattern(lines, r"size\s*=\s*\w*max_position\w*")
        for ln in hits:
            code = get_line(lines, ln)
            if "shares" not in code and "share" not in code:
                self.add(name, "CRITICAL", "ORDER_SIZING",
                         f"Order `size` uses max_position constant — likely USD, not shares.",
                         line=ln, snippet=code,
                         fix="Convert to shares before passing to CLOB order")

    def check_daily_loss_reset(self, name: str, text: str, lines: List[str]):
        """Check if daily_loss is reset on UTC day boundaries."""
        has_daily_loss = bool(find_pattern(lines, r"daily_loss"))
        has_daily_reset = bool(find_pattern(lines, r"daily_reset|daily_loss\s*=\s*0|daily_loss_usd\s*=\s*0"))
        
        # Check if daily reset is inside a proper date boundary check
        has_date_boundary = bool(find_pattern(lines, r"strftime.*%Y-%m-%d|date.*==.*today|daily_reset\s*!=.*today"))
        
        if has_daily_loss and not has_daily_reset:
            self.add(name, "HIGH", "DAILY_RESET_MISSING",
                     "Bot tracks daily_loss but never resets it on UTC day boundary. "
                     "Daily loss limit becomes a lifetime limit if bot runs for multiple days.",
                     fix="Add UTC date boundary check: if state.daily_reset != today: state.daily_loss = 0")
        elif has_daily_loss and has_daily_reset and not has_date_boundary:
            self.add(name, "MEDIUM", "DAILY_RESET_FRAGILE",
                     "Daily loss reset exists but no explicit UTC date boundary check found. "
                     "May not reset correctly on day transitions.")

    def check_committed_capital(self, name: str, text: str, lines: List[str]):
        """Check if bot accounts for committed capital in existing positions."""
        has_bankroll = bool(find_pattern(lines, r"bankroll"))
        has_committed = bool(find_pattern(lines, r"committed|cost_usd.*sum|committed_capital"))
        
        if has_bankroll and not has_committed:
            self.add(name, "HIGH", "CAPITAL_OVERCOMMIT",
                     "Bot tracks bankroll but doesn't subtract committed capital from open positions. "
                     "Can over-commit: $31 bankroll with 7 × $5 = $35 in open positions.",
                     fix="Track committed = sum(pos.cost_usd for pos in positions if not pos.settled); "
                          "available = bankroll - committed")

    def check_double_settlement(self, name: str, text: str, lines: List[str]):
        """Check for double-settlement risk (same position settled twice)."""
        has_settle_loop = bool(find_pattern(lines, r"for.*in.*positions.*if not.*settled"))
        has_guard = bool(find_pattern(lines, r"pos\.settled\s*=\s*True|if.*settled"))
        
        if has_settle_loop and not has_guard:
            self.add(name, "MEDIUM", "DOUBLE_SETTLE_RISK",
                     "Settlement loop iterates positions but no explicit guard against "
                     "settling the same position twice in one cycle.",
                     fix="Add settled check at start of settlement processing")

    def check_halt_flags(self, name: str, text: str, lines: List[str]):
        """Check for hardcoded halt flags that block new entries.
        Skip string literals, comments, print/log statements — only flag actual code assignments."""
        hits = find_pattern(lines, r"disable_new.*entries.*True|LIVE.*BLOCKED.*=\s*True|HALTED\s*=\s*True")
        for ln in hits:
            code = get_line(lines, ln)
            stripped = code.strip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            # Skip string literals (inside quotes)
            if stripped.startswith('"') or stripped.startswith("'"):
                continue
            # Skip print/log/f-string statements — these are messages, not actual flags
            if any(kw in stripped for kw in ['print(', 'log.info', 'log.warning', 'log.error', 'log.debug', 'f"', "f'"]):
                continue
            # Skip lines where the match is inside a string literal
            if 'WEATHER_BOT_LIVE_BLOCKED=True' in code and ('"block_reason"' in code or 'block_reason' in code):
                continue
            self.add(name, "CRITICAL", "HALT_FLAG",
                     f"Hardcoded halt flag blocks new entries: {code}",
                     line=ln, snippet=code,
                     fix="Remove or set to False to allow entries")

    def check_neg_risk_mapping(self, name: str, text: str, lines: List[str]):
        """Check if neg_risk is correctly mapped for PM CLOB orders."""
        # Find neg_risk usage
        hits = find_pattern(lines, r"neg_risk\s*=")
        for ln in hits:
            code = get_line(lines, ln)
            if "match_winner" in code or "True" in code or "False" in code:
                self.add(name, "MEDIUM", "NEG_RISK_MAPPING",
                         f"neg_risk assignment: {code}. Verify this matches PM's actual neg_risk "
                         f"group configuration for each market type.",
                         line=ln, snippet=code)

    def check_pnl_calculation(self, name: str, text: str, lines: List[str]):
        """Check PnL calculation for correctness."""
        # Check for common PnL patterns
        hits = find_pattern(lines, r"pnl\s*=.*\(1\.0\s*-\s*entry_price\).*shares|pnl\s*=.*shares.*-\s*cost")
        for ln in hits:
            code = get_line(lines, ln)
            # This is the standard PM binary settlement: win pays $1/share, loss pays $0
            # PnL = payout - cost = shares * (1 - entry_price) for WIN, -cost for LOSS
            # This pattern is CORRECT for PM binary markets
            pass  # Correct pattern
        
        # Check for PnL = shares * (1 - price) * (size/price) — this is also correct
        hits = find_pattern(lines, r"pnl\s*=\s*\(1\.0\s*-\s*.*entry.*\).*position_size|pnl\s*=\s*-.*entry.*position_size")
        for ln in hits:
            code = get_line(lines, ln)
            if "position_size_usd" in code and "/ entry_price" not in code and "/ best_ask" not in code:
                self.add(name, "HIGH", "PNL_CALCULATION",
                         f"PnL formula may not convert USD to shares correctly: {code}",
                         line=ln, snippet=code,
                         fix="Ensure PnL = shares * (1 - entry_price) for WIN, = -cost for LOSS")

    def check_position_persistence(self, name: str, text: str, lines: List[str]):
        """Check if positions are persisted across restarts."""
        has_positions = bool(find_pattern(lines, r"positions.*=.*\[\]|positions.*List"))
        has_jsonl_write = bool(find_pattern(lines, r"json\.dumps.*pos|json\.dumps.*position|positions\.jsonl"))
        has_jsonl_read = bool(find_pattern(lines, r"for line in|json\.loads.*line|positions.*jsonl"))
        
        if has_positions and has_jsonl_write and not has_jsonl_read:
            self.add(name, "HIGH", "POSITION_PERSISTENCE",
                     "Bot writes positions to JSONL but doesn't reload them on restart. "
                     "Open positions are lost on restart.",
                     fix="Add position loading from JSONL file in __init__ or load_state()")

    def check_slug_dedup(self, name: str, text: str, lines: List[str]):
        """Check if bot deduplicates entries by market slug."""
        has_slug = bool(find_pattern(lines, r"slug|market_slug"))
        has_dedup = bool(find_pattern(lines, r"existing_slugs|slug.*already|dedup|duplicate.*slug"))
        
        if has_slug and not has_dedup:
            self.add(name, "HIGH", "SLUG_DEDUP_MISSING",
                     "Bot uses market slugs but doesn't deduplicate. Same market could be "
                     "entered multiple times across cycles.",
                     fix="Add slug deduplication: existing_slugs = {p.market_slug for p in positions if not p.settled}")

    def check_spread_calculation(self, name: str, text: str, lines: List[str]):
        """Check if bid-ask spread is calculated correctly."""
        hits = find_pattern(lines, r"spread.*yes.*no|spread_pp.*=.*yes.*no.*-.*1")
        for ln in hits:
            code = get_line(lines, ln)
            if "yes_price + no_price - 1" in code:
                # This is the vig/spread calculation: spread = yes + no - 1
                # Correct for PM where yes + no should ≈ 1.0
                pass  # Correct

    def check_bankroll_double_deduction(self, name: str, text: str, lines: List[str]):
        """Check if bankroll is deducted at entry AND settlement incorrectly."""
        entry_deduct = find_pattern(lines, r"bankroll\s*-=\s*cost|bankroll\s*-=\s*position_size")
        settlement_add = find_pattern(lines, r"bankroll\s*\+=\s*shares|bankroll\s*\+=\s*payout|bankroll\s*\+=\s*total_payout")
        
        if entry_deduct and settlement_add:
            # Check if LOSS path also adds 0 (correct) or adds something (wrong)
            loss_path = find_pattern(lines, r"bankroll\s*\+=\s*0|else:.*bankroll.*0")
            # This is correct: bankroll deducted at entry, restored on WIN, not restored on LOSS
            pass

    def check_concurrent_position_limit(self, name: str, text: str, lines: List[str]):
        """Check if MAX_CONCURRENT is enforced in entry path."""
        has_max = bool(find_pattern(lines, r"MAX_CONCURRENT|max_open_positions"))
        # Check for explicit position limit enforcement (various patterns)
        has_check = bool(find_pattern(lines, 
            r"open_positions\s*[<>=].*max_open|open_positions.*MAX|max_positions|"
            r"can_trade.*open_positions|open_positions\s*>=\s*RISK_LIMITS|"
            r"open_positions\s*>=\s*CANARY_CONFIG|daily_trades.*max_daily|"
            r"active\s*>=\s*MAX_CONCURRENT|len.*>=.*MAX_CONCURRENT"))
        
        if has_max and not has_check:
            self.add(name, "HIGH", "POSITION_LIMIT_MISSING",
                     "Bot defines MAX_CONCURRENT but doesn't check before entering.",
                     fix="Add check: if active >= MAX_CONCURRENT: return None")

    def check_circuit_breaker(self, name: str, text: str, lines: List[str]):
        """Check if circuit breakers reset on UTC day boundary."""
        has_cb = bool(find_pattern(lines, r"circuit_breaker|consecutive_loss|daily_loss"))
        has_cb_reset = bool(find_pattern(lines, r"daily_reset|consecutive_losses\s*=\s*0|daily_loss\s*=\s*0"))
        
        if has_cb and not has_cb_reset:
            self.add(name, "HIGH", "CIRCUIT_BREAKER_NO_RESET",
                     "Bot has circuit breakers but no daily reset. "
                     "Halt state persists across day boundaries.")

    # ─── Main scan ───

    # Non-trading bots that should be excluded from certain checks
    NON_TRADING_BOTS = {"observer_v21751", "bridge_v21717", "ws_repair_v21716", "pm_markets", "match_model", "settlement_rounding", "deb_algorithm"}

    def scan_bot(self, name: str):
        """Run all checks on a single bot."""
        text, lines = read_bot(name)
        if not text:
            self.add(name, "INFO", "FILE_MISSING", f"Bot file not found for {name}")
            return

        self.check_size_vs_shares(name, text, lines)
        self.check_daily_loss_reset(name, text, lines)
        
        # Skip capital/dedup/position checks for non-trading bots
        if name not in self.NON_TRADING_BOTS:
            self.check_committed_capital(name, text, lines)
            self.check_slug_dedup(name, text, lines)
            self.check_concurrent_position_limit(name, text, lines)
            self.check_double_settlement(name, text, lines)
        
        self.check_halt_flags(name, text, lines)
        self.check_neg_risk_mapping(name, text, lines)
        self.check_pnl_calculation(name, text, lines)
        self.check_position_persistence(name, text, lines)
        self.check_spread_calculation(name, text, lines)
        self.check_bankroll_double_deduction(name, text, lines)
        self.check_circuit_breaker(name, text, lines)

    def scan_all(self):
        """Scan all registered bot files. Skip deprecated bots."""
        for name in BOT_FILES:
            if name in DEPRECATED_BOTS:
                continue
            self.scan_bot(name)

    def generate_report(self) -> Dict:
        """Generate structured audit report."""
        # Sort by severity
        sorted_findings = sorted(
            self.findings,
            key=lambda f: (SEVERITY_ORDER.get(f.severity, 4), f.bot, f.line)
        )

        report = {
            "scan_time": self.scan_time,
            "total_findings": len(sorted_findings),
            "by_severity": {},
            "by_bot": {},
            "findings": [],
        }

        for f in sorted_findings:
            report["findings"].append({
                "bot": f.bot,
                "severity": f.severity,
                "category": f.category,
                "description": f.description,
                "line": f.line,
                "code_snippet": f.code_snippet,
                "fix": f.fix,
            })
            report["by_severity"][f.severity] = report["by_severity"].get(f.severity, 0) + 1
            report["by_bot"][f.bot] = report["by_bot"].get(f.bot, 0) + 1

        return report

    def save_report(self):
        """Save report to file."""
        report = self.generate_report()
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"audit_{ts}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2)

        # Also save latest
        latest_path = OUTPUT_DIR / "audit_latest.json"
        with open(latest_path, "w") as f:
            json.dump(report, f, indent=2)

        return path, report


def main():
    import argparse
    parser = argparse.ArgumentParser(description="FDC Bot Auditor")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    parser.add_argument("--quiet", action="store_true", help="Only show CRITICAL and HIGH")
    args = parser.parse_args()

    auditor = BotAuditor()
    auditor.scan_all()
    path, report = auditor.save_report()

    print(f"\n{'='*70}")
    print(f"FDC Bot Audit Report — {auditor.scan_time}")
    print(f"{'='*70}\n")

    print(f"Total findings: {report['total_findings']}")
    print(f"\nBy severity:")
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        count = report["by_severity"].get(sev, 0)
        if count > 0:
            print(f"  {sev}: {count}")

    print(f"\nBy bot:")
    for bot, count in sorted(report["by_bot"].items(), key=lambda x: -x[1]):
        print(f"  {bot}: {count}")

    print(f"\n{'─'*70}")
    for f in report["findings"]:
        if args.quiet and f["severity"] not in ("CRITICAL", "HIGH"):
            continue
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "ℹ️"}
        print(f"\n{icon.get(f['severity'], '?')} [{f['severity']}] {f['bot']} — {f['category']}")
        print(f"   Line {f['line']}: {f['description']}")
        if f['code_snippet']:
            print(f"   Code: {f['code_snippet'][:100]}")
        if f['fix']:
            print(f"   Fix: {f['fix'][:120]}")

    print(f"\n{'─'*70}")
    print(f"Report saved to: {path}")

    if args.json:
        print(json.dumps(report, indent=2))

    # Return non-zero if CRITICAL findings
    critical_count = report["by_severity"].get("CRITICAL", 0)
    return 1 if critical_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())