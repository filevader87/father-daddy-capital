# Structure And Logic Improvements

This document supersedes the earlier container/Kubernetes-oriented deployment notes.

Current direction:

- Local-first runtime.
- `config/trading.yaml` as the supported configuration source.
- No Docker or Kubernetes dependency.
- Paper trading before live trading.
- Deterministic risk gating before broker execution.

See `docs/PRODUCTION_READINESS.md` for the active production-readiness plan.
