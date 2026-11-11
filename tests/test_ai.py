"""AI-delen mot en fejkad Claude-API: bilder och PDF i förfrågan, lärdomar och bibliotek i prompten,
strukturerad output, cachning, fallback och felhantering."""

import asyncio
import base64

import pytest

pytest.importorskip("anthropic")

from form_assistant import ai  # noqa: E402
from form_assistant.config import Config  # noqa: E402
from form_assistant.model import FormDraft  # noqa: E402

from conftest import FakeClaude, draft_dict, form_image  # noqa: E402
from reportlab_free_pdf import simple_pdf  # noqa: E402

CFG = Config()
LIBRARY = [{"url": "https://ex.se/ValueSet/janej", "title": "Ja/Nej", "uses": 7,
            "concepts": [{"system": "s", "code": "J", "display": "Ja"}, {"system": "s", "code": "N", "display": "Nej"}]}]


def run(coro):
    return asyncio.run(coro)


def test_interpret_sends_files_guidelines_library_and_examples():
    claude = FakeClaude(draft_dict())
    uploads = [{"filename": "sida1.png", "media_type": "image/png", "data": form_image(0, size=(3000, 4000))},
               {"filename": "bilaga.pdf", "media_type": "application/pdf",
                "data": simple_pdf(["Bilaga till hälsodeklarationen", "Fyll i uppgifterna om dina läkemedel nedan"])}]
    example = FormDraft.model_validate(draft_dict() | {"title": "Godkänt exempel"})
    seen = []
    draft, meta = run(ai.interpret(claude(), CFG, uploads, LIBRARY, ["Telefon och e-post är två frågor."],
                                   [example], "Hälsodeklaration", progress=seen.append))
    assert seen and ai.count_items(seen[-1]) == len(draft.items)          # framsteg medan svaret strömmar
    assert isinstance(draft, FormDraft) and draft.title == "Hälsodeklaration inför vaccination"
    assert draft == FormDraft.model_validate(draft_dict())                 # kompakt svar → fullständigt utkast
    assert meta["underlag"] == ["sida1.png: bild", "bilaga.pdf: PDF-dokument, 1 sidor"]
    assert meta["model"] == "claude-opus-5-5" and meta["request_id"] == "msg_1" and meta["kind"] == "ai-tolkning"
    body = claude.requests[0]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["stream"] is True and body["max_tokens"] == ai.MAX_TOKENS
    assert "server-side-fallback-2026-07-01" in body["_headers"]["anthropic-beta"]
    assert body["output_config"]["effort"] == "high" and body["output_config"]["format"]["type"] == "json_schema"
    system = body["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"} and "Telefon och e-post är två frågor." in system["text"]
    content = body["messages"][0]["content"]
    image = next(b for b in content if b["type"] == "image")
    from PIL import Image
    import io
    with Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))) as img:
        assert max(img.size) <= ai.MAX_IMAGE_EDGE                      # stora bilder skalas ned
    document = next(b for b in content if b["type"] == "document")
    assert document["source"]["media_type"] == "application/pdf" and document["cache_control"] == {"type": "ephemeral"}
    text = content[-1]["text"]
    assert "https://ex.se/ValueSet/janej: Ja/Nej – Ja, Nej" in text and "Godkänt exempel" in text
    assert "Hälsodeklaration" in text


PATCH = {"header": [{"field": "title", "value": "Reviderad"}], "remove": ["graviditet"],
         "changes": [{"action": "ändra", "after": "",
                      "item": {"link_id": "allergi", "text": "Är du allergisk mot något?", "type": "choice",
                               "parent": "halsa", "prefix": "3", "required": True,
                               "options": [{"display": "Ja"}, {"display": "Nej"}, {"display": "Vet inte"}]}},
                     {"action": "lägg_till", "after": "feber",
                      "item": {"link_id": "temperatur", "text": "Temperatur", "type": "decimal", "parent": "halsa"}}],
         "notes": ["Fråga 4 var delvis oläslig."]}


def test_revise_sends_only_changes_and_no_files_by_default():
    claude = FakeClaude(PATCH)
    draft = FormDraft.model_validate(draft_dict())
    pdf = simple_pdf(["Hälsodeklaration inför vaccination", "Har du feber idag? Ja Nej", "Är du allergisk mot något?"])
    uploads = [{"filename": "sida1.png", "media_type": "image/png", "data": form_image()},
               {"filename": "blankett.pdf", "media_type": "application/pdf", "data": pdf}]
    result, meta = run(ai.revise(claude(), CFG, uploads, LIBRARY, [], draft, "Gör fråga 3 obligatorisk"))
    assert result.title == "Reviderad" and result.item("allergi").required is True
    assert result.item("graviditet") is None and [i.link_id for i in result.children("halsa")] == [
        "feber", "temperatur", "allergi", "allergi-vad"]
    assert meta["instruction"] == "Gör fråga 3 obligatorisk" and meta["originalfiler"] is False
    assert meta["changes"] == {"ändrade": 1, "tillagda": 1, "borttagna": 1}
    content = claude.requests[0]["messages"][0]["content"]
    assert not any(b["type"] in ("image", "document") for b in content)   # originalen skickas inte om
    assert any("Har du feber idag?" in b.get("text", "") for b in content)  # men PDF:ens textlager gör det
    assert meta["underlag"] == ["sida1.png: utelämnad (bild)", "blankett.pdf: 1 sidor text"]
    text = content[-1]["text"]
    assert "Nuvarande beskrivning" in text and '"link_id":"allergi"' in text.replace(" ", "")
    assert '"read_only": false' not in text and "null" not in text                # standardvärden utelämnas
    assert claude.requests[0]["output_config"]["format"]["schema"]["properties"].keys() >= {"changes", "remove"}

    claude = FakeClaude(PATCH)
    _, meta = run(ai.revise(claude(), CFG, uploads, LIBRARY, [], draft, "Lägg till fråga 5", with_files=True))
    content = claude.requests[0]["messages"][0]["content"]
    assert meta["originalfiler"] is True and {"image", "document"} <= {b["type"] for b in content}


