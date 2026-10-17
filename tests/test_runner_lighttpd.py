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
    assert "sandbox" not in text
    assert '"Cross-Origin-Resource-Policy" => "same-origin"' in text


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


@pytest.mark.parametrize("existing", [False, True, "legacy"])
def test_start_passes_workspace_config_to_lighttpd(tmp_path, monkeypatch, existing):
    monkeypatch.setattr(m, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(m, "RUN_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(m, "_replace_running", lambda: None)
    monkeypatch.setattr(m, "make_certificate", lambda _: ("/tmp/c.pem", "/tmp/k.pem"))
    template = tmp_path / "image-template.conf"
    template.write_text(
        "# Preserved image template setting\n" + m.build_config("/workspace", None, tls_port=None)
    )
    monkeypatch.setattr(m, "TEMPLATE", str(template))
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda argv, **kwargs: calls.append(argv))

    class Started(Exception):
        pass

    def execute(binary, argv):
        calls.append(argv)
        raise Started

    monkeypatch.setattr(m.os, "execv", execute)
    path = str(tmp_path / "lighttpd.conf")
    custom = m.build_config(str(tmp_path / "custom"), TOKEN, tls_port=None)
    if existing:
        initial = custom.replace(m.CSP, m.LEGACY_CSP) if existing == "legacy" else custom
        (tmp_path / "lighttpd.conf").write_text(initial)
    with pytest.raises(Started):
        m.main(["--token", TOKEN, "--config", path, "--dir", "/workspace"])
    text = (tmp_path / "lighttpd.conf").read_text()
    if existing:
        assert text == custom
    else:
        assert "# Preserved image template setting" in text
        assert f'server.document-root = "{tmp_path}"' in text
        assert "server.port = 8080" in text
        assert 'ssl.engine = "enable"' in text
    assert calls == [[m.LIGHTTPD, "-tt", "-f", path], ["lighttpd", "-D", "-f", path]]


def test_readiness_failure_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "WORKSPACE", str(tmp_path))

    class Unreachable:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args):
            raise ConnectionRefusedError

        def close(self):
            pass

    monkeypatch.setattr(m.http.client, "HTTPConnection", Unreachable)
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    assert m.main(["--token", TOKEN, "--check-ready"]) == 1
    assert "nicht erreichbar" in capsys.readouterr().err


def test_readiness_accepts_native_lighttpd(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "WORKSPACE", str(tmp_path))

    class NativeHTTP:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path):
            assert (method, path) == ("HEAD", "/")

        def getresponse(self):
            from types import SimpleNamespace

            return SimpleNamespace(status=200)

        def close(self):
            pass

    monkeypatch.setattr(m.http.client, "HTTPConnection", NativeHTTP)
    assert m.main(["--check-ready"]) == 0
