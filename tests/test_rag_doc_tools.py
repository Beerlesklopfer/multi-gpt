"""Dokumentwerkzeuge list_documents, document_info, read_document (Plan 8b).

Rechte, Filter und Seitenabruf, Zusammensetzen der überlappenden Abschnitte,
Bereiche, Grenzen, Inhaltsverzeichnis, Quellen mit Fundstelle und ein
Durchlauf durch die Werkzeugschleife mit gemocktem Anbieter (respx).
"""

import json
import logging

import httpx
import pytest
import respx
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import tooling
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Chunk,
    Collection,
    Conversation,
    Document,
    Message,
    Provider,
    RagSettings,
    Share,
    SourceRef,
)
from multigpt.chat.rag import chunking, doc_tools, search
from multigpt.chat.rag.extract import Page
from multigpt.chat.sources import SourceCollector

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"
BASE = "https://llm.example.invalid/v1"


def axis(*weights) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for index, weight in enumerate(weights):
        vector[index] = float(weight)
    return vector


# --- Daten ------------------------------------------------------------------------


def make_user(username):
    return User.objects.create_user(username, password=PASSWORD, role=Role.objects.get(key="adult"))


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def bernd():
    return make_user("bernd")


def collection(owner, name="Ordner"):
    return Collection.objects.create(owner=owner, name=name)


def indexed(coll, title, pages, *, tokens=30, overlap=8, vector=None, **fields):
    """Dokument mit Abschnitten aus dem echten Chunker (Überlappung wie beim Indexieren)."""
    fields.setdefault("status", Document.Status.INDEXED)
    rag = RagSettings.load()
    rag.chunk_tokens, rag.overlap_tokens = tokens, overlap
    rag.save()
    doc = Document.objects.create(collection=coll, title=title, file="documents/x.pdf", **fields)
    for piece in chunking.split_pages(pages, tokens, overlap):
        Chunk.objects.create(
            document=doc,
            position=piece.position,
            text=piece.text,
            page=piece.page,
            page_end=piece.page_end,
            paragraph=piece.paragraph,
            paragraph_end=piece.paragraph_end,
            section=piece.section,
            section_title=piece.section_title,
            section_end=piece.section_end,
            embedding=vector or axis(0, 1),
        )
    return doc


def words(prefix, count):
    return " ".join(f"{prefix}{i}" for i in range(count)) + "."


def paged(*pages):
    return [Page(number, text) for number, text in enumerate(pages, start=1)]


@pytest.fixture
def collector(anna):
    conversation = Conversation.objects.create(user=anna)
    message = Message.objects.create(conversation=conversation, role="assistant", content="")
    return SourceCollector(message)


def run(tool, user, collector, **arguments):
    return getattr(doc_tools, f"run_{tool}")(user, arguments, collector)


NORM_PAGES = paged(
    "1 Anwendungsbereich\nDiese Norm legt Anforderungen fest.\n\n"
    "2 Normative Verweisungen\nEs gibt keine Verweisungen.",
    "3 Begriffe\nEs gelten die Begriffe der Grundnorm.\n\n"
    "4 Kontext der Organisation\nDie Organisation muss ihren Kontext bestimmen.",
    "5 Führung\nDie oberste Leitung muss Führung zeigen.\n\n"
    "6 Planung\nDie Organisation muss Risiken planen.",
    "7 Unterstützung\nDie Organisation muss Mittel bereitstellen.\n\n"
    "7.1 Ressourcen\nRessourcen müssen verfügbar sein.\n\n"
    "7.2 Kompetenz\nPersonen müssen kompetent sein.",
    "7.3 Bewusstsein\nPersonen müssen sich bewusst sein.\n\n"
    "7.4 Kommunikation\nDie Kommunikation muss geplant werden.\n\n"
    "7.5 Dokumentierte Information\nDie Organisation muss dokumentierte Information lenken.\n\n"
    "7.5.1 Allgemeines\nDas QM-System muss Nachweise enthalten.",
    "7.5.2 Erstellen und Aktualisieren\nBeim Erstellen ist die Kennzeichnung wichtig.\n\n"
    "7.5.3 Lenkung dokumentierter Information\nDokumentierte Information muss verfügbar sein.\n\n"
    "8 Betrieb\nDie Organisation muss Prozesse planen.",
)


