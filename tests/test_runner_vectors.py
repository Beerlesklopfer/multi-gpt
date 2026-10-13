"""Gemeinsame Testvektoren (rs-e2e): Rahmenprotokoll und feste Vorlage, Python-Seite.

Dieselben Dateien unter ``runner-rs/testdata/frames/`` prüft der Rust-Test des
Crates gegen die serde-Typen. Beide Seiten müssen dieselben Rahmen annehmen bzw.
ablehnen. Neu erzeugen: ``python -m tests.runner_vectors``.
"""

from __future__ import annotations

import json

import pytest

from multigpt.runner import protocol as P
from tests import runner_signature_vectors as SV
from tests import runner_template_vectors as TV
from tests import runner_vectors as V

FILES = sorted(p for p in V.FRAMES_DIR.glob("*/*.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _ids(paths):
    return [f"{p.parent.name}/{p.stem}" for p in paths]


def test_files_match_generator():
    """Die eingecheckten Dateien entsprechen genau ``runner_vectors.build()``."""
    expected = V.build()
    on_disk = {f"{p.parent.name}/{p.name}": p.read_text(encoding="utf-8") for p in FILES}
    assert set(on_disk) == set(expected), "python -m tests.runner_vectors ausführen"
    for rel, entry in expected.items():
        assert on_disk[rel] == V.render(entry), rel


def test_enough_vectors_for_every_frame_type():
    kinds = {_load(p)["frame"]["type"] for p in FILES if p.parent.name == "valid"}
    assert kinds == P.FRAME_TYPES
    assert len([p for p in FILES if p.parent.name == "invalid"]) >= 80


@pytest.mark.parametrize(
    "path",
    [p for p in FILES if p.parent.name == "valid"],
    ids=_ids(p for p in FILES if p.parent.name == "valid"),
)
def test_valid_vectors(path):
    entry = _load(path)
    frame = P.parse(V.case_text(entry), entry["sender"])
    if "pad_total_bytes" not in entry:
        assert V._strip(frame) == entry.get("canonical", entry["frame"])
        # Wiederholt kodiert bleibt der Rahmen gleich (encode ↔ parse).
        fields = {k: v for k, v in frame.items() if k != "type"}
        again = P.parse(P.encode(frame["type"], entry["sender"], **fields), entry["sender"])
        assert V._strip(again) == V._strip(frame)


def _invalid_params():
    for path in FILES:
        if path.parent.name != "invalid":
            continue
        marks = []
        if path.stem in V.KNOWN_PYTHON_DEVIATIONS:
            marks.append(
                pytest.mark.xfail(reason=V.KNOWN_PYTHON_DEVIATIONS[path.stem], strict=True)
            )
        yield pytest.param(path, marks=marks, id=f"invalid/{path.stem}")


@pytest.mark.parametrize("path", list(_invalid_params()))
def test_invalid_vectors(path):
    entry = _load(path)
    with pytest.raises(P.ProtocolError) as info:
        P.parse(V.case_text(entry), entry["sender"])
    assert info.value.close_code == entry["close_code"]


# --- Feste Vorlage (runner_agent/template.py) ----------------------------------------

TEMPLATE_FILES = sorted(TV.TEMPLATE_DIR.glob("*.json"))


def test_template_files_match_generator():
    expected = TV.build()
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in TEMPLATE_FILES}
    assert set(on_disk) == set(expected), "python -m tests.runner_template_vectors ausführen"
    for name, entry in expected.items():
        assert on_disk[name] == TV.render(entry), name


def test_template_vectors_cover_all_checks():
    entries = [_load(p) for p in TEMPLATE_FILES]
    assert {e["check"] for e in entries} == {"body", "network", "inspect", "limits", "image"}
    assert {e["expect"] for e in entries if e["check"] == "body"} == {
        "work",
        "relay",
        "tool",
        "reject",
    }
    assert sum(e["expect"] == "reject" for e in entries) >= 80


def _template_params():
    for path in TEMPLATE_FILES:
        marks = []
        if path.stem in TV.KNOWN_PYTHON_DEVIATIONS:
            marks.append(
                pytest.mark.xfail(reason=TV.KNOWN_PYTHON_DEVIATIONS[path.stem], strict=True)
            )
        yield pytest.param(path, marks=marks, id=path.stem)


@pytest.mark.parametrize("path", list(_template_params()))
def test_template_vectors(path):
    entry = _load(path)
    assert TV.evaluate(entry) == entry["expect"]


# --- Signaturen des Servers (Protokoll 2) ----------------------------------------------

SIG_FILES = sorted(SV.SIG_DIR.glob("*.json"))


def test_signature_files_match_generator():
    expected = SV.build()
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in SIG_FILES}
    assert set(on_disk) == set(expected), "python -m tests.runner_signature_vectors ausführen"
    for name, entry in expected.items():
        assert on_disk[name] == SV.render(entry), name


@pytest.mark.parametrize("path", SIG_FILES, ids=[p.stem for p in SIG_FILES])
def test_signature_vectors(path):
    entry = _load(path)
    result, detail = SV.evaluate(entry)
    assert result == entry["expect"]
    if result == "ok":
        assert detail == entry["inner"]
    else:
        assert detail == entry["close_code"]
