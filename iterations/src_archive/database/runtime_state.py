"""
FDC runtime_state shim — provides the same interface as upstream PolyWeather's
src.database.runtime_state, but backed by JSON files instead of SQLite.

This allows deb_algorithm.py to work standalone in the FDC project without
the full PolyWeather database stack.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ─── Storage mode ───
STATE_STORAGE_SQLITE = "sqlite"
STATE_STORAGE_JSON = "json"

_STORAGE_MODE = STATE_STORAGE_JSON  # FDC uses JSON, not SQLite

# ─── Paths ───
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "output" / "polyweather_data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DAILY_RECORDS_FILE = _DATA_DIR / "daily_records.json"
_TRAINING_FEATURES_FILE = _DATA_DIR / "training_features.json"
_TRUTH_RECORDS_FILE = _DATA_DIR / "truth_records.json"


def get_state_storage_mode() -> str:
    """Return the storage mode — always JSON for FDC."""
    return _STORAGE_MODE


# ─── JSON file helpers ───
def _load_json(filepath: Path) -> Dict:
    if filepath.exists():
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_json(filepath: Path, data: Dict) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# DailyRecordRepository — stores per-city per-date forecast + actual
# ═══════════════════════════════════════════════════════════════

class DailyRecordRepository:
    """JSON-backed daily record store compatible with deb_algorithm.py."""

    def __init__(self):
        self._filepath = _DAILY_RECORDS_FILE

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all daily records. Returns {city: {date: record}}."""
        return _load_json(self._filepath)

    def replace_all(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Replace all daily records."""
        _save_json(self._filepath, data)

    def upsert(self, city: str, date_str: str, record: Dict) -> None:
        """Insert or update a single record."""
        all_data = self.load_all()
        if city not in all_data:
            all_data[city] = {}
        all_data[city][date_str] = record
        _save_json(self._filepath, all_data)

    def get(self, city: str, date_str: str) -> Optional[Dict]:
        """Get a single record."""
        return self.load_all().get(city, {}).get(date_str)


# ═══════════════════════════════════════════════════════════════
# TrainingFeatureRecordRepository — stores training features for DEB
# ═══════════════════════════════════════════════════════════════

class TrainingFeatureRecordRepository:
    """JSON-backed training feature store."""

    def __init__(self):
        self._filepath = _TRAINING_FEATURES_FILE

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        return _load_json(self._filepath)

    def upsert_record(self, city: str, date_str: str, payload: Dict) -> None:
        """Insert or update a training feature record."""
        all_data = self.load_all()
        if city not in all_data:
            all_data[city] = {}
        all_data[city][date_str] = payload
        _save_json(self._filepath, all_data)

    def get(self, city: str, date_str: str) -> Optional[Dict]:
        return self.load_all().get(city, {}).get(date_str)


# ═══════════════════════════════════════════════════════════════
# TruthRecordRepository — stores actual observed highs
# ═══════════════════════════════════════════════════════════════

class TruthRecordRepository:
    """JSON-backed truth/actual record store."""

    def __init__(self):
        self._filepath = _TRUTH_RECORDS_FILE

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        return _load_json(self._filepath)

    def upsert_truth(
        self,
        city: str,
        target_date: str,
        actual_high: float,
        settlement_source: str = "",
        settlement_station_code: str = "",
        **kwargs,
    ) -> None:
        """Insert or update a truth record."""
        all_data = self.load_all()
        if city not in all_data:
            all_data[city] = {}
        all_data[city][target_date] = {
            "actual_high": float(actual_high),
            "settlement_source": settlement_source,
            "settlement_station_code": settlement_station_code,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        _save_json(self._filepath, all_data)

    def get(self, city: str, date_str: str) -> Optional[Dict]:
        return self.load_all().get(city, {}).get(date_str)


# ─── Module-level singleton instances (compatible with deb_algorithm.py) ───
# These are created at import time by deb_algorithm.py