def norm(coll, title="Qualitätsmanagementsysteme – Anforderungen", vector=None):
    return indexed(
        coll,
        title,
        NORM_PAGES,
        vector=vector,
        bib_type="standard",
        bib_number="DIN EN ISO 9001",
        bib_date="2015-11",
        bib_institution="DIN",
    )


# --- Zusammensetzen -------------------------------------------------------------------


def truth(pages):
    """(Seite, Absatz, Text) wie der Chunker zählt."""
    result = []
    paragraph = 0
    for page in pages:
        blocks = chunking._paragraphs(page.text)
        if page.number is not None:
            paragraph = 0
        for block in blocks:
            paragraph += 1
            text = "\n".join(" ".join(line.split()) for line in block.strip().split("\n"))
            result.append((page.number, paragraph, text))
    return result


def chunks_of(doc):
    return list(doc.chunks.order_by("position"))


@pytest.mark.parametrize("tokens,overlap", [(20, 5), (30, 8), (60, 30), (25, 0), (800, 100)])
def test_reconstruct_without_duplicates_and_with_locations(anna, tokens, overlap):
    pages = paged(
        f"{words('a', 30)}\n\n{words('b', 12)}\nzweite Zeile {words('c', 5)}\n\n{words('d', 40)}",
        f"{words('e', 8)}\n\n{words('f', 50)}",
        words("g", 3),
        f"{words('h', 25)}\n\n[Abbildung: Ein Diagramm.]\n\n{words('i', 10)}",
    )
    doc = indexed(collection(anna), "Bericht", pages, tokens=tokens, overlap=overlap)
    assert doc.chunks.count() > 1 or tokens == 800
    text = doc_tools.reconstruct(chunks_of(doc))
    expected = truth(pages)
    got = ["\n".join(u.lines) for u in text.units]
    if not overlap:
        # Ohne Überlappung ist ein Zeilenumbruch an der Chunk-Grenze nicht gespeichert.
        got = [" ".join(t.split()) for t in got]
        expected = [(p, a, " ".join(t.split())) for p, a, t in expected]
    assert got == [t for _, _, t in expected]
    for unit, (page, paragraph, _) in zip(text.units, expected, strict=True):
        if unit.certain:
            assert (unit.page, unit.paragraph) == (page, paragraph)
        else:
            assert unit.page <= page <= unit.page_end and unit.paragraph is None


def test_reconstruct_repeated_words_and_flat_documents(anna):
    pages = [Page(None, f"{'ja ' * 40}ende.\n\nZweiter Absatz {'nein ' * 30}.\n\nDritter.")]
    doc = indexed(collection(anna), "Notiz", pages, tokens=15, overlap=6)
    text = doc_tools.reconstruct(chunks_of(doc))
    assert ["\n".join(u.lines) for u in text.units] == [t for _, _, t in truth(pages)]
    assert [u.paragraph for u in text.units] == [1, 2, 3]
    assert not text.paged


def test_reconstruct_gap_without_overlap_uses_location(anna):
    doc = Document.objects.create(collection=collection(anna), title="Alt", file="x.pdf")
    for position, (text, page, paragraph) in enumerate(
        [("Erster Satz.", 1, 1), ("Weiter im Satz.", 1, 1), ("Neuer Absatz.", 1, 2)]
    ):
        Chunk.objects.create(
            document=doc,
            position=position,
            text=text,
            page=page,
            page_end=page,
            paragraph=paragraph,
            paragraph_end=paragraph,
            embedding=axis(1),
        )
    text = doc_tools.reconstruct(chunks_of(doc))
    assert [u.lines for u in text.units] == [["Erster Satz. Weiter im Satz."], ["Neuer Absatz."]]


# --- Rechte und Angebot ----------------------------------------------------------------


@pytest.fixture
def tool_model():
    provider = Provider.objects.create(name="LLM", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE)
    return AIModel.objects.create(
        provider=provider, model_id="gpt-test", display_name="G", supports_tools=True
    )


def test_tools_offered_only_with_readable_documents(anna, bernd, tool_model):
    indexed(collection(bernd), "Fremd", paged("Geheim."))
    names = set(tooling.builtin_bindings(anna, tool_model))
    assert not names & {"list_documents", "document_info", "read_document"}
    indexed(collection(anna), "Eigen", paged("Text."))
    names = set(tooling.builtin_bindings(anna, tool_model))
    assert {"list_documents", "document_info", "read_document"} <= names


