#!/bin/bash
# Läuft in: unshare -rn  (eigenes Netz, root im Namensraum)
source "$(dirname "$0")/harness.sh"
ip link set lo up
new_root
printf 'MULTI_GPT_BIND=192.168.1.5:8123\n' >> "$R/etc/multi-gpt/.env"
# nginx im Namensraum direkt (Wrapper ohne zweites unshare)
cat > "$R/bin/nginx" <<S
#!/bin/sh
exec /usr/sbin/nginx -c $R/etc/nginx/nginx.conf -p $R/ -e $R/log/error.log "\$@"
S
sed -i "1i user root root;" "$R/etc/nginx/nginx.conf"
mkdir -p "$R/tmp"
sed -i "s|    access_log off;|    access_log off;\n    client_body_temp_path $R/tmp/body; proxy_temp_path $R/tmp/proxy; fastcgi_temp_path $R/tmp/fcgi; uwsgi_temp_path $R/tmp/uwsgi; scgi_temp_path $R/tmp/scgi;|" "$R/etc/nginx/nginx.conf"
run_setup "nas.intranet.example nas"
mkdir -p "$R/static/css" "$R/media/documents/1"
echo "body{}" > "$R/static/css/app.0123456789ab.css"; gzip -k "$R/static/css/app.0123456789ab.css"
echo "plain" > "$R/static/css/app.css"
echo "GEHEIM-INHALT" > "$R/media/documents/1/abc.txt"
python3 "$T/backend.py" 8123 & BP=$!
trap 'kill $BP 2>/dev/null; "$R/bin/nginx" -s quit 2>/dev/null' EXIT
"$R/bin/nginx" || { cat "$R/log/error.log"; exit 1; }
sleep 0.5
C="curl -sk --resolve nas.intranet.example:443:127.0.0.1 --resolve nas.intranet.example:80:127.0.0.1"
U=https://nas.intranet.example

r=$($C -o /dev/null -w '%{http_code} %{redirect_url}' http://nas.intranet.example/foo?x=1)
check "HTTP -> 301 https ($r)" test "$r" = "301 https://nas.intranet.example/foo?x=1"
r=$($C -o /dev/null -w '%{http_code}' https://127.0.0.1/ )
check "Aufruf per IP (default_server) -> 200 ($r)" test "$r" = 200
j=$($C -H 'X-Forwarded-For: 6.6.6.6' -H 'X-Forwarded-Proto: http' $U/chat/)
check "X-Forwarded-Proto https" python3 -c "import json,sys; h=json.loads(sys.argv[1])['headers']; assert h['X-Forwarded-Proto']=='https', h" "$j"
check "X-Forwarded-For angehängt" python3 -c "import json,sys; h=json.loads(sys.argv[1])['headers']; assert h['X-Forwarded-For']=='6.6.6.6, 127.0.0.1', h" "$j"
check "Host" python3 -c "import json,sys; h=json.loads(sys.argv[1])['headers']; assert h['Host']=='nas.intranet.example', h" "$j"
hd=$($C -D - -o /dev/null $U/chat/)
check "nosniff genau einmal" test "$(grep -ci '^x-content-type-options' <<<"$hd")" = 1
check "Referrer-Policy genau einmal" test "$(grep -ci '^referrer-policy' <<<"$hd")" = 1
check "kein HSTS (snakeoil)" bash -c "! grep -qi strict-transport <<<'$hd'"
check "HTTP/2" bash -c "$C -o /dev/null -w '%{http_version}' $U/chat/ | grep -qx 2"
hd=$($C -D - -o /dev/null $U/static/css/app.0123456789ab.css)
check "static gehasht: immutable" grep -qi "cache-control: public, max-age=31536000, immutable" <<<"$hd"
check "static gehasht: nosniff" grep -qi "x-content-type-options: nosniff" <<<"$hd"
hd=$($C -H 'Accept-Encoding: gzip' -D - -o /dev/null $U/static/css/app.0123456789ab.css)
check "gzip_static" grep -qi "content-encoding: gzip" <<<"$hd"
hd=$($C -D - -o /dev/null $U/static/css/app.css)
check "static ungehasht: 1h" grep -qi "cache-control: public, max-age=3600" <<<"$hd"
r=$($C --path-as-is -o /dev/null -w '%{http_code}' $U/static/../../etc/passwd)
check "kein Pfadausbruch ($r)" test "$r" = 400 -o "$r" = 404
r=$($C -o /dev/null -w '%{http_code}' $U/_protected/media/documents/1/abc.txt)
check "interne location von außen 404 ($r)" test "$r" = 404
b=$($C $U/download)
check "X-Accel-Redirect liefert Datei" test "$b" = "GEHEIM-INHALT"
# SSE: erste Zeile muss nach < 1 s ankommen (nicht erst nach 3 s gepuffert)
lag=$($C -N $U/api/conversations/1/messages/ | python3 -c "
import sys, time
lags = []
for line in sys.stdin:
    if line.startswith('data:'):
        lags.append(time.time() - float(line.split()[2]))
print(max(lags))")
check "SSE ungepuffert (max. Verzögerung je Event ${lag}s)" python3 -c "assert $lag < 0.5"
dd if=/dev/zero of="$R/30m" bs=1M count=30 status=none
r=$($C -o /dev/null -w '%{http_code}' -X POST --data-binary @"$R/30m" -H 'Content-Type: application/octet-stream' $U/api/upload)
check "30 MB Upload erlaubt ($r)" test "$r" = 200
dd if=/dev/zero of="$R/40m" bs=1M count=40 status=none
r=$($C -o /dev/null -w '%{http_code}' -X POST --data-binary @"$R/40m" -H 'Content-Type: application/octet-stream' $U/api/upload)
check "40 MB Upload abgewiesen ($r)" test "$r" = 413
tls=$(echo | openssl s_client -connect 127.0.0.1:443 -servername nas.intranet.example -tls1_1 2>&1 | grep -c "Cipher is (NONE)\|alert\|no protocols")
check "TLS 1.1 abgelehnt" test "$tls" -ge 1
tls=$(echo | openssl s_client -connect 127.0.0.1:443 -servername nas.intranet.example 2>/dev/null | grep -E "^(New|Negotiated|Server Temp Key)|Protocol" | head -3)
echo "    TLS: $(tr '\n' ' ' <<<"$tls")"
"$R/bin/nginx" -s quit; kill $BP
echo "Fehlschläge: $FAILS"
rm -rf "$T"/root.*
exit $FAILS
