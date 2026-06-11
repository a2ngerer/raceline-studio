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
cp "$ROOT"/maps/icra2026_map/map.yaml "$ROOT"/maps/icra2026_map/map.png \
   "$ROOT"/maps/icra2026_map/icra2026_map_raceline.csv "$DIST/maps/"
[ -f "$ROOT/maps/icra2026_map/centerline.csv" ] && \
  cp "$ROOT/maps/icra2026_map/centerline.csv" "$DIST/maps/"

# Cache busting: stamp a build id into the worker URL and the asset links
# so a deploy invalidates cached copies immediately (Pages caches 10 min).
VERSION="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%s)"
sed -i.bak "s/__BUILD__/$VERSION/g" "$DIST/js/api.js"
sed -i.bak \
  -e "s|href=\"fonts.css\"|href=\"fonts.css?v=$VERSION\"|" \
  -e "s|href=\"css/studio.css\"|href=\"css/studio.css?v=$VERSION\"|" \
  -e "s|src=\"vendor/gsap.min.js\"|src=\"vendor/gsap.min.js?v=$VERSION\"|" \
  -e "s|src=\"js/main.js\"|src=\"js/main.js?v=$VERSION\"|" \
  "$DIST/index.html"
rm -f "$DIST/js/api.js.bak" "$DIST/index.html.bak"

echo "demo built -> $DIST (build $VERSION)"