def test_revise_errors():
    claude = FakeClaude(PATCH)
    draft = FormDraft.model_validate(draft_dict())
    with pytest.raises(ai.AiError, match="Skriv vad"):
        run(ai.revise(claude(), CFG, [], [], [], draft, "  "))
    truncated = FakeClaude((draft_dict(), "max_tokens"))
    with pytest.raises(ai.AiError, match="för långt"):
        run(ai.interpret(truncated(), CFG, [{"filename": "a.png", "media_type": "image/png", "data": form_image()}],
                         [], [], []))
    with pytest.raises(ai.AiError, match="minst en"):
        run(ai.interpret(claude(), CFG, [], [], [], []))


def test_rejected_schema_falls_back_to_full_schema():
    error = {"_status": 400, "message": "Schema is too complex for compilation"}
    claude = FakeClaude(error, draft_dict() | {"title": "Med fullständigt schema"})
    uploads = [{"filename": "a.png", "media_type": "image/png", "data": form_image()}]
    draft, meta = run(ai.interpret(claude(), CFG, uploads, [], [], []))
    assert draft.title == "Med fullständigt schema" and meta["schema"] == "fullständigt"
    schemas = [r["output_config"]["format"]["schema"] for r in claude.requests]
    assert "required" in schemas[1]["$defs"]["Item"] and "read_only" in schemas[1]["$defs"]["Item"]["required"]
    assert "read_only" not in schemas[0]["$defs"]["AiItem"]["required"]

    claude = FakeClaude(error, draft_dict() | {"title": "Hel revidering"})
    revised, meta = run(ai.revise(claude(), CFG, uploads, [], [], FormDraft.model_validate(draft_dict()), "Byt titel"))
    assert revised.title == "Hel revidering" and meta["schema"] == "fullständigt"
    assert "returnera\nhela formuläret" in claude.requests[1]["messages"][0]["content"][-1]["text"]


def test_pdf_modes_and_scanned_pages():
    from PIL import Image
    import io
    text_pdf = simple_pdf(["Hälsodeklaration inför operation och anestesi", "Har du haft problem vid narkos? Ja Nej"])
    page = Image.open(io.BytesIO(form_image(0)))
    scanned = io.BytesIO()
    page.save(scanned, format="PDF")
    uploads = [{"filename": "text.pdf", "media_type": "application/pdf", "data": text_pdf},
               {"filename": "skannad.pdf", "media_type": "application/pdf", "data": scanned.getvalue()}]

    def kinds(mode):
        files = ai.file_blocks(uploads, Config(pdf_input=mode))
        return [b["type"] for b in files.blocks], files

    types, files = kinds("dokument")
    assert types.count("document") == 1 and types.count("image") == 1   # inskannad PDF blir alltid egna sidbilder
    assert files.summary == ["text.pdf: PDF-dokument, 1 sidor", "skannad.pdf: 1 sidor bild"]
    types, files = kinds("text")
    assert "document" not in types and types.count("image") == 1
    assert "problem vid narkos" in files.blocks[1]["text"]
    types, files = kinds("text-bild")
    assert "document" not in types and types.count("image") == 2
    small = next(b for b in files.blocks if b["type"] == "image")
    with Image.open(io.BytesIO(base64.b64decode(small["source"]["data"]))) as img:
        assert max(img.size) == 1100                                    # liten sidbild bredvid texten
    assert files.blocks[-1]["cache_control"] == {"type": "ephemeral"}
    with pytest.raises(ValueError, match="FORM_PDF_INPUT"):
        import os
        os.environ["FORM_PDF_INPUT"] = "allt"
        try:
            Config.from_env()
        finally:
            del os.environ["FORM_PDF_INPUT"]


def test_suggest_guidelines():
    claude = FakeClaude({"guidelines": [{"text": "Dela upp adress i gatuadress, postnummer och ort.",
                                         "rationale": "Rättat i tre formulär."}]})
    result, _ = run(ai.suggest_guidelines(claude(), CFG, [{"title": "A", "instruction": "Dela upp adressen"}], []))
    assert result.guidelines[0].text.startswith("Dela upp adress")
    assert "Dela upp adressen" in claude.requests[0]["messages"][0]["content"][0]["text"]
    with pytest.raises(ai.AiError, match="ingen återkoppling"):
        run(ai.suggest_guidelines(claude(), CFG, [], []))
