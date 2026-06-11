#!/usr/bin/env bash
# Assemble the fully static browser demo into demo/dist/.
# Host that folder anywhere (GitHub Pages, any web space) — no server needed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/demo/dist"

rm -rf "$DIST"
mkdir -p "$DIST/py/raceline_studio/core" "$DIST/maps"

# frontend, with the HTTP api swapped for the Pyodide bridge
cp -R "$ROOT/raceline_studio/web/." "$DIST/"
cp "$ROOT/demo/api-pyodide.js" "$DIST/js/api.js"
cp "$ROOT/demo/pyodide-worker.js" "$DIST/"

# python core for the worker to install into the virtual FS
cp "$ROOT/raceline_studio/__init__.py" "$ROOT/raceline_studio/wasm_api.py" \
   "$DIST/py/raceline_studio/"
cp "$ROOT"/raceline_studio/core/*.py "$DIST/py/raceline_studio/core/"
(cd "$DIST/py" && find . -name '*.py' | sed 's|^\./||' | sort \
  | awk 'BEGIN{printf "["} NR>1{printf ","} {printf "\"%s\"", $0} END{print "]"}' \
  > manifest.json)

# demo map + start line
cp "$ROOT"/maps/demo/map.yaml "$ROOT"/maps/demo/map.png \
   "$ROOT"/maps/demo/demo_raceline.csv "$DIST/maps/"
[ -f "$ROOT/maps/demo/centerline.csv" ] && \
  cp "$ROOT/maps/demo/centerline.csv" "$DIST/maps/"

echo "demo built -> $DIST"
