# Tests der Paket-Skripte (nginx, debconf)

Stub-Tests für `debian/multi-gpt.postinst`, `postrm` und `config`. Sie laufen in einem
Wegwerf-Wurzelverzeichnis, ändern nichts an `/etc` und brauchen kein root.

Voraussetzungen: `bash`, `dash`, `nginx` (für `nginx -t`), `unshare` (util-linux).

```bash
make test-packaging          # postinst/postrm- und config-Fälle
unshare -rn bash tests/packaging/functional.sh   # optional: echter nginx mit Test-Backend
```

Die md5 der unveränderten Debian-`default`-Site (`DEFAULT_MD5` in `harness.sh`) gilt für
nginx-common 1.26.3 (Debian 13). Bei einer neuen nginx-Version ggf. anpassen.
