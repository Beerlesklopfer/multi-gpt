#!/bin/sh
# MultiGPT: den AKTUELLEN Arbeitsstand (auch uncommittet) auf eine Installation spielen, ohne Paket.
#
#   ./deploy.sh [--host <ziel>] [--deps] [--backup] [--no-migrate] [--no-restart] [--dry-run] [--help]
#
# Zweck: Zwischenstände auf einer mit dem .deb installierten Instanz ausprobieren. Kopiert den
# Quellcode (multigpt/ und mcp_imagetools/, falls vorhanden) in das venv der Installation
# (/usr/share/python/multi-gpt), sammelt die statischen Dateien, migriert die Datenbank und startet die
# Dienste neu. Die Dateien der Installation gehören root: alle Schreibschritte laufen mit sudo und
# werden vorher angekündigt; das Skript fragt nie nach Passwörtern und gibt weder Konfiguration noch
# Schlüssel aus. Ein späteres 'apt install' des Pakets überschreibt den Zwischenstand wieder.
# Gleichwertige Fassung für fish: ./deploy.fish (gleiche Schritte, gleiche Ausgabe).
set -eu

HOST=
DEPS=0
BACKUP=0
MIGRATE=1
RESTART=1
DRY=0
VENV=/usr/share/python/multi-gpt
UNITS="multi-gpt multi-gpt-worker multi-gpt-runner"
HEALTH="http://127.0.0.1:8000/healthz/"

usage() {
    cat <<'TXT'
Aufruf: ./deploy.sh [--host <ziel>] [--deps] [--backup] [--no-migrate] [--no-restart] [--dry-run]
  --host <ziel>  per ssh auf <ziel> spielen (ssh-Schlüssel, kein Passwort); sonst auf diesen Rechner
  --deps         zusätzlich Abhängigkeiten aus pyproject.toml installieren (pip install, braucht Netz)
  --backup       vor der Migration die Datenbank sichern (pg_dump nach /var/backups/multi-gpt)
  --no-migrate   Migrationen nicht ausführen
  --no-restart   Dienste nicht neu starten
  --dry-run      nur anzeigen, was getan würde
Exit-Codes: 0 Erfolg, 1 Fehler im Betrieb, 2 Voraussetzung fehlt, 64 falsche Befehlszeile.
TXT
}

step() { printf '%s\n' "==> $*"; }
fail() { printf '%s\n' "deploy: $1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --host) [ $# -ge 2 ] || fail "--host braucht ein Ziel." 64; HOST=$2; shift 2 ;;
        --deps) DEPS=1; shift ;;
        --backup) BACKUP=1; shift ;;
        --no-migrate) MIGRATE=0; shift ;;
        --no-restart) RESTART=0; shift ;;
        --dry-run) DRY=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; fail "unbekannte Option: $1" 64 ;;
    esac
done

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

run() {
    if [ "$DRY" = 1 ]; then printf '%s\n' "    (Probelauf) $*"; else "$@"; fi
}
# Eine Befehlszeile (ein Text) auf dem Ziel ausführen: lokal mit sh -c, entfernt per ssh.
target() {
    if [ -n "$HOST" ]; then run ssh -t "$HOST" "$1"; else run sh -c "$1"; fi
}

SUDO=
if [ -n "$HOST" ] || [ "$(id -u)" != 0 ]; then SUDO=sudo; fi

step "Vorprüfung"
command -v rsync >/dev/null 2>&1 || fail "rsync fehlt." 2
[ -d "$ROOT/multigpt" ] || fail "Quellordner multigpt/ nicht gefunden (neben deploy.sh)." 2
if [ -n "$HOST" ]; then
    command -v ssh >/dev/null 2>&1 || fail "ssh fehlt." 2
elif [ "$DRY" = 0 ]; then
    [ -d "$VENV" ] || fail "keine Installation unter $VENV: zuerst das Paket installieren." 2
    [ -z "$SUDO" ] || command -v sudo >/dev/null 2>&1 || fail "sudo fehlt, und du bist nicht root." 2
fi

REV=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unbekannt)
if [ -n "$(git -C "$ROOT" status --porcelain -- multigpt mcp_imagetools pyproject.toml 2>/dev/null)" ]; then
    DIRTY=" (mit uncommitteten Änderungen)"
