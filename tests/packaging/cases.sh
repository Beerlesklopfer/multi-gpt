#!/bin/bash
source "$(dirname "$0")/harness.sh"

echo "A: Neuinstallation, default-Site unverändert"
new_root; run_setup
check "Lauf ohne Fehler" test $? = 0
check "default deaktiviert" test ! -e "$R/etc/nginx/sites-enabled/default"
check "Vermerk gesetzt" test -e "$R/var/lib/multi-gpt/nginx-default-site-disabled"
check "Site aktiviert" test -L "$R/etc/nginx/sites-enabled/multi-gpt"
check "http default_server" grep -q "listen 80 default_server;" "$R/etc/multi-gpt/nginx/http.conf"
check "https default_server" grep -q "listen 443 ssl default_server;" "$R/etc/multi-gpt/nginx/https.conf"
check "server_name" grep -q "server_name nas.intranet.example nas;" "$R/etc/multi-gpt/nginx/https.conf"
check "snakeoil" grep -q "ssl-cert-snakeoil.pem;" "$R/etc/multi-gpt/nginx/https.conf"
check "snakeoil-Warnung" grep -q "selbstsignierte snakeoil" "$R/out"
check "kein HSTS bei snakeoil" bash -c "! grep -q Strict-Transport '$R/etc/multi-gpt/nginx/headers.conf'"
check "client_max_body_size 35m" grep -q "client_max_body_size 35m;" "$R/etc/multi-gpt/nginx/https.conf"
check "X-Accel-location" grep -q "alias $R/media/;" "$R/etc/multi-gpt/nginx/https.conf"
check "mcp.conf: base64-Upload 44m" grep -qx "client_max_body_size 44m;" "$R/etc/multi-gpt/nginx/mcp.conf"
check "Site: /mcp/ ungepuffert" bash -c "grep -A6 'location ~ ^/mcp/' '$R/etc/nginx/sites-available/multi-gpt' | grep -q 'proxy_buffering off;'"
check "Site bindet mcp.conf ein" grep -q "include $R/etc/multi-gpt/nginx/mcp.conf;" "$R/etc/nginx/sites-available/multi-gpt"
check "Site: /mcp/sse ungepuffert" bash -c "grep -A8 'location ~ ^/mcp/(sse|messages)' '$R/etc/nginx/sites-available/multi-gpt' | grep -q 'proxy_buffering off;'"
check "Site: /mcp/sse read_timeout 3600s" bash -c "grep -A8 'location ~ ^/mcp/(sse|messages)' '$R/etc/nginx/sites-available/multi-gpt' | grep -q 'proxy_read_timeout 3600s;'"
check "Site: /mcp/sse bindet mcp.conf ein" bash -c "grep -A8 'location ~ ^/mcp/(sse|messages)' '$R/etc/nginx/sites-available/multi-gpt' | grep -q 'mcp.conf;'"
check "upstream Runner-Dienst 8001" bash -c "grep -A1 'upstream multi_gpt_runner {' '$R/etc/multi-gpt/nginx/upstream.conf' | grep -q 'server 127.0.0.1:8001;'"
check "Runner ohne keepalive" bash -c "! sed -n '/multi_gpt_runner/,/}/p' '$R/etc/multi-gpt/nginx/upstream.conf' | grep -q keepalive"
check "MULTI_GPT_RUNNER_BIND nicht eingetragen" bash -c "! grep -q '^MULTI_GPT_RUNNER_BIND=' '$R/etc/multi-gpt/.env'"
check "Site: location = /runner/ws" bash -c "grep -A12 'location = /runner/ws {' '$R/etc/nginx/sites-available/multi-gpt' | grep -q 'proxy_pass http://multi_gpt_runner;'"
for d in 'proxy_set_header Upgrade \$http_upgrade;' 'proxy_set_header Connection \"upgrade\";' 'proxy_set_header Host \$host;' \
         'proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;' 'proxy_set_header X-Forwarded-Proto \$scheme;' \
         'proxy_http_version 1.1;' 'proxy_buffering off;' 'proxy_read_timeout 120s;'; do
  check "Site: /runner/ws $d" bash -c "sed -n '/location = \/runner\/ws {/,/}/p' '$R/etc/nginx/sites-available/multi-gpt' | grep -qF \"$d\""
