"""Webbklienten hela vägen: ladda upp, tolka (fejkad Claude), iterera, använd urval, godkänn, exportera,
känn igen samma blankett och skapa en kopia, lärdomar."""

import json
import re

import pytest

pytest.importorskip("anthropic")

from starlette.testclient import TestClient  # noqa: E402

from form_assistant.config import Config  # noqa: E402
from form_assistant.db import Database  # noqa: E402
from form_assistant.web import create_app  # noqa: E402

from conftest import FakeClaude, draft_dict, form_image  # noqa: E402


def msg(html: str) -> str:
    m = re.search(r'class="msg">([^<]*)', html)
    return m.group(1).strip() if m else ""


@pytest.fixture
def app(tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite", canonical_base="https://fhir.region-exempel.se/formular",
                 publisher="Region Exempel")
    db = Database(cfg.db_path)
    revised = draft_dict()
    revised["items"][6]["required"] = True                  # "Gör fråga 3 obligatorisk"
    claude = FakeClaude(draft_dict(), revised,
                        {"guidelines": [{"text": "Flervalsfrågor som gäller hälsa är oftast obligatoriska.",
                                         "rationale": "Rättat vid iteration."}]})
    client = TestClient(create_app(cfg, db, ai_client_factory=claude), follow_redirects=True)
    client.cookies.set("anvandare", "anna")
    return client, claude, db


def test_full_flow(app):
    client, claude, db = app
    assert "Nytt formulär" in client.get("/").text
    r = client.post("/ladda-upp", files={"files": ("sida1.png", form_image(0), "image/png")},
                    data={"hint": "Hälsodeklaration inför vaccination", "action": "skapa"})
    assert "Formuläret är tolkat" in msg(r.text), r.text[:500]
    fid = r.url.path.split("/")[2]
    page = r.text
    assert "Personnummer" in page and "personnummer (SEBasePatient)" in page and "HSA-id (SEBaseOrganization)" in page
    assert "Fråga 4 var delvis oläslig." in page and "Alternativen svårlästa." in page
    assert "inga extensions" in page

    r = client.post(f"/f/{fid}/revidera", data={"instruction": "Gör fråga 3 obligatorisk"})
    assert "Version 2 är klar" in msg(r.text)
    q = client.get(f"/f/{fid}/questionnaire.json").json()
    allergi = next(i for g in q["item"] for i in g.get("item", []) if i["linkId"] == "allergi")
    assert allergi["required"] is True
    assert "Gör fråga 3 obligatorisk" in claude.requests[1]["messages"][0]["content"][-1]["text"]
    assert any(b["type"] == "image" for b in claude.requests[1]["messages"][0]["content"])   # originalet följer med

    client.cookies.set("anvandare", "")
    assert "Ange ditt namn" in msg(client.post(f"/f/{fid}/godkann").text)
    client.cookies.set("anvandare", "bertil")
    r = client.post(f"/f/{fid}/godkann")
    assert "Formuläret är godkänt" in msg(r.text) and "nytt urval" in msg(r.text)
    assert len(db.valuesets()) == 2 and "Ja, Nej, Vet inte" in client.get("/urval").text

    bundle = client.get(f"/f/{fid}/bundle.json")
    assert "attachment" in bundle.headers["content-disposition"]
    b = bundle.json()
    assert b["type"] == "transaction" and [e["resource"]["resourceType"] for e in b["entry"]] == ["Questionnaire",
                                                                                                "Provenance"]
    q, prov = (e["resource"] for e in b["entry"])
    assert q["status"] == "active" and q["publisher"] == "Region Exempel"
    roles = [(a["type"]["coding"][0]["code"], a["who"]["display"]) for a in prov["agent"]]
    assert ("author", "AI: claude-opus-5-5") in roles and ("verifier", "bertil") in roles and ("enterer", "anna") in roles
    assert any(e["what"]["display"].startswith("Underlag: sida1.png") for e in prov["entity"])

    # Samma blankett skannad igen: tjänsten känner igen den och erbjuder en kopia i stället för AI-tolkning.
    calls = len(claude.requests)
    r = client.post("/ladda-upp", files={"files": ("ny-skanning.jpg", form_image(0, size=(1000, 1333), fmt="JPEG"),
                                                   "image/jpeg")}, data={"action": "skapa"})
    assert "Formuläret verkar redan finnas" in r.text and "samma blankett" in r.text
    assert len(claude.requests) == calls                                   # inget AI-anrop
    upload_id = re.search(r'name="upload_ids" value="([0-9a-f]+)"', r.text).group(1)
    r = client.post(f"/f/{fid}/kopia", data={"upload_ids": [upload_id]})
    assert "En kopia är skapad" in msg(r.text) and "Kopia av" in r.text
    copy_id = r.url.path.split("/")[2]
    copy_bundle = client.get(f"/f/{copy_id}/bundle.json").json()
    copy_q, copy_prov = (e["resource"] for e in copy_bundle["entry"])
    assert copy_q["derivedFrom"] == [q["url"]] and copy_q["url"] != q["url"]
    assert any(e["role"] == "derivation" for e in copy_prov["entity"])

    # Sökning på beskrivning hittar formuläret.
    assert "Hälsodeklaration inför vaccination" in client.get("/sok?q=vaccination+hälsodeklaration").text

    # Lärdomar: AI föreslår ur återkopplingen, en människa godkänner, prompten innehåller den sedan.
    r = client.post("/lardomar/foresla")
    assert "1 förslag" in msg(r.text) and "Flervalsfrågor som gäller hälsa" in r.text
    gid = re.search(r'/lardomar/(\d+)/aktiv', r.text).group(1)
    client.post(f"/lardomar/{gid}/aktiv")
    client.post("/ladda-upp", files={"files": ("annan.png", form_image(1), "image/png")}, data={"action": "skapa"})
    assert "Flervalsfrågor som gäller hälsa" in claude.requests[-1]["system"][0]["text"]
    assert "Godkända formulär som exempel" in claude.requests[-1]["messages"][0]["content"][-1]["text"]