else
    DIRTY=""
fi
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)
STAMP="$REV$DIRTY, Quelle Version $VERSION"
step "Stand: $STAMP"

# Ziel des Codes: site-packages des venv (Python-Version wird auf dem Ziel ermittelt).
if [ "$DRY" = 1 ]; then
    SITE="$VENV/lib/python3.X/site-packages"
elif [ -n "$HOST" ]; then
    SITE=$(ssh "$HOST" "ls -d $VENV/lib/python3.*/site-packages | head -n 1") || fail "Installation auf $HOST nicht gefunden ($VENV)." 2
else
    SITE=$(ls -d "$VENV"/lib/python3.*/site-packages | head -n 1)
fi

step "Code kopieren (nur multigpt/ und mcp_imagetools/; Besitzer root; __pycache__ bleibt unberührt)"
for pkg in multigpt mcp_imagetools; do
    [ -d "$ROOT/$pkg" ] || continue
    if [ -n "$HOST" ]; then
        run rsync -a --delete --no-o --no-g --chmod=D755,F644 --exclude __pycache__ --rsync-path="sudo rsync" "$ROOT/$pkg/" "$HOST:$SITE/$pkg/"
    else
        run $SUDO rsync -a --delete --no-o --no-g --chmod=D755,F644 --exclude __pycache__ "$ROOT/$pkg/" "$SITE/$pkg/"
    fi
done

if [ "$DEPS" = 1 ]; then
    step "Abhängigkeiten installieren (pip, braucht Netz)"
    [ -z "$HOST" ] || fail "--deps geht nur für diesen Rechner (ohne --host)." 64
    run $SUDO "$VENV/bin/pip" install --quiet "$ROOT"
fi

step "Statische Dateien sammeln (braucht sudo auf dem Ziel)"
target "$SUDO env DJANGO_SETTINGS_MODULE=multigpt.settings MULTI_GPT_ENV_FILE=/dev/null SECRET_KEY=deploy-only-not-secret DATABASE_URL=postgres://build/build DEBUG=False STATIC_ROOT=$VENV/static $VENV/bin/python -I -B -m django collectstatic --noinput --verbosity 0"

if [ "$BACKUP" = 1 ]; then
    step "Datenbank sichern (braucht sudo auf dem Ziel)"
    target "$SUDO install -d -o postgres -g postgres -m 0750 /var/backups/multi-gpt && $SUDO -u postgres pg_dump -Fc multi-gpt -f /var/backups/multi-gpt/vor-$REV.dump"
fi

if [ "$MIGRATE" = 1 ]; then
    step "Datenbank migrieren (mgpt-ctl migrate)"
    target "$SUDO mgpt-ctl migrate"
fi

step "Stand vermerken ($VENV/DEPLOYED)"
target "printf '%s\n' '$STAMP' | $SUDO tee $VENV/DEPLOYED >/dev/null"

if [ "$RESTART" = 1 ]; then
    step "Dienste neu starten ($UNITS)"
    target "$SUDO systemctl restart $UNITS"
fi

step "Prüfen"
FAILED=0
for unit in $UNITS; do
    if [ "$DRY" = 1 ]; then
        printf '%s\n' "    (Probelauf) systemctl is-active $unit"
    elif [ -n "$HOST" ]; then
        ssh "$HOST" "systemctl is-active --quiet $unit" || { printf '%s\n' "    $unit: nicht aktiv" >&2; FAILED=1; }
    else
        systemctl is-active --quiet "$unit" || { printf '%s\n' "    $unit: nicht aktiv" >&2; FAILED=1; }
    fi
done
if [ "$DRY" = 1 ]; then
    printf '%s\n' "    (Probelauf) GET $HEALTH"
elif [ -n "$HOST" ]; then
    ssh "$HOST" "curl -fsS --max-time 10 $HEALTH >/dev/null" || FAILED=1
else
    curl -fsS --max-time 10 "$HEALTH" >/dev/null || FAILED=1
fi
[ "$FAILED" = 0 ] || fail "Prüfung fehlgeschlagen. Rückweg: Paket neu installieren (apt-get install --reinstall ./multi-gpt_<version>_amd64.deb); Migrationen werden nicht zurückgenommen (Sicherung mit --backup)." 1
step "Fertig: $STAMP"
