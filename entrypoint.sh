#!/bin/sh
# kombo container entrypoint — dispatches the two roles of the one image.
#
#   serve   long-running FastAPI/uvicorn API over DATA_DIR/eiendommer.geojson
#   sync    one-shot: download the latest XLSX + regenerate the dataset into
#           DATA_DIR (the persistent volume). The serve process notices the new
#           file by mtime and reloads it — no restart needed.
#
# Anything else is exec'd verbatim (handy for `docker compose run … sh`).
set -e

DATA_DIR="${DATA_DIR:-/data}"
cmd="${1:-serve}"

case "$cmd" in
  serve)
    exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8090}"
    ;;
  sync)
    cd "$DATA_DIR"
    # geocode.py / fetch_xlsx.py read source.env relative to CWD and write
    # their outputs (eiendommer.geojson, missing.csv, geocode_cache.sqlite)
    # there too. Seed a source.env on the volume so the documented config knob
    # keeps working; real env vars still win (the scripts use setdefault).
    [ -f source.env ] || cp /app/source.env ./source.env
    echo "[sync] resolving + downloading latest XLSX into $DATA_DIR …"
    python /app/fetch_xlsx.py -o source.xlsx
    echo "[sync] geocoding (cache: $DATA_DIR/geocode_cache.sqlite) …"
    python /app/geocode.py source.xlsx
    echo "[sync] done — eiendommer.geojson refreshed."
    ;;
  *)
    exec "$@"
    ;;
esac
