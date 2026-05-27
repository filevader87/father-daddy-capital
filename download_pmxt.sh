#!/bin/bash
# Download 24h of pmxt archive (May 25 00:00 - May 25 23:00 UTC)
OUTDIR="/mnt/c/Users/12035/father_daddy_capital/pmxt_data"
BASE="https://r2v2.pmxt.dev/polymarket_orderbook_2026-05-25T"
for h in $(seq -w 0 23); do
  FILE="polymarket_orderbook_2026-05-25T${h}.parquet"
  if [ -f "$OUTDIR/$FILE" ]; then
    echo "SKIP $FILE (exists)"
  else
    echo "DL $FILE..."
    curl -s -o "$OUTDIR/$FILE" "${BASE}${h}.parquet"
    SIZE=$(stat -c%s "$OUTDIR/$FILE" 2>/dev/null || echo 0)
    echo "  -> $(( SIZE / 1024 / 1024 ))MB"
  fi
done
echo "DONE"
ls -lh "$OUTDIR" | tail -5