done
check "ALLOWED_HOSTS" test "$(envv ALLOWED_HOSTS)" = "localhost,127.0.0.1,nas.intranet.example,nas,192.168.24.250,172.17.0.1"
check "CSRF" test "$(envv CSRF_TRUSTED_ORIGINS)" = "https://nas.intranet.example,https://nas,https://192.168.24.250,https://172.17.0.1"
check "SECURE_COOKIES True" test "$(envv SECURE_COOKIES)" = True
check "AXES_PROXY_COUNT 1" test "$(envv AXES_PROXY_COUNT)" = 1
check "RUNNER_TLS_CERT = snakeoil (Fingerprint für Runner)" test "$(envv RUNNER_TLS_CERT)" = "$R/etc/ssl/certs/ssl-cert-snakeoil.pem"
check "nginx -t ok" nginx_ok
check "reload" grep -q "deb-systemd-invoke reload nginx.service" "$R/calls"
check "keine .dpkg-old" bash -c "! ls '$R'/etc/multi-gpt/nginx/*.dpkg-old 2>/dev/null"
check "local/ angelegt" test -d "$R/etc/multi-gpt/nginx/local"
cp -r "$R" "$T/last-good-root" 2>/dev/null || true

echo "A2: postrm remove/purge"
run_postrm remove
check "Site-Symlink weg" test ! -e "$R/etc/nginx/sites-enabled/multi-gpt"
check "default wiederhergestellt" test -L "$R/etc/nginx/sites-enabled/default"
check "Vermerk weg" test ! -e "$R/var/lib/multi-gpt/nginx-default-site-disabled"
check "nginx -t ok nach remove" nginx_ok
run_postrm purge
check "Generiertes weg (purge)" test ! -e "$R/etc/multi-gpt/nginx"

echo "B: default-Site angepasst (md5 weicht ab)"
new_root; echo 0000 > "$R/dpkg-md5"; run_setup
check "default bleibt" test -L "$R/etc/nginx/sites-enabled/default"
check "kein Vermerk" test ! -e "$R/var/lib/multi-gpt/nginx-default-site-disabled"
check "Hinweis angepasst" grep -q "ist angepasst" "$R/out"
check "http ohne default_server" grep -qx "listen 80;" "$R/etc/multi-gpt/nginx/http.conf"
check "https mit default_server (default hat 443 nicht)" grep -q "listen 443 ssl default_server;" "$R/etc/multi-gpt/nginx/https.conf"
check "Hinweis anderer default_server" grep -q "Port 80 bereits default_server" "$R/out"
check "nginx -t ok" nginx_ok
check "Site aktiviert" test -L "$R/etc/nginx/sites-enabled/multi-gpt"

echo "C: andere eigene Site ist default_server auf 443"
new_root
cat > "$R/etc/nginx/sites-available/other" <<NG
server {
    listen 443 ssl default_server;  # eigene Site
    # listen 80 default_server;
    server_name other.example;
    ssl_certificate $R/etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key $R/etc/ssl/private/ssl-cert-snakeoil.key;
    return 204;
}
NG
ln -s ../sites-available/other "$R/etc/nginx/sites-enabled/other"
run_setup
check "default deaktiviert (unverändert)" test ! -e "$R/etc/nginx/sites-enabled/default"
check "http default_server" grep -q "listen 80 default_server;" "$R/etc/multi-gpt/nginx/http.conf"
check "https ohne default_server" grep -qx "listen 443 ssl;" "$R/etc/multi-gpt/nginx/https.conf"
check "Hinweis 443" grep -q "Port 443 bereits default_server" "$R/out"
check "fremde Site unverändert" grep -q "listen 443 ssl default_server;" "$R/etc/nginx/sites-available/other"
check "nginx -t ok" nginx_ok

