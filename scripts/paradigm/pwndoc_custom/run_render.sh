#!/bin/sh
# Host wrapper: run render_template.js inside the PwnDoc backend container, scratch in container /tmp only.
#   run_render.sh <template.docx> <out.docx> [audit.json] [--dump data.json] [--filters custom.js]
# --filters: the file is placed at <tmp>/lib/custom/report-filters-custom.js with <tmp>/lib/html2ooxml.js
# linked to the image's converter, so its relative requires resolve exactly as in src/lib/custom/.
set -eu
C=${PWNDOC_BACKEND:-pwndoc-pwndoc-backend-1}
HERE=$(cd "$(dirname "$0")" && pwd)
TPL=$1; OUT=$2; AUD=${3:-$HERE/sample_audit.json}
shift 2; [ $# -gt 0 ] && shift
DUMP=""; FILT=""
while [ $# -gt 0 ]; do
  case $1 in
    --dump) DUMP=$2; shift 2;;
    --filters) FILT=$2; shift 2;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done
D=/tmp/pdrender-$$
docker exec "$C" mkdir -p "$D/lib/custom"
trap 'docker exec "$C" rm -rf "$D" >/dev/null 2>&1 || true' EXIT
docker cp "$HERE/render_template.js" "$C:$D/render_template.js"
docker cp "$TPL" "$C:$D/tpl.docx"
docker cp "$AUD" "$C:$D/audit.json"
EXTRA=""
if [ -n "$FILT" ]; then
  docker cp "$FILT" "$C:$D/lib/custom/report-filters-custom.js"
  docker exec "$C" ln -s /app/src/lib/html2ooxml.js "$D/lib/html2ooxml.js"
  EXTRA="--filters $D/lib/custom/report-filters-custom.js"
fi
[ -n "$DUMP" ] && EXTRA="$EXTRA --dump $D/data.json"
# shellcheck disable=SC2086
docker exec -e NODE_PATH=/app/node_modules -w /app "$C" node "$D/render_template.js" "$D/tpl.docx" "$D/audit.json" "$D/out.docx" $EXTRA
[ -n "$DUMP" ] && docker cp "$C:$D/data.json" "$DUMP"
docker cp "$C:$D/out.docx" "$OUT"
