#!/usr/bin/env python3
"""
FDC Bug Scanner Bot
===================
Scans all trading bot source files for bugs, race conditions, state issues,
logic errors, and common Python pitfalls. Auto-fixes safe issues and
reports complex ones.

Scans:
  - src/weather/*.py (weather bot family)
  - src/v217_live/*.py (scalper/canary family)
  - Any other Python files in src/ that contain trading logic

Checks:
  1. Race conditions (shared mutable state without locks)
  2. State persistence bugs (save after modify, not before)
  3. Integer/float overflow or underflow
  4. Unbound variables after try/except
  5. Resource leaks (unclosed files/connections)
  6. JSONL append-without-read-back (settlement persistence)
  7. Circuit breaker logic errors (reset ordering)
  8. Division by zero (position size, PnL calculations)
  9. Missing max(0, ...) guards on position counts
  10. Bare except clauses hiding real errors
  11. Time/timezone bugs (UTC vs local)
  12. Duplicate process detection
  13. Hardcoded paths that break on different hosts
  14. API call timeout missing
  15. Infinite retry loops without backoff

Usage:
  python3 src/audit/bug_scanner.py          # scan only
  python3 src/audit/bug_scanner.py --fix     # scan + auto-fix safe issues
  python3 src/audit/bug_scanner.py --monitor  # continuous mode (every 15min)
"""
from __future__ import annotations
import ast, sys, os, json, re, time, traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "output" / "bug_scanner"
OUT.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# BUG PATTERN DEFINITIONS
# ═══════════════════════════════════════════════════════════════

@dataclass
class BugFinding:
    file: str
    line: int
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    category: str
    description: str
    code_snippet: str
    auto_fixable: bool = False
    fix_applied: bool = False

SCAN_TARGETS = [
    "src/weather/v1_weather_runner_v21.py",
    "src/weather/v1_weather_runner_v2.py",
    "src/weather/v1_weather_runner.py",
    "src/weather/v2_3_rain_shadow_cell.py",
    "src/weather/deb_hindcast.py",
    "src/weather/fdeb_integration.py",
    "src/v217_live/v21762_reversal_scalper_canary.py",
    "src/v217_live/scalper_reversal_cell.py",
    "src/v217_live/scalper_paper_live_simulator.py",
    "src/v217_live/multi_market_scanner.py",
    "src/v217_live/persistent_clob_client.py",
    "src/v217_live/live_quote_cache.py",
]

# ═══════════════════════════════════════════════════════════════
# SCANNERS
# ═══════════════════════════════════════════════════════════════

