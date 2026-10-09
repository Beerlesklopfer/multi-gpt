#!/bin/bash
PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
T=$(cd "$(dirname "$0")" && pwd); FAILS=0
check() { local d="$1"; shift; if "$@"; then echo "  ok   $d"; else echo "  FAIL $d"; FAILS=$((FAILS+1)); fi; }
setup() {
  R=$(mktemp -d "$T/croot.XXXX"); mkdir -p "$R/bin" "$R/db" "$R/etc/multi-gpt/nginx"
  cat > "$R/confmodule" <<S
db_get() { RET=\$(cat "$R/db/\$(echo \$1 | tr / _)" 2>/dev/null) || { RET=""; return 10; }; }
db_set() { printf '%s' "\$2" > "$R/db/\$(echo \$1 | tr / _)"; }
db_input() { echo "\$*" >> "$R/asked"; }
db_go() { :; }
S
  cat > "$R/bin/hostname" <<'S'
#!/bin/sh
case "$1" in -I) echo "192.168.24.250 172.17.0.1 fd00::5 " ;; -f) echo "nas.intranet.example" ;; *) echo "nas" ;; esac
S
  chmod +x "$R/bin/hostname"
  sed -e "s|^\. /usr/share/debconf/confmodule|. $R/confmodule|" -e "s|^ENV_FILE=/etc|ENV_FILE=$R/etc|" \
      -e "s|^NGINX_GEN_DIR=/etc|NGINX_GEN_DIR=$R/etc|" "$PROJ/debian/multi-gpt.config" > "$R/config"
}
run() { PATH="$R/bin:$PATH" dash "$R/config"; }
val() { cat "$R/db/multi-gpt_$1" 2>/dev/null; }

echo "1: Neuinstallation"
setup; run
check "Vorschlag FQDN + Kurzname" test "$(val server_name)" = "nas.intranet.example nas"
check "server_name und Zertifikat gefragt" grep -q "high multi-gpt/tls_certificate" "$R/asked"
check "Schlüssel nicht gefragt (kein Zertifikat)" bash -c "! grep -q tls_key '$R/asked'"

echo "2: Upgrade von 0.1.0 mit Handeinträgen in ALLOWED_HOSTS"
setup
printf 'ALLOWED_HOSTS=localhost,127.0.0.1,192.168.24.250,nas.intranet.example,multigpt.intern,10.8.0.1,*\n' > "$R/etc/multi-gpt/.env"
run
check "Handeinträge übernommen, eigene IP nicht" test "$(val server_name)" = "nas.intranet.example nas multigpt.intern 10.8.0.1"

echo "3: Reconfigure: Werte aus erzeugter Konfiguration"
setup
printf 'server_name chat.example;\n' > "$R/etc/multi-gpt/nginx/http.conf"
printf 'server_name chat.example;\nssl_certificate /etc/ssl/certs/chat.pem;\nssl_certificate_key /etc/ssl/private/chat.key;\n' > "$R/etc/multi-gpt/nginx/https.conf"
printf 'ALLOWED_HOSTS=localhost,127.0.0.1,chat.example,192.168.24.250,172.17.0.1\n' > "$R/etc/multi-gpt/.env"
run
check "server_name aus http.conf" test "$(val server_name)" = "chat.example"
check "Zertifikat" test "$(val tls_certificate)" = /etc/ssl/certs/chat.pem
check "Schlüssel" test "$(val tls_key)" = /etc/ssl/private/chat.key
check "Schlüssel gefragt" grep -q "high multi-gpt/tls_key" "$R/asked"

echo "4: snakeoil in https.conf -> leerer Vorschlag"
setup
printf 'server_name a;\n' > "$R/etc/multi-gpt/nginx/http.conf"
printf 'ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;\nssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;\n' > "$R/etc/multi-gpt/nginx/https.conf"
run
check "Zertifikat leer" test -z "$(val tls_certificate)"

echo "5: vorbelegter debconf-Wert (preseed) ohne erzeugte Dateien"
setup; printf 'pre.example' > "$R/db/multi-gpt_server_name"; run
check "preseed bleibt" test "$(val server_name)" = "pre.example"
echo "6: Platzhalter nicht doppelt"
setup
printf 'server_name *.wild.example;\n' > "$R/etc/multi-gpt/nginx/http.conf"
printf 'ALLOWED_HOSTS=localhost,127.0.0.1,.wild.example,192.168.24.250\n' > "$R/etc/multi-gpt/.env"
run
check "kein .wild.example zusätzlich" test "$(val server_name)" = "*.wild.example"
echo "Fehlschläge: $FAILS"; [ -n "$KEEP" ] || rm -rf "$T"/croot.*; exit $FAILS
