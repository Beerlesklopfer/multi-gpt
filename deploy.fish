#!/usr/bin/env fish
# MultiGPT: den AKTUELLEN Arbeitsstand (auch uncommittet) auf eine Installation spielen, ohne Paket.
#
#   ./deploy.fish [--host <ziel>] [--deps] [--backup] [--no-migrate] [--no-restart] [--dry-run] [--help]
#
# Zweck: Zwischenstände auf einer mit dem .deb installierten Instanz ausprobieren. Kopiert den
# Quellcode (multigpt/ und mcp_imagetools/, falls vorhanden) in das venv der Installation
# (/usr/share/python/multi-gpt), sammelt die statischen Dateien, migriert die Datenbank und startet die
# Dienste neu. Die Dateien der Installation gehören root: alle Schreibschritte laufen mit sudo und
# werden vorher angekündigt; das Skript fragt nie nach Passwörtern und gibt weder Konfiguration noch
# Schlüssel aus. Ein späteres 'apt install' des Pakets überschreibt den Zwischenstand wieder.
# Gleichwertige Fassung für sh: ./deploy.sh (gleiche Schritte, gleiche Ausgabe).

set -g host
set -g deps 0
set -g backup 0
set -g migrate 1
set -g restart 1
set -g dry 0
set -g venv /usr/share/python/multi-gpt
set -g units multi-gpt multi-gpt-worker multi-gpt-runner
set -g health http://127.0.0.1:8000/healthz/

function usage
    echo 'Aufruf: ./deploy.sh [--host <ziel>] [--deps] [--backup] [--no-migrate] [--no-restart] [--dry-run]'
    echo '  --host <ziel>  per ssh auf <ziel> spielen (ssh-Schlüssel, kein Passwort); sonst auf diesen Rechner'
    echo '  --deps         zusätzlich Abhängigkeiten aus pyproject.toml installieren (pip install, braucht Netz)'
    echo '  --backup       vor der Migration die Datenbank sichern (pg_dump nach /var/backups/multi-gpt)'
    echo '  --no-migrate   Migrationen nicht ausführen'
    echo '  --no-restart   Dienste nicht neu starten'
    echo '  --dry-run      nur anzeigen, was getan würde'
    echo 'Exit-Codes: 0 Erfolg, 1 Fehler im Betrieb, 2 Voraussetzung fehlt, 64 falsche Befehlszeile.'
end

function step
    echo "==> $argv"
end

function fail
    echo "deploy: $argv[1]" >&2
    if test (count $argv) -ge 2
        exit $argv[2]
    end
    exit 1
end

set -l i 1
while test $i -le (count $argv)
    switch $argv[$i]
        case --host
            test (math $i + 1) -le (count $argv); or fail "--host braucht ein Ziel." 64
            set host $argv[(math $i + 1)]
            set i (math $i + 2)
        case --deps
            set deps 1
            set i (math $i + 1)
        case --backup
            set backup 1
            set i (math $i + 1)
        case --no-migrate
            set migrate 0
            set i (math $i + 1)
        case --no-restart
            set restart 0
            set i (math $i + 1)
        case --dry-run
            set dry 1
            set i (math $i + 1)
        case --help -h
            usage
            exit 0
        case '*'
            usage >&2
            fail "unbekannte Option: $argv[$i]" 64
    end
end

set -g root (path resolve (status dirname))

# Befehl ausführen oder im Probelauf nur anzeigen.
function run
    if test $dry = 1
        echo "    (Probelauf) $argv"
    else
        $argv
    end
end

# Eine Befehlszeile (ein Text) auf dem Ziel ausführen: lokal mit sh -c, entfernt per ssh.
function target
    if test -n "$host"
        run ssh -t $host $argv[1]
    else
        run sh -c $argv[1]
    end
end

set -g sudo
if test -n "$host"; or test (id -u) != 0
    set sudo sudo
end

step "Vorprüfung"
command -q rsync; or fail "rsync fehlt." 2
test -d $root/multigpt; or fail "Quellordner multigpt/ nicht gefunden (neben deploy.fish)." 2
if test -n "$host"
    command -q ssh; or fail "ssh fehlt." 2