def test_foreign_document_behaves_like_missing(anna, bernd, collector):
    foreign = indexed(collection(bernd, "Privat"), "Geheimvertrag", paged("Geheimer Inhalt."))
    missing = foreign.pk + 1000
    for tool in ("document_info", "read_document"):
        a = run(tool, anna, collector, document_id=foreign.pk)
        b = run(tool, anna, collector, document_id=missing)
        assert a == b == tooling.BuiltinResult(doc_tools.MSG_NOT_FOUND, is_error=True)
    result = run("list_documents", anna, collector, collection="Privat")
    assert result.is_error and "Sammlung nicht gefunden" in result.text
    result = run("list_documents", anna, collector, collection=str(foreign.collection_id))
    assert result.is_error and "Geheim" not in result.text
    assert not SourceRef.objects.exists()


def test_shared_collection_is_readable(anna, bernd, collector):
    group = UserGroup.objects.create(name="Haushalt-Test")
    anna.groups.add(group)
    coll = collection(bernd, "Haushalt")
    doc = indexed(coll, "Mietvertrag", paged("Die Kaution beträgt drei Monatsmieten."))
    Share.objects.create(collection=coll, group=group)
    assert "Mietvertrag" in run("list_documents", anna, collector).text
    assert not run("document_info", anna, collector, document_id=doc.pk).is_error
    result = run("read_document", anna, collector, document_id=doc.pk)
    assert "drei Monatsmieten" in result.text
    Share.objects.all().delete()
    assert run("read_document", anna, collector, document_id=doc.pk).is_error


@pytest.mark.parametrize(
    "tool,arguments,text",
    [
        ("read_document", {}, "document_id"),
        ("read_document", {"document_id": "abc"}, "document_id"),
        ("read_document", {"document_id": True}, "document_id"),
        ("read_document", {"document_id": 1, "page_from": 0}, "page_from"),
        ("read_document", {"document_id": 1, "page_from": 5, "page_to": 2}, "page_to"),
        ("document_info", {"document_id": -3}, "document_id"),
        ("list_documents", {"page": "x"}, "page"),
        ("list_documents", {"page_size": 51}, "höchstens 50"),
        ("list_documents", {"status": "kaputt"}, "Status"),
        ("list_documents", {"kind": "Roman"}, "Dokumentart"),
        ("list_documents", {"query": ["a"]}, "Text"),
    ],
)
def test_invalid_arguments_are_german_errors(anna, collector, tool, arguments, text):
    indexed(collection(anna), "Doc", paged("Text."))
    result = getattr(doc_tools, f"run_{tool}")(anna, arguments, collector)
    assert result.is_error and text in result.text


# --- list_documents ---------------------------------------------------------------------


def test_list_filters(anna, collector):
    a = collection(anna, "Arbeit")
    b = collection(anna, "Bücher")
    indexed(a, "Reisekosten 2023", paged("Text."), bib_date="2023")
    indexed(a, "Urlaubsantrag", paged("Text."), bib_authors="Müller, Eva")
    norm(b)
    Document.objects.create(collection=b, title="Wartet noch", file="x.pdf")
    text = run("list_documents", anna, collector).text
    assert "3 Dokumente insgesamt" in text and "Wartet noch" not in text
    assert "Wartet noch" in run("list_documents", anna, collector, status="all").text
    assert "4 Dokumente" in run("list_documents", anna, collector, status="alle").text
    only = run("list_documents", anna, collector, query="müller").text
    assert "Urlaubsantrag" in only and "Reisekosten" not in only
    by_number = run("list_documents", anna, collector, query="9001").text
    assert "1 Dokumente insgesamt" in by_number
    by_year = run("list_documents", anna, collector, year=2023).text
    assert "Reisekosten" in by_year and "Urlaub" not in by_year
    norms = run("list_documents", anna, collector, kind="Norm").text
    assert "Norm DIN EN ISO 9001:2015-11" in norms and "Reisekosten" not in norms
    assert "Kurzbeleg:" in norms
    by_name = run("list_documents", anna, collector, collection="arbeit").text
    assert "2 Dokumente" in by_name and "Qualität" not in by_name
    by_id = run("list_documents", anna, collector, collection=b.pk).text
    assert "1 Dokumente" in by_id
    none = run("list_documents", anna, collector, query="gibtsnicht")
    assert not none.is_error and none.text.startswith(doc_tools.MSG_NO_DOCUMENTS)


