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
               {"filename": "bilaga.pdf", "media_type": "application/pdf", "data": simple_pdf(["Bilaga"])}]
    example = FormDraft.model_validate(draft_dict() | {"title": "Godkänt exempel"})
    draft, meta = run(ai.interpret(claude(), CFG, uploads, LIBRARY, ["Telefon och e-post är två frågor."],
                                   [example], "Hälsodeklaration"))
    assert isinstance(draft, FormDraft) and draft.title == "Hälsodeklaration inför vaccination"
    assert meta["model"] == "claude-opus-5-5" and meta["request_id"] == "msg_1" and meta["kind"] == "ai-tolkning"
    body = claude.requests[0]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
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


def test_revise_keeps_context_and_handles_errors():
    revised = draft_dict() | {"title": "Reviderad"}
    claude = FakeClaude(revised)
    draft = FormDraft.model_validate(draft_dict())
    result, meta = run(ai.revise(claude(), CFG, [], LIBRARY, [], draft, "Gör fråga 3 obligatorisk"))
    assert result.title == "Reviderad" and meta["instruction"] == "Gör fråga 3 obligatorisk"
    text = claude.requests[0]["messages"][0]["content"][-1]["text"]
    assert "Nuvarande beskrivning" in text and '"link_id":"allergi"' in text.replace(" ", "")
    with pytest.raises(ai.AiError, match="Skriv vad"):
        run(ai.revise(claude(), CFG, [], [], [], draft, "  "))
    truncated = FakeClaude((draft_dict(), "max_tokens"))
    with pytest.raises(ai.AiError, match="för långt"):
        run(ai.interpret(truncated(), CFG, [{"filename": "a.png", "media_type": "image/png", "data": form_image()}],
                         [], [], []))
    with pytest.raises(ai.AiError, match="minst en"):
        run(ai.interpret(claude(), CFG, [], [], [], []))


def test_suggest_guidelines():
    claude = FakeClaude({"guidelines": [{"text": "Dela upp adress i gatuadress, postnummer och ort.",
                                         "rationale": "Rättat i tre formulär."}]})
    result, _ = run(ai.suggest_guidelines(claude(), CFG, [{"title": "A", "instruction": "Dela upp adressen"}], []))
    assert result.guidelines[0].text.startswith("Dela upp adress")
    assert "Dela upp adressen" in claude.requests[0]["messages"][0]["content"][0]["text"]
    with pytest.raises(ai.AiError, match="ingen återkoppling"):
        run(ai.suggest_guidelines(claude(), CFG, [], []))
