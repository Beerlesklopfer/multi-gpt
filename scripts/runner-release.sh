#!/usr/bin/env bash
# Release-Dateien des Runner-Agents (M14, Rust-Crate runner-rs/) bauen und
# signieren – in der CI je Release-Lauf (package.yml, Job runner-dist), lokal mit
# `make runner-dist` zum Ausprobieren (dann mit Wegwerf-Schlüssel, Version "dev").
#
#   scripts/runner-release.sh <version> <ausgabe-ordner> [vorige release_keys.json]
#
# Ablauf (alles in EINEM Prozess, der private Schlüssel verlässt ihn nie):
#   1. frisches Ed25519-Paar in einem RAM-Ordner (/dev/shm, umask 077);
#   2. runner-rs/catalog.json mit "release": <version> → catalog.json, signieren;
#   3. Binaries für amd64 und arm64 bauen (cargo zigbuild, musl, statisch), dabei
#      Katalog, Signatur und key_id einbetten (build.rs: MULTIGPT_CATALOG*);
#   4. SHA256SUMS über Binaries und catalog.json, signieren;
#   5. release_keys.json = dieser Schlüssel + bis zu zwei der vorigen Releases;
#   6. alle Signaturen mit dem öffentlichen Teil prüfen;
#   7. privaten Teil überschreiben und löschen (trap, auch bei Fehlern).
#
# Formate (mit trust abgestimmt): öffentlicher Schlüssel = Base64 der 32 Byte
# roh; key_id = sha256(32 Byte roh) hex, erste 16 Zeichen; <datei>.sig = Base64
# der 64 Byte rohen Ed25519-Signatur über die exakten Bytes der Datei, eine
# Zeile. Ausgabe (= RUNNER_DIST_DIR im Paket, /usr/share/multi-gpt/runner):
#   multigpt-runner-linux-amd64, multigpt-runner-linux-arm64, catalog.json(.sig),
#   SHA256SUMS(.sig), release_keys.json
#
# Umgebung: RUNNER_CRATE_DIR (Vorgabe runner-rs), RUNNER_TARGETS (Vorgabe "amd64 arm64"), CARGO (Vorgabe cargo),
# CARGO_TARGET_DIR (Vorgabe runner-rs/target), RUNNER_SKIP_BUILD=1 (nur Signieren
# vorhandener Binaries in $CARGO_TARGET_DIR – für Tests), RUNNER_BUILD_NONET=1
# (Bau ohne Netz in eigenem Namensraum, unshare -rn; vorher `cargo fetch` – so
# erreichen Build-Skripte der Abhängigkeiten niemanden, solange der Schlüssel
# existiert; die CI setzt es).
set -euo pipefail
set +x   # nie Befehle samt Pfaden/Inhalten ins Log

version=${1:?Aufruf: runner-release.sh <version> <ausgabe> [vorige release_keys.json]}
out=${2:?Ausgabe-Ordner fehlt}
prev_keys=${3:-}

root=$(cd "$(dirname "$0")/.." && pwd)
crate=${RUNNER_CRATE_DIR:-$root/runner-rs}
cargo=${CARGO:-cargo}
targets=${RUNNER_TARGETS:-amd64 arm64}
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-$crate/target}

[[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+|dev)$ ]] || {
  echo "runner-release: Version '$version' ungültig (x.y.z oder dev)" >&2; exit 1; }
command -v openssl >/dev/null || { echo "runner-release: openssl fehlt" >&2; exit 1; }

triple() {
  case "$1" in
    amd64) echo x86_64-unknown-linux-musl ;;
    arm64) echo aarch64-unknown-linux-musl ;;
    *) echo "runner-release: unbekannte Architektur $1" >&2; exit 1 ;;
  esac
}

# Abhängigkeiten holen, bevor ein Schlüssel existiert (der Bau läuft danach
# mit RUNNER_BUILD_NONET=1 ohne Netz).
if [ "${RUNNER_SKIP_BUILD:-0}" != 1 ]; then
  (cd "$crate" && "$cargo" fetch --locked >/dev/null)
fi