def test_list_uses_collections_chosen_for_this_answer(anna, collector):
    chosen = collection(anna, "Gewählt")
    indexed(chosen, "Drin", paged("Text."))
    indexed(collection(anna, "Andere"), "Draußen", paged("Text."))
    collector.message.tool_state = {"collections": [chosen.pk]}
    text = run("list_documents", anna, collector).text
    assert "Drin" in text and "Draußen" not in text


def test_list_pages_with_stable_order(anna, collector):
    first = collection(anna, "A-Sammlung")
    second = collection(anna, "B-Sammlung")
    for title in ("Zeta", "Alpha", "Mitte"):
        indexed(second, title, paged("Text."))
    for title in ("Gleich", "Gleich", "Beta"):
        indexed(first, title, paged("Text."))
    seen = []
    page = 1
    while True:
        result = run("list_documents", anna, collector, page=page, page_size=2)
        assert f"Seite {page} von 3 (6 Dokumente insgesamt" in result.text
        seen += [line for line in result.text.splitlines() if line.startswith("- ID")]
        if "letzte Seite" in result.text:
            break
        assert f"page={page + 1}" in result.text
        page += 1
    expected = Document.objects.order_by("collection__name", "title", "pk")
    assert [int(line.split()[2].rstrip(":")) for line in seen] == [d.pk for d in expected]
    error = run("list_documents", anna, collector, page=4, page_size=2)
    assert error.is_error and "3 Seite(n)" in error.text


def test_list_default_page_size_is_fifty(anna, collector):
    coll = collection(anna)
    Document.objects.bulk_create(
        Document(collection=coll, title=f"Dok {i:03d}", file="x.pdf", status="indexed")
        for i in range(55)
    )
    text = run("list_documents", anna, collector).text
    assert "Seite 1 von 2 (55 Dokumente insgesamt, 50 je Seite" in text
    assert text.count("\n- ID") == 50
    assert run("list_documents", anna, collector, page=2).text.count("\n- ID") == 5


@pytest.fixture
def query_vector(monkeypatch):
    box = {"vector": axis(1)}
    monkeypatch.setattr(search, "embed_query", lambda text: box["vector"])
    return box


@pytest.fixture
def rag_ready():
    provider = Provider.objects.create(name="Emb", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE)
    model = AIModel.objects.create(
        provider=provider,
        model_id="emb",
        display_name="Emb",
        capability=AIModel.Capability.EMBEDDING,
    )
    rag = RagSettings.load()
    rag.embedding_model = model
    rag.hybrid = False
    rag.save()
    return model


def test_list_topic_ranks_by_best_chunk_and_respects_filters(
    anna, collector, query_vector, rag_ready
):
    coll = collection(anna)
    far = norm(coll, "Umweltmanagement", vector=axis(0, 1))
    near = norm(coll, "Managementsysteme", vector=axis(1, 0.1))
    indexed(coll, "Roman", paged("Text."), vector=axis(1))
    text = run("list_documents", anna, collector, kind="Norm", topic="Qualitätsmanagement").text
    assert "Relevanz zum Thema" in text and "Roman" not in text
    assert text.index(f"ID {near.pk}:") < text.index(f"ID {far.pk}:")
    assert "bester Treffer: S. " in text


def test_list_topic_without_embeddings_is_clear_error(anna, collector):
    indexed(collection(anna), "Doc", paged("Text."))
    result = run("list_documents", anna, collector, topic="Steuern")
    assert result.is_error and "topic" in result.text


# --- document_info -----------------------------------------------------------------------


def test_document_info_with_sections_and_bibliography(anna, collector):
    doc = norm(collection(anna, "Normen"))
    result = run("document_info", anna, collector, document_id=doc.pk)
    assert not result.is_error
    text = result.text
    assert f"Dokument ID {doc.pk}" in text and "Status: indexiert" in text
    assert "Seiten mit Text: 6" in text
    assert "Normnummer" in text or "DIN EN ISO 9001" in text
    assert "Inhaltsverzeichnis (nummerierte Gliederung)" in text
    assert "7.5 Dokumentierte Information – S. 5, Abs. 3" in text
    assert "    7.5.3 Lenkung dokumentierter Information – S. 6, Abs. 2" in text
    assert text.index("<quellmaterial>") < text.index("7.5 Dokumentierte")


