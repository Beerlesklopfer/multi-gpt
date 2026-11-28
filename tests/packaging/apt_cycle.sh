#!/usr/bin/env bash
# M12-04: Paket-Zyklus in einem Debian-13-Container mit systemd (Podman):
# Installation einer älteren Version, Update, Prüfungen, remove, purge, Neuinstallation.
#
#   tests/packaging/apt_cycle.sh ALT.deb NEU.deb
#
# Beispiel mit den Release-Paketen:
#   gh release download v0.4.0 -p '*.deb' -D /tmp/debs
#   gh release download v0.4.1 -p '*.deb' -D /tmp/debs
#   tests/packaging/apt_cycle.sh /tmp/debs/multi-gpt_0.4.0_amd64.deb /tmp/debs/multi-gpt_0.4.1_amd64.deb
#
# Braucht Internet (Debian-Pakete) und Podman; dauert einige Minuten. Räumt Container und
# Image am Ende ab. Jede Prüfung druckt PASS/FAIL; Exit-Code 1 bei einem FAIL.
set -euo pipefail

OLD=${1:?altes .deb}
NEW=${2:?neues .deb}
NAME=mgpt-apt-cycle-$$
IMAGE=localhost/mgpt-apt-cycle:test
WORK=$(mktemp -d)
FAILED=0

cleanup() {
    podman rm -f "$NAME" >/dev/null 2>&1 || true
    podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

cp "$OLD" "$WORK/old.deb"
cp "$NEW" "$WORK/new.deb"
cat > "$WORK/Containerfile" <<'EOF'
FROM docker.io/library/debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus ca-certificates \
      curl postgresql postgresql-17-pgvector >/dev/null \
 && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF

echo "== Image bauen"
podman build -q -t "$IMAGE" "$WORK" >/dev/null
podman run -d --name "$NAME" --systemd=always -v "$WORK:/debs:ro,Z" "$IMAGE" >/dev/null

ct() { podman exec "$NAME" bash -c "$*"; }
check() {
    local label=$1; shift
    if ct "$@" >/dev/null 2>&1; then echo "PASS $label"; else echo "FAIL $label"; FAILED=1; fi
}
wait_active() {
    for _ in $(seq 60); do
        ct "systemctl is-active --quiet multi-gpt && curl -skf https://localhost/healthz/" \
            >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

echo "== Warten auf systemd und PostgreSQL"
for _ in $(seq 60); do
    ct "systemctl is-active --quiet postgresql" >/dev/null 2>&1 && break
    sleep 2
done
check "PostgreSQL läuft" "systemctl is-active --quiet postgresql"

echo "== Alte Version installieren"
ct "apt-get update -qq && apt-get install -y -qq /debs/old.deb" >"$WORK/install-old.log" 2>&1 \
    || { echo "FAIL Installation alt"; tail -30 "$WORK/install-old.log"; exit 1; }
echo "PASS Installation alt ($(ct "dpkg-query -W -f='\${Version}' multi-gpt"))"
if wait_active; then echo "PASS Dienst und /healthz/ (alt)"; else echo "FAIL Dienst (alt)"; FAILED=1; fi
ct "echo marker > /var/lib/multi-gpt/media/marker.txt && chown multi-gpt: /var/lib/multi-gpt/media/marker.txt"
ct "mgpt-ctl shell -c \"from django.contrib.auth import get_user_model as g; g().objects.create_user('Probe', password='Probe-Kennwort-1')\"" \
    >/dev/null 2>&1 || true
KEY_BEFORE=$(ct "grep ^FIELD_ENCRYPTION_KEY= /etc/multi-gpt/.env")

echo "== Update auf die neue Version"
ct "apt-get install -y -qq /debs/new.deb" >"$WORK/install-new.log" 2>&1 \
    || { echo "FAIL Update"; tail -30 "$WORK/install-new.log"; exit 1; }
echo "PASS Update ($(ct "dpkg-query -W -f='\${Version}' multi-gpt"))"
if wait_active; then echo "PASS Dienst und /healthz/ (neu)"; else echo "FAIL Dienst (neu)"; FAILED=1; fi
check "Keine offenen Migrationen" "! mgpt-ctl showmigrations --plan | grep -q '\[ \]'"
check "Worker läuft" "systemctl is-active --quiet multi-gpt-worker"
check "Konto erhalten" "mgpt-ctl shell -c \"from django.contrib.auth import get_user_model as g; assert g().objects.filter(username='Probe').exists()\""
check "Medien erhalten" "test -f /var/lib/multi-gpt/media/marker.txt"
check ".env unverändert (Schlüssel)" "grep -qx '$KEY_BEFORE' /etc/multi-gpt/.env"
check "nginx-Konfiguration gültig" "nginx -t"

echo "== apt remove"
ct "apt-get remove -y -qq multi-gpt" >"$WORK/remove.log" 2>&1 || { echo "FAIL remove"; FAILED=1; }
check "Dienst weg nach remove" "! systemctl is-active --quiet multi-gpt"
check ".env bleibt nach remove" "test -f /etc/multi-gpt/.env"
check "Medien bleiben nach remove" "test -f /var/lib/multi-gpt/media/marker.txt"
check "Datenbank bleibt nach remove" "su postgres -c \"psql -Atc 'select 1' multi-gpt\""

echo "== apt purge"
ct "apt-get purge -y -qq multi-gpt" >"$WORK/purge.log" 2>&1 || { echo "FAIL purge"; FAILED=1; }
check ".env weg nach purge" "! test -e /etc/multi-gpt/.env"
check "nginx-Site weg nach purge" "! test -e /etc/nginx/sites-enabled/multi-gpt"
check "nginx läuft weiter" "nginx -t"
check "Medien bleiben nach purge" "test -f /var/lib/multi-gpt/media/marker.txt"
check "Datenbank bleibt nach purge" "su postgres -c \"psql -Atc 'select 1' multi-gpt\""

echo "== Neuinstallation nach purge"
ct "apt-get install -y -qq /debs/new.deb" >"$WORK/reinstall.log" 2>&1 \
    || { echo "FAIL Neuinstallation"; tail -30 "$WORK/reinstall.log"; FAILED=1; }
if wait_active; then echo "PASS Dienst nach Neuinstallation"; else echo "FAIL Dienst nach Neuinstallation"; FAILED=1; fi

[ "$FAILED" = 0 ] && echo "== ALLES GRÜN" || echo "== FEHLER (siehe FAIL)"
exit "$FAILED"