# --- Schlüssel: nur im RAM, nur für diesen Prozess --------------------------
keydir=$(umask 077; mktemp -d "${RUNNER_KEY_TMP:-/dev/shm}/mgpt-release.XXXXXX" 2>/dev/null \
         || mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/mgpt-release.XXXXXX")
chmod 700 "$keydir"
destroy_key() {
  if [ -f "$keydir/release.key" ]; then
    shred -u -z "$keydir/release.key" 2>/dev/null || rm -f "$keydir/release.key"
  fi
  rm -rf "$keydir"
}
trap destroy_key EXIT
trap 'exit 130' INT TERM

(umask 077; openssl genpkey -algorithm ed25519 -out "$keydir/release.key" 2>/dev/null)
openssl pkey -in "$keydir/release.key" -pubout -out "$keydir/release.pub.pem" 2>/dev/null
pub_raw() { openssl pkey -pubin -in "$keydir/release.pub.pem" -outform DER | tail -c 32; }
pub_b64=$(pub_raw | base64 -w0)
key_id=$(pub_raw | sha256sum | cut -c1-16)
[ "$(pub_raw | wc -c)" -eq 32 ] || { echo "runner-release: öffentlicher Schlüssel unerwartet" >&2; exit 1; }

sign() {  # sign <datei> → <datei>.sig (Base64, eine Zeile)
  openssl pkeyutl -sign -rawin -inkey "$keydir/release.key" -in "$1" \
    | base64 -w0 > "$1.sig"
  echo >> "$1.sig"
}
verify() {  # verify <datei>  (gegen den öffentlichen Teil)
  base64 -d "$1.sig" > "$keydir/sig.bin"
  [ "$(wc -c < "$keydir/sig.bin")" -eq 64 ] || { echo "runner-release: Signatur $1 hat falsche Länge" >&2; exit 1; }
  openssl pkeyutl -verify -rawin -pubin -inkey "$keydir/release.pub.pem" \
    -in "$1" -sigfile "$keydir/sig.bin" >/dev/null \
    || { echo "runner-release: Signatur von $1 ungültig" >&2; exit 1; }
}

mkdir -p "$out"
out=$(cd "$out" && pwd)
rm -f "$out"/multigpt-runner-linux-* "$out"/catalog.json* "$out"/SHA256SUMS* "$out"/release_keys.json

# --- Katalog -------------------------------------------------------------------
python3 -I - "$crate/catalog.json" "$out/catalog.json" "$version" <<'PY'
import json, sys
src, dst, version = sys.argv[1:]
data = json.load(open(src, encoding="utf-8"))
if data.get("format") != 1 or not isinstance(data.get("services"), list):
    sys.exit("runner-release: catalog.json hat nicht format 1 mit services")
data["release"] = version
text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
if len(text.encode()) > 256 * 1024:
    sys.exit("runner-release: catalog.json größer als 256 KiB")
open(dst, "w", encoding="utf-8").write(text)
PY
sign "$out/catalog.json"

# --- Binaries ----------------------------------------------------------------
if [ "${RUNNER_SKIP_BUILD:-0}" != 1 ]; then
  build_args=()
  nonet=()
  [ "${RUNNER_BUILD_NONET:-0}" = 1 ] && nonet=(unshare -rn) && export CARGO_NET_OFFLINE=true
  for arch in $targets; do build_args+=(--target "$(triple "$arch")"); done
  # Release-Profil ohne Änderung an Cargo.toml: kleine, gestrippte Binaries.
  (
    cd "$crate"
    MULTIGPT_CATALOG="$out/catalog.json" \
    MULTIGPT_CATALOG_SIG="$out/catalog.json.sig" \
    MULTIGPT_CATALOG_KEY="$key_id" \
    CARGO_PROFILE_RELEASE_STRIP=true \
    CARGO_PROFILE_RELEASE_LTO=true \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
    "${nonet[@]}" "$cargo" zigbuild --release --locked -p multigpt-runner --bin multigpt-runner "${build_args[@]}"
  )
fi
for arch in $targets; do
  bin="$CARGO_TARGET_DIR/$(triple "$arch")/release/multigpt-runner"
  [ -f "$bin" ] || { echo "runner-release: $bin fehlt" >&2; exit 1; }
  install -m 0644 "$bin" "$out/multigpt-runner-linux-$arch"
done

# --- Prüfsummen ----------------------------------------------------------------
(cd "$out" && sha256sum -- multigpt-runner-linux-* catalog.json > SHA256SUMS)
sign "$out/SHA256SUMS"

# --- release_keys.json: dieser + bis zu zwei vorige (neueste zuerst) ----------
python3 -I - "$out/release_keys.json" "$version" "$key_id" "$pub_b64" "$prev_keys" <<'PY'
import base64, hashlib, json, re, sys
dst, version, key_id, pub, prev = sys.argv[1:]
keys = [{"release": version, "key_id": key_id, "public_key": pub}]
if prev:
    try:
        old = json.load(open(prev, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"runner-release: vorige release_keys.json unlesbar: {exc}")
    for entry in old.get("keys", []):
        raw = base64.b64decode(entry["public_key"], validate=True)
        # Nur gültige Einträge übernehmen; die eigene Version (erneuter Lauf) nicht doppelt.
        if len(raw) != 32 or hashlib.sha256(raw).hexdigest()[:16] != entry["key_id"]:
            sys.exit(f"runner-release: vorige release_keys.json: key_id passt nicht ({entry})")
        if not re.fullmatch(r"\d+\.\d+\.\d+|dev", entry["release"]):
            sys.exit(f"runner-release: vorige release_keys.json: Version ungültig ({entry})")
        if entry["release"] == version or any(k["key_id"] == entry["key_id"] for k in keys):
            continue
        keys.append({k: entry[k] for k in ("release", "key_id", "public_key")})
        if len(keys) == 3:
            break
json.dump({"version": 1, "keys": keys}, open(dst, "w", encoding="utf-8"), indent=2)
open(dst, "a").write("\n")
PY

# --- Selbstprüfung -------------------------------------------------------------
verify "$out/catalog.json"
verify "$out/SHA256SUMS"
(cd "$out" && sha256sum --check --strict --quiet SHA256SUMS)
chmod 0644 "$out"/*

echo "runner-release: ${version} key_id=${key_id}"
(cd "$out" && ls -l)