def test_document_info_guessed_headings_and_relative_path(anna, collector):
    pages = [
        Page(
            None,
            "# Einleitung\n\nText der Einleitung steht hier.\n\n## Ziele\n\n"
            "Die Ziele werden genannt.\n\nZusammenfassung\nKurz gesagt: alles gut.",
        )
    ]
    doc = indexed(collection(anna), "Notizen.md", pages)
    Document.objects.filter(pk=doc.pk).update(source_path="Projekte/2024/Notizen.md")
    text = run("document_info", anna, collector, document_id=doc.pk).text
    assert "aus Absatzanfängen geschätzt" in text
    assert "Einleitung – Abs. 1" in text and "  Ziele – Abs. 3" in text
    assert "Zusammenfassung – Abs. 5" in text


# --- read_document ------------------------------------------------------------------------


def body(result):
    """Text der Quellen ohne Kopf- und Zitierzeilen."""
    parts = result.text.split("<quelle ")[1:]
    return ["\n".join(p.split("\n")[3:]).split("\n</quelle>")[0] for p in parts]


def test_read_page_and_paragraph_range(anna, collector):
    pages = paged(
        "Seite eins Absatz eins.\n\nSeite eins Absatz zwei.\n\nSeite eins Absatz drei.",
        "Seite zwei Absatz eins.\n\nSeite zwei Absatz zwei.",
        "Seite drei Absatz eins.",
    )
    doc = indexed(collection(anna), "Bericht", pages, tokens=6, overlap=3)
    result = run(
        "read_document",
        anna,
        collector,
        document_id=doc.pk,
        page_from=1,
        paragraph_from=2,
        page_to=2,
        paragraph_to=1,
    )
    assert body(result) == [
        "Seite eins Absatz zwei.\n\nSeite eins Absatz drei.",
        "Seite zwei Absatz eins.",
    ]
    assert "gelesen: S. 1, Abs. 2 – S. 2, Abs. 1" in result.text
    refs = list(SourceRef.objects.order_by("pk"))
    assert [(r.page, r.paragraph, r.paragraph_end) for r in refs] == [(1, 2, 3), (2, 1, None)]
    assert refs[0].chunk.document_id == doc.pk and refs[0].biblio["title"] == "Bericht"
    events = collector.event()["sources"]
    assert events[0]["location"] == "S. 1, Abs. 2–3"
    only = run("read_document", anna, collector, document_id=doc.pk, page_from=3)
    assert body(only) == ["Seite drei Absatz eins."] and doc_tools.MSG_DOCUMENT_END in only.text
    error = run("read_document", anna, collector, document_id=doc.pk, page_from=9)
    assert error.is_error and "3 Seite(n)" in error.text
    error = run("read_document", anna, collector, document_id=doc.pk, paragraph_from=2)
    assert error.is_error and "page_from" in error.text


def test_read_flat_document_by_paragraph(anna, collector):
    pages = [Page(None, "\n\n".join(f"Absatz {i} Text." for i in range(1, 8)))]
    doc = indexed(collection(anna), "Notiz", pages, tokens=8, overlap=3)
    result = run(
        "read_document", anna, collector, document_id=doc.pk, paragraph_from=3, paragraph_to=5
    )
    assert body(result) == ["Absatz 3 Text.\n\nAbsatz 4 Text.\n\nAbsatz 5 Text."]
    ref = SourceRef.objects.get()
    assert (ref.page, ref.paragraph, ref.paragraph_end) == (None, 3, 5)
    error = run("read_document", anna, collector, document_id=doc.pk, page_from=1)
    assert error.is_error and "keine Seiten" in error.text


def test_read_truncates_and_continues_without_gap(anna, collector):
    pages = paged(*[f"{words(f's{n}x', 150)}\n\n{words(f's{n}y', 150)}" for n in range(1, 13)])
    doc = indexed(collection(anna), "Lang", pages, tokens=120, overlap=30)
    first = run("read_document", anna, collector, document_id=doc.pk)
    assert len("".join(body(first))) <= doc_tools.READ_MAX_CHARS
    assert "Gekürzt" in first.text
    hint = first.text.split("read_document(")[-1].split(")")[0]
    arguments = dict(part.split("=") for part in hint.split(", "))
    assert arguments["document_id"] == str(doc.pk)
    second = run(
        "read_document",
        anna,
        collector,
        document_id=doc.pk,
        page_from=int(arguments["page_from"]),
        paragraph_from=int(arguments["paragraph_from"]),
    )
    combined = "\n\n".join(body(first) + body(second))
    expected = [t for _, _, t in truth(pages)]
    assert combined.startswith("\n\n".join(expected[: combined.count("\n\n") + 1]))