echo "D: Upgrade von 0.1.0 (LAN-Bind, eigene Hosts)"
new_root
cat > "$R/etc/multi-gpt/.env" <<E
SECRET_KEY=x
ALLOWED_HOSTS=localhost,127.0.0.1,192.168.24.250,nas.intranet.example,multigpt.intern
CSRF_TRUSTED_ORIGINS=http://192.168.24.250:8123
SECURE_COOKIES=False
LOG_LEVEL=INFO
#AXES_PROXY_COUNT=1
DOCUMENT_MAX_UPLOAD_MB=50
MULTI_GPT_BIND=192.168.24.250:8123
MULTI_GPT_RUNNER_BIND=0.0.0.0:8124
E
chmod 640 "$R/etc/multi-gpt/.env"
run_setup "nas.intranet.example nas multigpt.intern"
check "Bind lokal, Port behalten" test "$(envv MULTI_GPT_BIND)" = 127.0.0.1:8123
check "Runner-Bind lokal, Port behalten" test "$(envv MULTI_GPT_RUNNER_BIND)" = 127.0.0.1:8124
check "Hinweis Runner-Bind" grep -q "MULTI_GPT_RUNNER_BIND 0.0.0.0:8124 -> 127.0.0.1:8124" "$R/out"
check "upstream Runner-Port" grep -q "server 127.0.0.1:8124;" "$R/etc/multi-gpt/nginx/upstream.conf"
check "Hinweis Bind" grep -q "MULTI_GPT_BIND 192.168.24.250:8123 -> 127.0.0.1:8123" "$R/out"
check "upstream Port" grep -q "server 127.0.0.1:8123;" "$R/etc/multi-gpt/nginx/upstream.conf"
check "client_max_body_size 60m" grep -q "client_max_body_size 60m;" "$R/etc/multi-gpt/nginx/https.conf"
check "mcp.conf 77m (50 MB als base64)" grep -qx "client_max_body_size 77m;" "$R/etc/multi-gpt/nginx/mcp.conf"
check "SECURE_COOKIES True" test "$(envv SECURE_COOKIES)" = True
check "AXES aktiviert (statt Kommentar)" bash -c "grep -qx 'AXES_PROXY_COUNT=1' '$R/etc/multi-gpt/.env' && ! grep -q '^#AXES' '$R/etc/multi-gpt/.env'"
check "CSRF https" test "$(envv CSRF_TRUSTED_ORIGINS)" = "https://nas.intranet.example,https://nas,https://multigpt.intern,https://192.168.24.250,https://172.17.0.1"
check "Rechte .env 640" test "$(stat -c %a "$R/etc/multi-gpt/.env")" = 640
check "nginx -t ok" nginx_ok

echo "E: erneuter Lauf respektiert SECURE_COOKIES=False von Hand"
sed -i 's/^SECURE_COOKIES=True/SECURE_COOKIES=False/' "$R/etc/multi-gpt/.env"
run_setup "nas.intranet.example"
check "SECURE_COOKIES bleibt False" test "$(envv SECURE_COOKIES)" = False
check "server_name aktualisiert" grep -q "server_name nas.intranet.example;" "$R/etc/multi-gpt/nginx/http.conf"
check "keine .dpkg-old" bash -c "! ls '$R'/etc/multi-gpt/nginx/*.dpkg-old 2>/dev/null"

echo "F: nginx -t schlägt fehl (kaputtes Zertifikat), Neuinstallation"
new_root
echo "kein pem" > "$R/bad.pem"; echo "kein key" > "$R/bad.key"
run_setup "nas" "$R/bad.pem" "$R/bad.key"
check "Lauf endet trotzdem mit 0" test $? = 0
check "Fehlermeldung" grep -q "FEHLER: nginx -t" "$R/out"
check "Site NICHT aktiviert" test ! -e "$R/etc/nginx/sites-enabled/multi-gpt"
check "default wiederhergestellt" test -L "$R/etc/nginx/sites-enabled/default"
check "Vermerk entfernt" test ! -e "$R/var/lib/multi-gpt/nginx-default-site-disabled"
check ".failed vorhanden" test -e "$R/etc/multi-gpt/nginx/https.conf.failed"
check "mcp.conf ebenfalls zurückgenommen" test -e "$R/etc/multi-gpt/nginx/mcp.conf.failed"
check "kein https.conf" test ! -e "$R/etc/multi-gpt/nginx/https.conf"
check "kein reload" bash -c "! grep -q reload '$R/calls' 2>/dev/null"
check "nginx -t ok (alter Zustand)" nginx_ok