class BugScanner:
    def __init__(self, root: Path):
        self.root = root
        self.findings: List[BugFinding] = []

    def scan_file(self, rel_path: str) -> List[BugFinding]:
        abs_path = self.root / rel_path
        if not abs_path.exists():
            return []
        try:
            source = abs_path.read_text()
            tree = ast.parse(source, filename=str(abs_path))
        except SyntaxError as e:
            self.findings.append(BugFinding(
                file=rel_path, line=e.lineno or 0, severity="CRITICAL",
                category="syntax_error", description=f"Syntax error: {e.msg}",
                code_snippet=str(e)[:100]
            ))
            return self.findings

        lines = source.splitlines()
        findings = []

        # Run all scanners
        findings += self._scan_bare_except(rel_path, tree, lines)
        findings += self._scan_division_by_zero(rel_path, tree, lines)
        findings += self._scan_unbound_after_except(rel_path, tree, lines)
        findings += self._scan_resource_leaks(rel_path, tree, lines)
        findings += self._scan_state_persistence_ordering(rel_path, tree, lines)
        findings += self._scan_position_underflow(rel_path, tree, lines)
        findings += self._scan_missing_timeouts(rel_path, tree, lines)
        findings += self._scan_hardcoded_paths(rel_path, tree, lines)
        findings += self._scan_circuit_breaker_logic(rel_path, tree, lines)
        findings += self._scan_jsonl_persistence(rel_path, tree, lines)
        findings += self._scan_timezone_issues(rel_path, tree, lines)
        findings += self._scan_infinite_loops(rel_path, tree, lines)
        findings += self._scan_mutable_defaults(rel_path, tree, lines)
        findings += self._scan_f_string_in_logging(rel_path, tree, lines)
        findings += self._scan_race_conditions(rel_path, tree, lines)

        self.findings.extend(findings)
        return findings

    def _get_source_segment(self, lines, lineno, context=2):
        start = max(0, lineno - 1 - context)
        end = min(len(lines), lineno + context)
        return "\n".join(f"{i+1}|{lines[i]}" for i in range(start, end))

    def _scan_bare_except(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    seg = self._get_source_segment(lines, node.lineno)
                    findings.append(BugFinding(
                        file=filepath, line=node.lineno, severity="MEDIUM",
                        category="bare_except",
                        description="Bare except: catches SystemExit/KeyboardInterrupt, hides real errors",
                        code_snippet=seg, auto_fixable=True
                    ))
        return findings

    def _scan_division_by_zero(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if isinstance(node.right, ast.Constant) and node.right.value == 0:
                    seg = self._get_source_segment(lines, node.lineno)
                    findings.append(BugFinding(
                        file=filepath, line=node.lineno, severity="HIGH",
                        category="division_by_zero",
                        description="Literal division by zero",
                        code_snippet=seg
                    ))
                # Check for division by variable that might be zero
                if isinstance(node.right, ast.Name):
                    # Look for patterns like / len(x) or / total without guard
                    parent_lines = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                    if "len(" in parent_lines or "count" in parent_lines.lower():
                        seg = self._get_source_segment(lines, node.lineno)
                        # Check if there's a guard nearby
                        has_guard = False
                        for i in range(max(0, node.lineno - 3), min(len(lines), node.lineno + 1)):
                            if any(g in lines[i].lower() for g in ["if ", "> 0", "!= 0", "if not"]):
                                has_guard = True
                                break
                        if not has_guard:
                            findings.append(BugFinding(
                                file=filepath, line=node.lineno, severity="MEDIUM",
                                category="unguarded_division",
                                description=f"Division by '{node.right.id}' without zero guard nearby",
                                code_snippet=seg
                            ))
        return findings

    def _scan_unbound_after_except(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if handler.type is None or (isinstance(handler.type, ast.Name)):
                        # Check if variables defined in try block are used after except
                        for body_node in ast.walk(node):
                            if isinstance(body_node, ast.Assign) and body_node.lineno < handler.lineno:
                                for target in body_node.targets:
                                    if isinstance(target, ast.Name):
                                        var_name = target.id
                                        # Check if used in handler or finally
                                        for handler_node in ast.walk(handler):
                                            if isinstance(handler_node, ast.Name) and handler_node.id == var_name:
                                                # Check if there's a pass or continue in except
                                                has_pass = any(
                                                    isinstance(n, ast.Pass) for n in ast.walk(handler)
                                                )
                                                if has_pass:
                                                    seg = self._get_source_segment(lines, handler.lineno)
                                                    findings.append(BugFinding(
                                                        file=filepath, line=handler.lineno, severity="HIGH",
                                                        category="unbound_after_except",
                                                        description=f"Variable '{var_name}' may be unbound if exception raised before assignment (except has pass)",
                                                        code_snippet=seg, auto_fixable=True
                                                    ))
        return findings

    def _scan_resource_leaks(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name == "open" and not any(
                    isinstance(parent, ast.withitem) for parent in ast.walk(tree)
                    if hasattr(parent, 'context_expr') and parent.context_expr is node
                ):
                    # Check if it's in a with statement
                    in_with = False
                    for wnode in ast.walk(tree):
                        if isinstance(wnode, ast.With):
                            for item in wnode.items:
                                if item.context_expr is node:
                                    in_with = True
                    if not in_with:
                        seg = self._get_source_segment(lines, node.lineno)
                        findings.append(BugFinding(
                            file=filepath, line=node.lineno, severity="MEDIUM",
                            category="resource_leak",
                            description="open() without with statement — file handle may leak on exception",
                            code_snippet=seg
                        ))

                if func_name in ("urlopen", "request"):
                    # Check for timeout parameter
                    has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                    if not has_timeout:
                        seg = self._get_source_segment(lines, node.lineno)
                        findings.append(BugFinding(
                            file=filepath, line=node.lineno, severity="MEDIUM",
                            category="missing_timeout",
                            description=f"{func_name}() without timeout — can hang indefinitely",
                            code_snippet=seg
                        ))
        return findings

    def _scan_state_persistence_ordering(self, filepath, tree, lines):
        """Check that save_state() is called BEFORE removing items from positions list."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef,)):
                body_lines = [n for n in ast.walk(node)]
                save_calls = [n for n in body_lines if isinstance(n, ast.Call)
                              and isinstance(n.func, ast.Attribute) and n.func.attr == "save_state"]
                filter_calls = [n for n in body_lines if isinstance(n, ast.Assign)
                                and any(isinstance(t, ast.Subscript) for t in n.targets)]

                for save in save_calls:
                    for filt in filter_calls:
                        if filt.lineno > save.lineno and filt.lineno < save.lineno + 10:
                            # save_state called BEFORE list filter — but check if there's another save after
                            has_later_save = any(s.lineno > filt.lineno for s in save_calls)
                            if not has_later_save:
                                # Check if the filter removes settled items
                                source = self._get_source_segment(lines, filt.lineno, 3)
                                if "settled" in source.lower():
                                    findings.append(BugFinding(
                                        file=filepath, line=save.lineno, severity="HIGH",
                                        category="state_persistence_ordering",
                                        description="save_state() called BEFORE removing settled items from list — settlements won't persist to JSONL",
                                        code_snippet=source, auto_fixable=True
                                    ))
        return findings

    def _scan_position_underflow(self, filepath, tree, lines):
        """Check for active_positions -= 1 without max(0, ...) guard."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Sub):
                if isinstance(node.target, ast.Attribute):
                    target_name = node.target.attr
                    if "position" in target_name.lower() or "active" in target_name.lower():
                        # Check if max(0, ...) guard exists nearby
                        source = self._get_source_segment(lines, node.lineno, 3)
                        if "max(0" not in source:
                            findings.append(BugFinding(
                                file=filepath, line=node.lineno, severity="HIGH",
                                category="position_underflow",
                                description=f"{target_name} -= 1 without max(0, ...) guard — can go negative",
                                code_snippet=source, auto_fixable=True
                            ))
        return findings

    def _scan_missing_timeouts(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name in ("get", "post", "request", "urlopen"):
                    has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                    if not has_timeout and func_name == "urlopen":
                        # Already caught by resource_leak scanner
                        pass
        return findings

    def _scan_hardcoded_paths(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "/home/naq1987s" in node.value:
                    seg = self._get_source_segment(lines, node.lineno)
                    findings.append(BugFinding(
                        file=filepath, line=node.lineno, severity="LOW",
                        category="hardcoded_path",
                        description=f"Hardcoded path: {node.value[:60]}",
                        code_snippet=seg
                    ))
        return findings

    def _scan_circuit_breaker_logic(self, filepath, tree, lines):
        """Check for circuit breaker reset ordering issues."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and "circuit" in node.name.lower():
                source = self._get_source_segment(lines, node.lineno, 30)
                # Check if halt check comes before reset
                halt_check = "halted" in source.lower() or "halt" in source.lower()
                reset_check = "weekly_loss = 0" in source or "daily_loss = 0" in source
                if halt_check and reset_check:
                    # Check ordering — reset should come before halt check
                    for child in ast.walk(node):
                        if isinstance(child, ast.If):
                            test_src = self._get_source_segment(lines, child.lineno, 1)
                            if "halted" in test_src.lower():
                                # Check if weekly_loss reset is after this
                                for child2 in ast.walk(node):
                                    if isinstance(child2, ast.Assign):
                                        if child2.lineno > child.lineno:
                                            for t in child2.targets:
                                                if isinstance(t, ast.Attribute) and "loss" in t.attr.lower():
                                                    findings.append(BugFinding(
                                                        file=filepath, line=child.lineno, severity="MEDIUM",
                                                        category="circuit_breaker_ordering",
                                                        description="Circuit breaker halt check before reset — stale halt may persist",
                                                        code_snippet=self._get_source_segment(lines, child.lineno, 5)
                                                    ))
                                                    break
        return findings

    def _scan_jsonl_persistence(self, filepath, tree, lines):
        """Check if JSONL files are written with append mode but never read back to update settled status."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value == "a" and ".jsonl" in str(node.args):
                            # Found append-mode JSONL write
                            # Check if there's a corresponding read+update pattern
                            has_read_update = False
                            for node2 in ast.walk(tree):
                                if isinstance(node2, ast.Call) and isinstance(node2.func, ast.Name) and node2.func.id == "open":
                                    for arg2 in node2.args:
                                        if isinstance(arg2, ast.Constant) and arg2.value == "w":
                                            has_read_update = True
                            if not has_read_update:
                                seg = self._get_source_segment(lines, node.lineno)
                                findings.append(BugFinding(
                                    file=filepath, line=node.lineno, severity="HIGH",
                                    category="jsonl_no_persist",
                                    description="JSONL append-only — settled positions never persisted back, causing re-settlement on restart",
                                    code_snippet=seg, auto_fixable=False
                                ))
        return findings

    def _scan_timezone_issues(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name == "now" and not node.args:
                    # datetime.now() without tz — creates naive datetime
                    seg = self._get_source_segment(lines, node.lineno)
                    findings.append(BugFinding(
                        file=filepath, line=node.lineno, severity="MEDIUM",
                        category="naive_datetime",
                        description="datetime.now() without timezone — naive datetime, UTC mismatch risk",
                        code_snippet=seg
                    ))
                if func_name == "fromtimestamp" and not any(kw.arg == "tz" for kw in node.keywords):
                    seg = self._get_source_segment(lines, node.lineno)
                    findings.append(BugFinding(
                        file=filepath, line=node.lineno, severity="LOW",
                        category="naive_timestamp",
                        description="fromtimestamp() without tz — uses local timezone, may cause UTC mismatch",
                        code_snippet=seg
                    ))
        return findings

    def _scan_infinite_loops(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.While):
                if isinstance(node.test, ast.Constant) and node.test.value is True:
                    has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
                    if not has_break:
                        seg = self._get_source_segment(lines, node.lineno)
                        findings.append(BugFinding(
                            file=filepath, line=node.lineno, severity="MEDIUM",
                            category="infinite_loop",
                            description="while True with no break — will hang if exception not raised",
                            code_snippet=seg
                        ))
        return findings

    def _scan_mutable_defaults(self, filepath, tree, lines):
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default is None:
                        continue
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        seg = self._get_source_segment(lines, node.lineno)
                        findings.append(BugFinding(
                            file=filepath, line=node.lineno, severity="MEDIUM",
                            category="mutable_default",
                            description=f"Mutable default argument in {node.name}() — shared across calls",
                            code_snippet=seg, auto_fixable=True
                        ))
        return findings

    def _scan_f_string_in_logging(self, filepath, tree, lines):
        """f-strings in log calls are evaluated even when log level is filtered."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("info", "warning", "error", "debug"):
                    for arg in node.args:
                        if isinstance(arg, ast.JoinedStr):
                            # Check if it's a complex f-string (not just a simple format)
                            has_expression = any(isinstance(v, ast.FormattedValue) for v in arg.values)
                            if has_expression:
                                seg = self._get_source_segment(lines, node.lineno)
                                findings.append(BugFinding(
                                    file=filepath, line=node.lineno, severity="LOW",
                                    category="fstring_logging",
                                    description="f-string in log call — evaluated even when log level filters it out (minor perf)",
                                    code_snippet=seg
                                ))
        return findings

    def _scan_race_conditions(self, filepath, tree, lines):
        """Check for shared state modified without locks in threaded context."""
        findings = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "threading" in node.module:
                # Check if Lock is imported
                has_lock = any(alias.name == "Lock" for alias in node.names)
                if has_lock:
                    # Check if shared state access is guarded
                    # This is a heuristic — look for ThreadPoolExecutor usage
                    for node2 in ast.walk(tree):
                        if isinstance(node2, ast.Call) and isinstance(node2.func, ast.Attribute):
                            if node2.func.attr in ("submit", "map"):
                                # Threaded execution found — check for unguarded state
                                seg = self._get_source_segment(lines, node2.lineno)
                                findings.append(BugFinding(
                                    file=filepath, line=node2.lineno, severity="MEDIUM",
                                    category="potential_race",
                                    description="ThreadPoolExecutor with Lock imported — verify shared state is lock-guarded",
                                    code_snippet=seg
                                ))
                                break
        return findings

    def scan_all(self) -> List[BugFinding]:
        self.findings = []
        for target in SCAN_TARGETS:
            self.scan_file(target)
        return self.findings

    def auto_fix(self, findings: List[BugFinding]) -> int:
        fixed = 0
        for f in findings:
            if not f.auto_fixable:
                continue
            filepath = self.root / f.file
            if not filepath.exists():
                continue
            source = filepath.read_text()
            lines = source.splitlines()

            if f.category == "bare_except":
                # Replace `except:` with `except Exception:`
                if f.line <= len(lines):
                    lines[f.line - 1] = lines[f.line - 1].replace("except:", "except Exception:")
                    filepath.write_text("\n".join(lines))
                    f.fix_applied = True
                    fixed += 1

            elif f.category == "position_underflow":
                # Replace `active_positions -= 1` with `active_positions = max(0, active_positions - 1)`
                if f.line <= len(lines):
                    old = lines[f.line - 1]
                    if "-= 1" in old and "max(0" not in old:
                        # Find the variable name
                        match = re.search(r'(self\.\w+).*-=\s*1', old)
                        if match:
                            var = match.group(1)
                            new_line = old.replace(f"{var} -= 1", f"{var} = max(0, {var} - 1)")
                            lines[f.line - 1] = new_line
                            filepath.write_text("\n".join(lines))
                            f.fix_applied = True
                            fixed += 1

            elif f.category == "unbound_after_except":
                # Replace `pass` in except with `continue` or assignment
                if f.line <= len(lines):
                    # Find the pass statement in the except block
                    for i in range(f.line, min(f.line + 5, len(lines))):
                        if "pass" in lines[i] and "except" not in lines[i]:
                            lines[i] = lines[i].replace("pass", "continue  # skip unbound variable")
                            filepath.write_text("\n".join(lines))
                            f.fix_applied = True
                            fixed += 1
                            break

            elif f.category == "mutable_default":
                # Replace =[] with =None and add `if x is None: x = []` in function body
                # This is complex — just flag it, don't auto-fix
                pass

        return fixed


# ═══════════════════════════════════════════════════════════════
# PROCESS MONITOR — Check running bots for issues
# ═══════════════════════════════════════════════════════════════

def check_running_processes() -> List[Dict]:
    """Check all running FDC bot processes for issues."""
    import subprocess
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    bots = []
    for line in ps.split("\n"):
        if "grep" in line:
            continue
        for bot_name in ["weather.*v21", "v21762", "observer", "scanner", "supervisor"]:
            if re.search(bot_name, line):
                parts = line.split()
                if len(parts) >= 11:
                    pid = parts[1]
                    cpu = parts[2]
                    mem = parts[3]
                    vsz = int(parts[4]) if parts[4].isdigit() else 0
                    rss = int(parts[5]) if parts[5].isdigit() else 0
                    cmd = " ".join(parts[10:])
                    # Check for issues
                    issues = []
                    if float(cpu) > 50:
                        issues.append(f"HIGH_CPU ({cpu}%)")
                    if rss > 200_000:  # >200MB
                        issues.append(f"HIGH_MEM ({rss//1024}MB)")
                    # Check for zombie
                    stat = parts[7] if len(parts) > 7 else ""
                    if "Z" in stat:
                        issues.append("ZOMBIE_PROCESS")
                    bots.append({
                        "pid": pid, "name": bot_name, "cpu": cpu, "mem_kb": rss,
                        "cmd": cmd[:120], "issues": issues
                    })
                break
    return bots

def check_duplicate_processes() -> List[Dict]:
    """Detect duplicate bot instances running simultaneously."""
    bots = check_running_processes()
    by_type = defaultdict(list)
    for b in bots:
        if "weather.*v21" in b["name"]:
            by_type["weather"].append(b)
        elif "v21762" in b["name"]:
            by_type["canary"].append(b)
    duplicates = []
    for bot_type, instances in by_type.items():
        if len(instances) > 2:  # Allow bash + python
            duplicates.append({
                "type": bot_type,
                "count": len(instances),
                "pids": [b["pid"] for b in instances],
                "warning": f"{len(instances)} processes for {bot_type} — possible duplicate instance"
            })
    return duplicates

def check_state_files() -> List[Dict]:
    """Check state files for corruption or inconsistencies."""
    issues = []
    state_files = [
        ROOT / "output" / "weather_bot" / "v2_1_live_state.json",
    ]
    # JSONL state files: validate line-by-line (whole-file json.loads fails on JSONL)
    jsonl_state_files = [
        ROOT / "output" / "v21762_scalper_canary" / "live_resolved.jsonl",
    ]
    for sf in state_files:
        if not sf.exists():
            continue
        try:
            data = json.loads(sf.read_text())
            # Check for negative active_positions
            ap = data.get("active_positions", 0)
            if ap < 0:
                issues.append({"file": str(sf), "issue": f"active_positions={ap} (negative!)"})
            # Check for halted without reason
            if data.get("halted") and not data.get("halt_reason"):
                issues.append({"file": str(sf), "issue": "halted=True but no halt_reason"})
            # Check for stale daily_reset
            dr = data.get("daily_reset", "")
            if dr:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if dr < today:
                    issues.append({"file": str(sf), "issue": f"stale daily_reset={dr} (today={today})"})
        except json.JSONDecodeError as e:
            issues.append({"file": str(sf), "issue": f"JSON corruption: {e}"})
    for sf in jsonl_state_files:
        if not sf.exists():
            continue
        bad_lines = 0
        total_lines = 0
        for i, line in enumerate(sf.read_text().splitlines(), 1):
            if not line.strip():
                continue
            total_lines += 1
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                bad_lines += 1
                if bad_lines <= 3:
                    issues.append({"file": str(sf), "issue": f"Line {i} corrupt: {e}"})
        if bad_lines > 3:
            issues.append({"file": str(sf), "issue": f"{bad_lines} corrupt lines total ({total_lines} lines)"})
    return issues

def check_jsonl_consistency() -> List[Dict]:
    """Check JSONL trade files for consistency."""
    issues = []
    jsonl_files = [
        ROOT / "output" / "weather_bot" / "v2_1_paper_trades.jsonl",
        ROOT / "output" / "v21762_scalper_canary" / "resolved_positions.jsonl",
        ROOT / "output" / "v21762_scalper_canary" / "paper_orders.jsonl",
    ]
    for jf in jsonl_files:
        if not jf.exists():
            continue
        lines = jf.read_text().splitlines()
        total = 0
        unparseable = 0
        for line in lines:
            if not line.strip():
                continue
            total += 1
            try:
                json.loads(line)
            except:
                unparseable += 1
        if unparseable > 0:
            issues.append({"file": str(jf), "issue": f"{unparseable}/{total} unparseable lines"})
    return issues


# ═══════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════

def generate_report(scanner: BugScanner, fix: bool = False) -> Dict:
    findings = scanner.scan_all()

    fixed = 0
    if fix:
        fixed = scanner.auto_fix(findings)

    # Runtime checks
    processes = check_running_processes()
    duplicates = check_duplicate_processes()
    state_issues = check_state_files()
    jsonl_issues = check_jsonl_consistency()

    # Categorize
    by_severity = defaultdict(list)
    by_category = defaultdict(list)
    for f in findings:
        by_severity[f.severity].append(f)
        by_category[f.category].append(f)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scan_targets": SCAN_TARGETS,
        "total_findings": len(findings),
        "by_severity": {k: len(v) for k, v in by_severity.items()},
        "by_category": {k: len(v) for k, v in by_category.items()},
        "auto_fixed": fixed,
        "findings": [asdict(f) for f in findings],
        "runtime": {
            "processes": processes,
            "duplicate_processes": duplicates,
            "state_issues": state_issues,
            "jsonl_issues": jsonl_issues,
        }
    }

    # Save report
    report_file = OUT / f"scan_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    report_file.write_text(json.dumps(report, indent=2, default=str))

    # Also save latest
    (OUT / "latest_report.json").write_text(json.dumps(report, indent=2, default=str))

    return report

def print_report(report: Dict):
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  FDC Bug Scanner — Report                                   ║")
    print(f"║  {report['timestamp'][:19]}                                      ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    bs = report["by_severity"]
    print("═══ FINDINGS BY SEVERITY ═══")
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        count = bs.get(sev, 0)
        print(f"  {sev:8s}: {count}")
    print(f"  {'TOTAL':8s}: {report['total_findings']}")
    print()

    bc = report["by_category"]
    print("═══ FINDINGS BY CATEGORY ═══")
    for cat, count in sorted(bc.items(), key=lambda x: -x[1]):
        print(f"  {cat:30s}: {count}")
    print()

    if report["auto_fixed"] > 0:
        print(f"═══ AUTO-FIX: {report['auto_fixed']} issues fixed ═══")
    else:
        print("═══ AUTO-FIX: 0 (run with --fix to enable) ═══")
    print()

    # Runtime checks
    rt = report["runtime"]
    print("═══ RUNTIME CHECKS ═══")
    print(f"  Processes: {len(rt['processes'])}")
    for p in rt["processes"]:
        status = "✅" if not p["issues"] else "⚠️"
        print(f"    {status} PID {p['pid']} ({p['name']}) CPU={p['cpu']}% MEM={p['mem_kb']//1024}MB")
        for issue in p["issues"]:
            print(f"       ⚠️ {issue}")

    if rt["duplicate_processes"]:
        print(f"\n  ⚠️ DUPLICATE PROCESSES:")
        for d in rt["duplicate_processes"]:
            print(f"    {d['type']}: {d['count']} processes (PIDs: {', '.join(d['pids'])})")
    else:
        print(f"  Duplicates: None ✅")

    if rt["state_issues"]:
        print(f"\n  ⚠️ STATE FILE ISSUES:")
        for s in rt["state_issues"]:
            print(f"    {s['file']}: {s['issue']}")
    else:
        print(f"  State files: OK ✅")

    if rt["jsonl_issues"]:
        print(f"\n  ⚠️ JSONL ISSUES:")
        for j in rt["jsonl_issues"]:
            print(f"    {j['file']}: {j['issue']}")
    else:
        print(f"  JSONL files: OK ✅")
    print()

    # Detailed findings
    print("═══ DETAILED FINDINGS ═══")
    for f in report["findings"]:
        sev_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(f["severity"], "⚪")
        fix_icon = "🔧" if f.get("fix_applied") else ("⚙️" if f["auto_fixable"] else "")
        print(f"\n  {sev_icon} [{f['severity']}] {f['category']} — {f['file']}:{f['line']} {fix_icon}")
        print(f"    {f['description']}")
        if f["code_snippet"]:
            for line in f["code_snippet"].split("\n")[:3]:
                print(f"    {line}")
    print()
    print(f"Report saved to {OUT / 'latest_report.json'}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FDC Bug Scanner")
    parser.add_argument("--fix", action="store_true", help="Auto-fix safe issues")
    parser.add_argument("--monitor", action="store_true", help="Continuous monitoring (every 15min)")
    parser.add_argument("--target", type=str, default=None, help="Scan specific file")
    args = parser.parse_args()

    scanner = BugScanner(ROOT)

    if args.target:
        scanner.scan_file(args.target)
        findings = scanner.findings
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_findings": len(findings),
            "findings": [asdict(f) for f in findings],
            "runtime": {"processes": [], "duplicate_processes": [], "state_issues": [], "jsonl_issues": []}
        }
        print_report(report)
        return

    if args.monitor:
        print(f"Starting continuous monitoring (15min interval)...")
        while True:
            report = generate_report(scanner, fix=args.fix)
            print_report(report)
            print(f"\n--- Next scan in 15min ---")
            time.sleep(900)
    else:
        report = generate_report(scanner, fix=args.fix)
        print_report(report)

if __name__ == "__main__":
    main()