def test_valueset_suggestion_restore_and_errors(app):
    client, claude, db = app
    db.save_valueset("https://ex.se/ValueSet/janejvet", "JaNejVet", "Ja/Nej/Vet ej", [
        {"system": "https://ex.se/CodeSystem/jnv", "code": "J", "display": "Ja"},
        {"system": "https://ex.se/CodeSystem/jnv", "code": "N", "display": "Nej"},
        {"system": "https://ex.se/CodeSystem/jnv", "code": "V", "display": "Vet ej"}], None)
    r = client.post("/ladda-upp", files={"files": ("s.png", form_image(0), "image/png")}, data={"action": "skapa"})
    fid = r.url.path.split("/")[2]
    assert "Befintliga urval i biblioteket" in r.text and "täcker alla alternativ och fler" in r.text
    r = client.post(f"/f/{fid}/urval", data={"link_id": "graviditet", "url": "https://ex.se/ValueSet/janejvet"})
    assert "urvalet från biblioteket" in msg(r.text)
    q = client.get(f"/f/{fid}/questionnaire.json").json()
    grav = next(i for g in q["item"] for i in g.get("item", []) if i["linkId"] == "graviditet")
    assert [o["valueCoding"]["code"] for o in grav["answerOption"]] == ["J", "N", "V"]
    assert "Återställ den här versionen" in client.get(f"/f/{fid}?v=1").text
    r = client.post(f"/f/{fid}/aterstall", data={"number": "1"})
    assert "återställd som version 3" in msg(r.text)

    assert client.post("/ladda-upp", data={"action": "skapa"}).status_code == 400
    r = client.post("/ladda-upp", files={"files": ("x.txt", b"hej", "text/plain")}, data={"action": "skapa"})
    assert r.status_code == 400 and "bara bilder" in r.text
    assert client.get("/f/finns-inte").status_code == 404
    r = client.post(f"/f/{fid}/godkann", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/").headers["x-frame-options"] == "DENY"


def test_ai_failure_is_shown(tmp_path):
    cfg = Config(db_path=tmp_path / "db.sqlite")
    client = TestClient(create_app(cfg, Database(cfg.db_path), ai_client_factory=FakeClaude((draft_dict(), "refusal"))))
    r = client.post("/ladda-upp", files={"files": ("s.png", form_image(0), "image/png")}, data={"action": "skapa"})
    assert r.status_code == 502 and "avböjde" in r.text
    assert json.loads(client.get("/health").text) == {"ok": True}