echo "F2: nginx -t schlägt beim Upgrade fehl -> vorige Dateien zurück"
new_root; run_setup "nas"
cp "$R/etc/multi-gpt/nginx/https.conf" "$R/https.before"
echo "kein pem" > "$R/bad.pem"; echo "kein key" > "$R/bad.key"
run_setup "nas" "$R/bad.pem" "$R/bad.key"
check "Site bleibt aktiv" test -L "$R/etc/nginx/sites-enabled/multi-gpt"
check "https.conf wie vorher" cmp -s "$R/https.before" "$R/etc/multi-gpt/nginx/https.conf"
check "mcp.conf wiederhergestellt" test -e "$R/etc/multi-gpt/nginx/mcp.conf"
check "Hinweis wiederhergestellt" grep -q "vorige Konfiguration wurde wiederhergestellt" "$R/out"
check "nginx -t ok" nginx_ok

echo "G: eigenes Zertifikat -> HSTS"
new_root
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 -subj /CN=nas.intranet.example \
   -addext "subjectAltName=DNS:nas.intranet.example" -keyout "$R/own.key" -out "$R/own.pem" 2>/dev/null
run_setup "nas.intranet.example" "$R/own.pem" "$R/own.key"
check "HSTS" grep -q 'Strict-Transport-Security "max-age=63072000"' "$R/etc/multi-gpt/nginx/headers.conf"
check "eigenes Zertifikat" grep -q "ssl_certificate $R/own.pem;" "$R/etc/multi-gpt/nginx/https.conf"
check "keine snakeoil-Warnung" bash -c "! grep -q snakeoil-Zertifikat '$R/out'"
check "kein RUNNER_TLS_CERT bei eigenem Zertifikat" bash -c "! grep -q '^RUNNER_TLS_CERT=' '$R/etc/multi-gpt/.env'"
check "nginx -t ok" nginx_ok

echo "G3: erst snakeoil, dann eigenes Zertifikat -> RUNNER_TLS_CERT entfernt; eigener Wert bleibt"
new_root; run_setup "nas.intranet.example"
check "snakeoil eingetragen" test "$(envv RUNNER_TLS_CERT)" = "$R/etc/ssl/certs/ssl-cert-snakeoil.pem"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 -subj /CN=nas.intranet.example \
   -keyout "$R/own.key" -out "$R/own.pem" 2>/dev/null
run_setup "nas.intranet.example" "$R/own.pem" "$R/own.key"
check "Eintrag entfernt" bash -c "! grep -q '^RUNNER_TLS_CERT=' '$R/etc/multi-gpt/.env'"
check "Hinweis" grep -q "RUNNER_TLS_CERT entfernt" "$R/out"
echo "RUNNER_TLS_CERT=/srv/eigene-ca.pem" >> "$R/etc/multi-gpt/.env"
run_setup "nas.intranet.example"
check "eigener Wert bleibt (auch bei snakeoil)" test "$(envv RUNNER_TLS_CERT)" = /srv/eigene-ca.pem

echo "G2: Zertifikat angegeben, Schlüssel fehlt -> snakeoil"
new_root; run_setup "nas" "$R/etc/ssl/certs/ssl-cert-snakeoil.pem" "" ; run_setup "nas" "/nicht/da.pem" "/nicht/da.key"
check "Warnung fehlt" grep -q "Zertifikat: /nicht/da.pem fehlt" "$R/out"
check "snakeoil" grep -q "ssl-cert-snakeoil.pem;" "$R/etc/multi-gpt/nginx/https.conf"

echo "H: ungültige Namen"
new_root; run_setup 'bad;name "x" *.wild.example .dot.example _'
check "nur gültige" grep -qx "server_name \*.wild.example .dot.example;" "$R/etc/multi-gpt/nginx/http.conf"
check "ALLOWED_HOSTS Wildcards" test "$(envv ALLOWED_HOSTS)" = "localhost,127.0.0.1,.wild.example,.dot.example,192.168.24.250,172.17.0.1"
check "CSRF Wildcards" test "$(envv CSRF_TRUSTED_ORIGINS)" = "https://*.wild.example,https://*.dot.example,https://192.168.24.250,https://172.17.0.1"
new_root; run_setup ''
check "Fallback Hostname" grep -qx "server_name nas.intranet.example nas;" "$R/etc/multi-gpt/nginx/http.conf"
check "nginx -t ok" nginx_ok

