#!/bin/bash
# Stub-Testumgebung für die nginx-Logik aus debian/multi-gpt.postinst/postrm.
# Nutzung: source harness.sh; new_root; run_setup; ...
set -u
PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
T=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FAILS=0
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; FAILS=$((FAILS+1)); }
check() { local d="$1"; shift; if "$@"; then ok "$d"; else fail "$d"; fi; }

new_root() {
  R=$(mktemp -d "$T/root.XXXX")
  mkdir -p "$R/etc/nginx/sites-available" "$R/etc/nginx/sites-enabled" "$R/etc/nginx/conf.d" \
           "$R/etc/multi-gpt" "$R/var/lib/multi-gpt" "$R/etc/ssl/certs" "$R/etc/ssl/private" \
           "$R/bin" "$R/static" "$R/log" "$R/media"
  cat > "$R/etc/nginx/nginx.conf" <<NG
worker_processes 1;
error_log $R/log/error.log;
pid $R/nginx.pid;
events { worker_connections 64; }
http {
    access_log off;
    include /etc/nginx/mime.types;
    include $R/etc/nginx/conf.d/*.conf;
    include $R/etc/nginx/sites-enabled/*;
}
NG
  # Site aus dem Paket, Pfade in die Testwurzel umgebogen
  sed -e "s|/etc/multi-gpt/|$R/etc/multi-gpt/|g" -e "s|/usr/share/python/multi-gpt/static/|$R/static/|" \
      "$PROJ/deploy/nginx/multi-gpt" > "$R/etc/nginx/sites-available/multi-gpt"
  cp -r /etc/nginx/snippets "$R/etc/nginx/"; sed -i "s|/etc/nginx/|$R/etc/nginx/|g" "$R"/etc/nginx/snippets/*
  cp "$T/data/nginx-default-site" "$R/etc/nginx/sites-available/default"
  ln -s "$R/etc/nginx/sites-available/default" "$R/etc/nginx/sites-enabled/default"
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=snakeoil \
     -keyout "$R/etc/ssl/private/ssl-cert-snakeoil.key" -out "$R/etc/ssl/certs/ssl-cert-snakeoil.pem" 2>/dev/null
  printf 'SECRET_KEY=x\nALLOWED_HOSTS=localhost,127.0.0.1\nCSRF_TRUSTED_ORIGINS=\nMEDIA_ROOT=%s/media\nSECURE_COOKIES=False\n#AXES_PROXY_COUNT=1\nMULTI_GPT_BIND=127.0.0.1:8000\n' "$R" > "$R/etc/multi-gpt/.env"
  # Stubs
  DEFAULT_MD5=f1f26aef86f90a484f3a2f46ccc46ff6  # md5 laut dpkg (nginx-common 1.26.3)
  echo "$DEFAULT_MD5" > "$R/dpkg-md5"
  cat > "$R/bin/dpkg-query" <<S
#!/bin/sh
echo " $R/etc/nginx/sites-available/default \$(cat $R/dpkg-md5)"
echo " /etc/default/nginx 596fbc1f5712caaa02e8c799a4e2a01f"
S
  cat > "$R/bin/nginx" <<S
#!/bin/sh
exec unshare -rn /usr/sbin/nginx -c $R/etc/nginx/nginx.conf -p $R/ -e $R/log/error.log "\$@"
S
  cat > "$R/bin/hostname" <<'S'
#!/bin/sh
case "$1" in -I) echo "192.168.24.250 172.17.0.1 fd00::5 " ;; -f) echo "nas.intranet.example" ;; *) echo "nas" ;; esac
S
  cat > "$R/bin/systemctl" <<S
#!/bin/sh
echo "systemctl \$*" >> $R/calls; exit 0
S
  cat > "$R/bin/deb-systemd-invoke" <<S
#!/bin/sh
echo "deb-systemd-invoke \$*" >> $R/calls
S
  printf '#!/bin/sh\necho make-ssl-cert >> %s/calls\n' "$R" > "$R/bin/make-ssl-cert"
  chmod +x "$R"/bin/*
  # Testversionen der Maintainer-Skripte (nur Funktionen, Pfade umgebogen)
  for s in postinst postrm; do
    sed -e "s|^ENV_FILE=/etc/multi-gpt/.env|ENV_FILE=$R/etc/multi-gpt/.env|" \
        -e "s|^NGINX_DIR=/etc/nginx|NGINX_DIR=$R/etc/nginx|" \
        -e "s|^NGINX_GEN_DIR=/etc/multi-gpt/nginx|NGINX_GEN_DIR=$R/etc/multi-gpt/nginx|" \
        -e "s|^DEFAULT_SITE_MARK=/var/lib/multi-gpt/|DEFAULT_SITE_MARK=$R/var/lib/multi-gpt/|" \
        -e "s|^SNAKEOIL_CERT=/etc/ssl/|SNAKEOIL_CERT=$R/etc/ssl/|" \
        -e "s|^SNAKEOIL_KEY=/etc/ssl/|SNAKEOIL_KEY=$R/etc/ssl/|" \
        -e "s|-d /run/systemd/system|-d /|" \
        -e "s|rm -f /etc/multi-gpt/.env /etc/multi-gpt/.env.dpkg-new|rm -f $R/etc/multi-gpt/.env|" \
        -e "s|rmdir --ignore-fail-on-non-empty /etc/multi-gpt|rmdir --ignore-fail-on-non-empty $R/etc/multi-gpt|" \
        "$PROJ/debian/multi-gpt.$s" | sed -n '/^case "\$1" in$/q; /^if \[ "\$1" = "configure" \]; then$/q; p' > "$R/$s.funcs"
  done
  grep -q "/etc/multi-gpt/nginx\b" "$R/postinst.funcs" && grep -n "^NGINX_GEN_DIR" "$R/postinst.funcs" >/dev/null
}

# Setup wie im postinst "configure" (nur nginx-Teil), Antworten als Variablen.
run_setup() {
  PATH="$R/bin:$PATH" SERVER_NAME_ANSWER="${1-nas.intranet.example nas}" TLS_CERT_ANSWER="${2-}" TLS_KEY_ANSWER="${3-}" \
    dash -c '. "$1"; multi_gpt_setup_nginx' sh "$R/postinst.funcs" > "$R/out" 2>&1
  local rc=$?; [ "${VERBOSE:-0}" = 1 ] && cat "$R/out"; return $rc
}
run_postrm() {
  PATH="$R/bin:$PATH" dash -c '. "$1"; case "$2" in remove) multi_gpt_remove_site ;; purge) multi_gpt_remove_site; rm -rf "$NGINX_GEN_DIR" ;; esac' sh "$R/postrm.funcs" "$1" > "$R/out.postrm" 2>&1
}
nginx_ok() { "$R/bin/nginx" -t >/dev/null 2>&1; }
gen() { cat "$R/etc/multi-gpt/nginx/$1"; }
envv() { sed -n "s/^$1=//p" "$R/etc/multi-gpt/.env" | tail -n1; }