def test_read_page_limit(anna, collector):
    pages = paged(*[f"Seite {n} {words('w', 12)}" for n in range(1, 16)])
    doc = indexed(collection(anna), "Folien", pages, tokens=10, overlap=3)
    result = run("read_document", anna, collector, document_id=doc.pk, page_from=2)
    assert body(result)[0].startswith("Seite 2 ") and body(result)[-1].startswith("Seite 11 ")
    assert len(body(result)) == 10 and "page_from=12, paragraph_from=1" in result.text


def test_read_short_pages_inside_one_chunk_give_page_range(anna, collector):
    """Mehrere kurze Seiten in einem Chunk: Seitengrenzen sind nicht gespeichert."""
    pages = paged(*[f"Folie {n}." for n in range(1, 9)])
    doc = indexed(collection(anna), "Folien", pages, tokens=12, overlap=2)
    result = run("read_document", anna, collector, document_id=doc.pk, page_from=4, page_to=4)
    text = "\n\n".join(body(result))
    assert "Folie 4." in text
    ref = SourceRef.objects.filter(page__lte=4).last()
    assert ref.page <= 4 <= (ref.page_end or ref.page)


def test_read_long_paragraph_is_clipped(anna, collector):
    doc = indexed(collection(anna), "Block", paged(words("w", 3000), "Danach."), tokens=400)
    result = run("read_document", anna, collector, document_id=doc.pk)
    assert body(result)[0].endswith("[…]") and "page_from=2" in result.text


def test_read_section_with_subsections(anna, collector):
    doc = norm(collection(anna))
    result = run("read_document", anna, collector, document_id=doc.pk, section="Abschnitt 7.5.")
    assert result.text.startswith("Abschnitt 7.5 Dokumentierte Information")
    text = "\n\n".join(body(result))
    assert text.startswith("7.5 Dokumentierte Information\nDie Organisation muss dokumentierte")
    assert "7.5.3 Lenkung" in text and "verfügbar sein." in text
    assert "Kommunikation" not in text and "8 Betrieb" not in text
    assert doc_tools.MSG_SECTION_END.format(section="7.5") in result.text
    refs = list(SourceRef.objects.order_by("pk"))
    assert [(r.section, r.page, r.paragraph) for r in refs] == [("7.5", 5, 3), ("7.5.2", 6, 1)]
    assert refs[0].section_end == "7.5.1" and refs[1].section_end == "7.5.3"
    location = collector.event()["sources"][0]["location"]
    assert "Abschn. 7.5" in location and "S. 5" in location
    assert 'fundstelle="Abschn. 7.5' in result.text


def test_read_missing_section_lists_top_level(anna, collector):
    doc = norm(collection(anna))
    result = run("read_document", anna, collector, document_id=doc.pk, section="9.9")
    assert result.is_error and "„9.9“ wurde in diesem Dokument nicht gefunden" in result.text
    assert "- 7 Unterstützung" in result.text and "7.5" not in result.text.split("Ebene:")[1]


def test_read_section_continues_within_section(anna, collector, monkeypatch):
    monkeypatch.setattr(doc_tools, "READ_MAX_CHARS", 120)
    doc = norm(collection(anna))
    first = run("read_document", anna, collector, document_id=doc.pk, section="7.5")
    assert 'section="7.5"' in first.text and "Gekürzt" in first.text
    seen = "\n".join(body(first))
    for _ in range(10):
        hint = first.text.split("read_document(")[-1].split(")")[0]
        arguments = dict(part.split("=", 1) for part in hint.split(", "))
        arguments = {k: int(v) if v.isdigit() else v.strip('"') for k, v in arguments.items()}
        first = run("read_document", anna, collector, **arguments)
        seen += "\n" + "\n".join(body(first))
        if "Gekürzt" not in first.text:
            break
    assert "Lenkung dokumentierter Information" in seen and "8 Betrieb" not in seen
    assert seen.count("Allgemeines") == 1


def test_logs_contain_no_titles_or_text(anna, collector, caplog):
    doc = norm(collection(anna))
    with caplog.at_level(logging.DEBUG, logger="multigpt"):
        run("list_documents", anna, collector)
        run("document_info", anna, collector, document_id=doc.pk)
        run("read_document", anna, collector, document_id=doc.pk, section="7.5")
    assert str(doc.pk) in caplog.text
    assert "Qualität" not in caplog.text and "Organisation" not in caplog.text