echo "J: Verwalter aktiviert default wieder -> kein erneutes Deaktivieren"
new_root; run_setup
ln -s "$R/etc/nginx/sites-available/default" "$R/etc/nginx/sites-enabled/default"
run_setup
check "default bleibt aktiv" test -L "$R/etc/nginx/sites-enabled/default"
check "http ohne default_server" grep -qx "listen 80;" "$R/etc/multi-gpt/nginx/http.conf"
check "nginx -t ok" nginx_ok
run_postrm remove
check "remove: default bleibt" test -L "$R/etc/nginx/sites-enabled/default"

echo "K: MULTI_GPT_SKIP_NGINX=1"
new_root; MULTI_GPT_SKIP_NGINX=1 run_setup
check "Site nicht aktiviert" test ! -e "$R/etc/nginx/sites-enabled/multi-gpt"
check "default unverändert" test -L "$R/etc/nginx/sites-enabled/default"
check ".env trotzdem gesetzt" test "$(envv SECURE_COOKIES)" = True

echo "M: Runner-Bind kollidiert mit gunicorn bzw. ist ungültig"
new_root; sed -i 's/^MULTI_GPT_BIND=.*/MULTI_GPT_BIND=127.0.0.1:8001/' "$R/etc/multi-gpt/.env"
run_setup
check "Runner weicht auf 8002 aus" grep -q "server 127.0.0.1:8002;" "$R/etc/multi-gpt/nginx/upstream.conf"
check "Runner-Bind eingetragen" test "$(envv MULTI_GPT_RUNNER_BIND)" = 127.0.0.1:8002
check "Warnung Port" grep -q "Runner-Dienst und gunicorn auf Port 8001" "$R/out"
check "nginx -t ok" nginx_ok
new_root; echo 'MULTI_GPT_RUNNER_BIND=quatsch' >> "$R/etc/multi-gpt/.env"
run_setup
check "ungültig -> Warnung" grep -q "MULTI_GPT_RUNNER_BIND 'quatsch' ist ungültig" "$R/out"
check "ungültig -> 8001" test "$(envv MULTI_GPT_RUNNER_BIND)" = 127.0.0.1:8001
check "nginx -t ok" nginx_ok

echo "L: vorab angelegte .env ohne Pflichtschlüssel (z. B. nur RAG_SOURCE_ROOTS)"
new_root
printf 'RAG_SOURCE_ROOTS="/srv/n8n"\nSECRET_KEY=\nFIELD_ENCRYPTION_KEY=vorhanden-bleibt=\n' > "$R/etc/multi-gpt/.env"
dash -c '. "$1"; multi_gpt_ensure_env_keys' sh "$R/postinst.funcs" > "$R/out" 2>&1
check "Lauf ohne Fehler" test $? -eq 0
check "SECRET_KEY ergänzt" sh -c "grep -Eq '^SECRET_KEY=.{40,}' '$R/etc/multi-gpt/.env'"
check "leere SECRET_KEY-Zeile entfernt" sh -c "[ \$(grep -c '^SECRET_KEY=' '$R/etc/multi-gpt/.env') -eq 1 ]"
check "FIELD_ENCRYPTION_KEY unverändert" test "$(envv FIELD_ENCRYPTION_KEY)" = "vorhanden-bleibt="
check "DATABASE_URL ergänzt" test "$(envv DATABASE_URL)" = "postgres:///multi-gpt"
check "MEDIA_ROOT ergänzt" test "$(envv MEDIA_ROOT)" = "/var/lib/multi-gpt/media"
check "RAG_SOURCE_ROOTS bleibt" grep -q '^RAG_SOURCE_ROOTS="/srv/n8n"' "$R/etc/multi-gpt/.env"
check "Hinweis ausgegeben" grep -q "fehlten Pflichtschlüssel" "$R/out"
dash -c '. "$1"; multi_gpt_ensure_env_keys' sh "$R/postinst.funcs" > "$R/out2" 2>&1
check "zweiter Lauf ändert nichts" sh -c "! grep -q fehlten '$R/out2'"

echo
echo "Fehlschläge: $FAILS"
rm -rf "$T"/root.* "$T/last-good-root"
exit $FAILS
