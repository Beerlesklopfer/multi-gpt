"""mgpt-serve im Arbeits-Image (runner_agent/mgpt_lighttpd.py): Konfiguration für lighttpd."""

import pytest

from runner_agent import mgpt_lighttpd as m

TOKEN = "abcdefghijklmnop0123456789"


def config(**kwargs):
    kwargs.setdefault("cert", "/tmp/c.pem")
    kwargs.setdefault("key", "/tmp/k.pem")
    return m.build_config("/workspace/site", TOKEN, **kwargs)


def test_static_only_and_hardened():
    text = config()
    assert 'server.modules = ( "mod_access", "mod_alias", "mod_setenv", "mod_openssl" )' in text
    for forbidden in ("mod_cgi", "mod_fastcgi", "mod_dirlisting", "mod_proxy", "mod_scgi"):
        assert forbidden not in text
    assert 'server.follow-symlink = "disable"' in text
    assert '$HTTP["request-method"] !~ "^(GET|HEAD)$"' in text
    assert r"(^|/)\.git(/|$)" in text
    assert 'server.document-root = "/workspace/site"' in text
    assert f'alias.url = ( "/{TOKEN}/" => "/workspace/site/" )' in text
    assert '"X-Content-Type-Options" => "nosniff"' in text
    assert "sandbox allow-scripts" in text


def test_ports_and_tls():
    text = config(bind="0.0.0.0", port=8080, tls_port=8443)
    assert "server.port = 8080" in text
    assert '$SERVER["socket"] == "0.0.0.0:8443"' in text
    assert 'ssl.engine = "enable"' in text and 'ssl.privkey = "/tmp/k.pem"' in text
    plain = m.build_config("/workspace", TOKEN, tls_port=None)
    assert "mod_openssl" not in plain and "ssl.engine" not in plain
    with pytest.raises(ValueError):
        m.build_config("/workspace", TOKEN, tls_port=8443)  # ohne Zertifikat


def test_rejects_bad_token_and_injection():
    for bad in ("kurz", "mit Leerzeichen 0123456789", 'a"b0123456789012345', "a/b0123456789012345"):
        with pytest.raises(ValueError):
            m.build_config("/workspace", bad, tls_port=None)
    with pytest.raises(ValueError):
        m.build_config('/workspace/"; evil', TOKEN, tls_port=None)
    with pytest.raises(ValueError):
        m.build_config("/workspace/a\nb", TOKEN, tls_port=None)


def test_main_validates_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "WORKSPACE", str(tmp_path))
    assert m.main(["--token", TOKEN, "--dir", "../etc"]) == 2
    assert m.main(["--token", "kurz"]) == 2
    assert "Token ungültig" in capsys.readouterr().err
