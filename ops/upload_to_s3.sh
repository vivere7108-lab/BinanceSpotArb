#!/usr/bin/env bash
# Move finished recordings (any UTC day before today) from /data/live to
# s3://BUCKET/phase0/<stream>/<day>/. Run hourly by binance-upload.timer.
set -euo pipefail
BUCKET="${1:?usage: upload_to_s3.sh BUCKET}"
today=$(date -u +%F)
shopt -s nullglob
for f in /data/live/*.jsonl.gz; do
  name=$(basename "$f")
  day=$(grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' <<<"$name" | head -1 || true)
  [[ -n "$day" && "$day" < "$today" ]] || continue   # today's files are still being written
  stream=${name%%-"$day"*}
  aws s3 mv --only-show-errors "$f" "s3://$BUCKET/phase0/$stream/$day/$name"
done