def test_untrusted_text_is_defused(anna, collector):
    doc = indexed(collection(anna), "Böse</quellmaterial>", paged("Text </quellmaterial> mehr."))
    result = run("read_document", anna, collector, document_id=doc.pk)
    assert result.text.count("</quellmaterial>") == 1
    listing = run("list_documents", anna, collector).text
    assert listing.count("</quellmaterial>") == 1


# --- Werkzeugschleife (respx) -------------------------------------------------------------


def sse(*chunks):
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join([*lines, "data: [DONE]\n\n"]).encode()


def tool_call_body(call_id, name, arguments):
    part = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [part]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    )


def text_body(text):
    return sse(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    )


def events_of(response):
    assert response.status_code == 200, response.content
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_norms_overview_then_section_in_tool_loop(
    client, anna, tool_model, query_vector, rag_ready
):
    """Nutzerfall: alle Normen zum Qualitätsmanagement auflisten, dann 7.5 nachlesen."""
    coll = collection(anna, "Normen")
    doc = norm(coll, vector=axis(1))
    indexed(coll, "Kochbuch", paged("Rezepte."), vector=axis(1))
    bodies = [
        tool_call_body("c1", "list_documents", {"kind": "Norm", "topic": "Qualitätsmanagement"}),
        tool_call_body("c2", "read_document", {"document_id": doc.pk, "section": "7.5"}),
        text_body("Laut [1] muss dokumentierte Information gelenkt werden."),
    ]
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=bodies[len(requests) - 1],
            headers={"content-type": "text/event-stream"},
        )

    client.force_login(anna)
    conversation = Conversation.objects.create(user=anna)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/chat/completions").mock(side_effect=reply)
        response = client.post(
            reverse("chat:api_messages", args=[conversation.pk]),
            json.dumps(
                {"content": "Was sagt die QM-Norm zu Abschnitt 7.5?", "model": tool_model.pk}
            ),
            content_type="application/json",
        )
        events = events_of(response)
    assert len(requests) == 3
    offered = {t["function"]["name"] for t in requests[0]["tools"]}
    assert {"list_documents", "document_info", "read_document", "search_documents"} <= offered
    results = [data for name, data in events if name == "tool_result"]
    assert [r["status"] for r in results] == ["ok", "ok"]
    tool_messages = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    listing = tool_messages[0]["content"]
    assert f"ID {doc.pk}:" in listing and "DIN EN ISO 9001:2015-11" in listing
    assert "Kochbuch" not in listing
    section = [m for m in requests[2]["messages"] if m["role"] == "tool"][-1]["content"]
    assert section.startswith("Abschnitt 7.5 Dokumentierte Information")
    assert "Dokumentierte Information muss verfügbar sein." in section
    sources = [data for name, data in events if name == "sources"][-1]["sources"]
    assert "Abschn. 7.5" in sources[0]["location"] and sources[0]["page"] == 5
    answer = Message.objects.get(role="assistant")
    assert "dokumentierte Information gelenkt" in answer.content
    assert answer.sources.count() == 2


def test_pages_starting_in_same_chunk_get_own_numbers(anna, collector):
    pages = paged(*[f"Folie {n} {words('w', 3)}" for n in range(1, 7)])
    doc = indexed(collection(anna), "Folien", pages, tokens=200, overlap=20)
    assert doc.chunks.count() == 1
    first = run("read_document", anna, collector, document_id=doc.pk, page_from=1, page_to=2)
    second = run("read_document", anna, collector, document_id=doc.pk, page_from=1, page_to=2)
    assert first.text == second.text  # gleiche Fundstelle -> gleiche Nummer
    numbers = [s["n"] for s in collector.event()["sources"]]
    assert len(numbers) == len(set(numbers)) >= 2


def test_search_results_name_document_id(anna, collector, query_vector, rag_ready):
    from multigpt.chat.rag import chat as rag_chat

    doc = indexed(collection(anna), "Mietvertrag", paged("Die Kaution beträgt drei Mieten."))
    result = rag_chat.run_search_documents(anna, {"query": "Kaution"}, collector)
    assert f'dokument_id="{doc.pk}"' in result.text
    assert "read_document" in rag_chat.SEARCH_DOCUMENTS_SPEC.description