else if test $dry = 0
    test -d $venv; or fail "keine Installation unter $venv: zuerst das Paket installieren." 2
    if test -n "$sudo"
        command -q sudo; or fail "sudo fehlt, und du bist nicht root." 2
    end
end

set -l rev (git -C $root rev-parse --short HEAD 2>/dev/null; or echo unbekannt)
set -l dirty ""
if test (count (git -C $root status --porcelain -- multigpt mcp_imagetools pyproject.toml 2>/dev/null)) -gt 0
    set dirty " (mit uncommitteten Änderungen)"
end
set -l src_version (sed -n 's/^version = "\(.*\)"/\1/p' $root/pyproject.toml | head -n 1)
set -g stamp "$rev$dirty, Quelle Version $src_version"
step "Stand: $stamp"

# Ziel des Codes: site-packages des venv (Python-Version wird auf dem Ziel ermittelt).
set -l site
if test $dry = 1
    set site "$venv/lib/python3.X/site-packages"
else if test -n "$host"
    set site (ssh $host "ls -d $venv/lib/python3.*/site-packages | head -n 1"); or fail "Installation auf $host nicht gefunden ($venv)." 2
else
    set site (ls -d $venv/lib/python3.*/site-packages | head -n 1)
end

step "Code kopieren (nur multigpt/ und mcp_imagetools/; Besitzer root; __pycache__ bleibt unberührt)"
for pkg in multigpt mcp_imagetools
    test -d $root/$pkg; or continue
    if test -n "$host"
        run rsync -a --delete --no-o --no-g --chmod=D755,F644 --exclude __pycache__ --rsync-path="sudo rsync" "$root/$pkg/" "$host:$site/$pkg/"
    else
        run $sudo rsync -a --delete --no-o --no-g --chmod=D755,F644 --exclude __pycache__ "$root/$pkg/" "$site/$pkg/"
    end
end

if test $deps = 1
    step "Abhängigkeiten installieren (pip, braucht Netz)"
    test -z "$host"; or fail "--deps geht nur für diesen Rechner (ohne --host)." 64
    run $sudo "$venv/bin/pip" install --quiet "$root"
end

step "Statische Dateien sammeln (braucht sudo auf dem Ziel)"
target "$sudo env DJANGO_SETTINGS_MODULE=multigpt.settings MULTI_GPT_ENV_FILE=/dev/null SECRET_KEY=deploy-only-not-secret DATABASE_URL=postgres://build/build DEBUG=False STATIC_ROOT=$venv/static $venv/bin/python -I -B -m django collectstatic --noinput --verbosity 0"

if test $backup = 1
    step "Datenbank sichern (braucht sudo auf dem Ziel)"
    target "$sudo install -d -o postgres -g postgres -m 0750 /var/backups/multi-gpt && $sudo -u postgres pg_dump -Fc multi-gpt -f /var/backups/multi-gpt/vor-$rev.dump"
end

if test $migrate = 1
    step "Datenbank migrieren (mgpt-ctl migrate)"
    target "$sudo mgpt-ctl migrate"
end

step "Stand vermerken ($venv/DEPLOYED)"
target "printf '%s\n' '$stamp' | $sudo tee $venv/DEPLOYED >/dev/null"

if test $restart = 1
    step "Dienste neu starten ($units)"
    target "$sudo systemctl restart $units"
end

step "Prüfen"
set -l failed 0
for unit in $units
    if test $dry = 1
        echo "    (Probelauf) systemctl is-active $unit"
    else if test -n "$host"
        ssh $host "systemctl is-active --quiet $unit"; or begin
            echo "    $unit: nicht aktiv" >&2
            set failed 1
        end
    else
        systemctl is-active --quiet $unit; or begin
            echo "    $unit: nicht aktiv" >&2
            set failed 1
        end
    end
end
if test $dry = 1
    echo "    (Probelauf) GET $health"
else if test -n "$host"
    ssh $host "curl -fsS --max-time 10 $health >/dev/null"; or set failed 1
else
    curl -fsS --max-time 10 $health >/dev/null; or set failed 1
end
test $failed = 0; or fail "Prüfung fehlgeschlagen. Rückweg: Paket neu installieren (apt-get install --reinstall ./multi-gpt_<version>_amd64.deb); Migrationen werden nicht zurückgenommen (Sicherung mit --backup)." 1
step "Fertig: $stamp"
