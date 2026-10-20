#!/usr/bin/env bash
# Install a GitHub release binary into /usr/local/bin (placeholders substituted by Python).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates unzip >/dev/null

OWNER=__OWNER__
REPO=__REPO__
BINARY=__BINARY__
TAG=__TAG__
ASSET_SUBSTR=__ASSET_SUBSTR__
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

if [ -n "$TAG" ]; then
  API_URL="https://api.github.com/repos/${OWNER}/${REPO}/releases/tags/${TAG}"
else
  API_URL="https://api.github.com/repos/${OWNER}/${REPO}/releases/latest"
fi

python3 - "$API_URL" "$ASSET_SUBSTR" "$WORKDIR/asset_url" "$WORKDIR/asset_name" <<'PY'
import json, sys, urllib.request
api_url, needle, out_url, out_name = sys.argv[1:5]
req = urllib.request.Request(
    api_url,
    headers={"Accept": "application/vnd.github+json", "User-Agent": "orchestrator-catalog-provision"},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.load(resp)
assets = data.get("assets") or []
needle = needle.lower()
exts = (".zip", ".tar.gz", ".tgz")

def pick(pred):
    out = []
    for a in assets:
        an = (a.get("name") or "").lower()
        url = a.get("browser_download_url") or ""
        if url and pred(an) and any(an.endswith(e) for e in exts):
            out.append((an, url, a.get("name") or ""))
    return out

preferred = pick(lambda an: needle in an)
if not preferred:
    preferred = pick(lambda an: "linux" in an and ("amd64" in an or "x86_64" in an))
if not preferred:
    raise SystemExit(f"No matching release asset for {needle!r}. Assets: {[a.get('name') for a in assets]}")
preferred.sort(key=lambda t: (0 if t[0].endswith(".zip") else 1, t[0]))
_, url, name = preferred[0]
open(out_url, "w", encoding="utf-8").write(url)
open(out_name, "w", encoding="utf-8").write(name)
print(f"selected_asset={name}")
PY

ASSET_URL=$(cat asset_url)
ASSET_NAME=$(cat asset_name)
curl -fsSL "$ASSET_URL" -o "$ASSET_NAME"
SIZE=$(wc -c < "$ASSET_NAME" | tr -d ' ')
if [ "$SIZE" -lt 10000 ]; then
  echo "Downloaded asset too small (${SIZE} bytes)" >&2
  exit 1
fi
mkdir -p extract
case "$ASSET_NAME" in
  *.zip) unzip -qo "$ASSET_NAME" -d extract ;;
  *.tar.gz|*.tgz) tar -xzf "$ASSET_NAME" -C extract ;;
  *) echo "Unsupported archive: $ASSET_NAME" >&2; exit 1 ;;
esac
CANDIDATE=$(find extract -type f -name "$BINARY" | head -n 1 || true)
[ -z "$CANDIDATE" ] && CANDIDATE=$(find extract -type f -executable | head -n 1 || true)
if [ -z "$CANDIDATE" ]; then
  echo "Could not find binary '$BINARY' in archive" >&2
  exit 1
fi
install -m 0755 "$CANDIDATE" "/usr/local/bin/$BINARY"
# Move any other PATH hit that is a Python console-script (same basename).
python3 - "$BINARY" <<'PY'
import pathlib, shutil, sys
name = sys.argv[1]
target = pathlib.Path("/usr/local/bin") / name
for found in {shutil.which(name) or "", str(target)}:
    if not found:
        continue
    p = pathlib.Path(found)
    if not p.is_file() or p.resolve() == target.resolve():
        continue
    try:
        head = p.read_bytes()[:240]
        size = p.stat().st_size
    except OSError:
        continue
    if head.startswith(b"\x7fELF"):
        continue
    text = head.decode("utf-8", errors="ignore")
    first = text.split("\n", 1)[0].lower()
    if not (first.startswith("#!") and "python" in first):
        continue
    bak = p.with_name(p.name + ".py-wrapper")
    try:
        p.rename(bak)
        print(f"shadow_wrapper_moved path={p} -> {bak} size={size}")
    except OSError as exc:
        print(f"shadow_wrapper_keep path={p} err={exc}")
PY
python3 - "/usr/local/bin/$BINARY" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
data = p.read_bytes()[:8]
if len(data) < 4 or data.startswith((b"Not ", b"<", b"{")):
    raise SystemExit("installed path is not a binary")
if not data.startswith(b"\x7fELF") and p.stat().st_size < 100000:
    raise SystemExit(f"refusing suspicious binary size={p.stat().st_size}")
print(f"installed_ok path={p} size={p.stat().st_size}")
PY
# Prefer the just-installed path for the version probe.
export PATH="/usr/local/bin:${PATH}"
RESOLVED=$(command -v "$BINARY")
echo "resolved=$RESOLVED"
case "$RESOLVED" in
  /usr/local/bin/"$BINARY") ;;
  *)
    echo "PATH still resolves $BINARY away from /usr/local/bin" >&2
    exit 1
    ;;
esac
OUT=$("$BINARY" -version 2>&1 || "$BINARY" --version 2>&1 || true)
echo "$OUT"
echo "INSTALL_OK binary=$BINARY"

