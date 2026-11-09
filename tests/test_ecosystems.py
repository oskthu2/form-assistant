"""Formulärekosystem: lagras som ett urval, standard från miljön, val per formulär och export."""

import re
import sqlite3

import pytest

from form_assistant import ecosystems
from form_assistant.config import REGEX_EXTENSION, Config
from form_assistant.db import Database

from conftest import FakeClaude, draft_dict, follow_job, form_image

CFG = Config(canonical_base="http://example.org/fhir", publisher="Exempel")


def test_default_and_saving(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    ecosystems.ensure_default(db, CFG)
    ecosystems.ensure_default(db, CFG)                                   # en gång
    [standard] = ecosystems.all_ecosystems(db)
    assert (standard.code, standard.default, standard.example, standard.publisher) == ("standard", True, True, "Exempel")
    assert db.valuesets() == [] and len(db.valuesets("ekosystem")) == 1  # skilt från svarsurvalen

    inera = ecosystems.Ecosystem(code="Inera", display="Ineras formulärtjänst",
                                 canonical_base="https://fhir.region-exempel.se/formular/", publisher="Region Exempel",
                                 allowed_extensions=[REGEX_EXTENSION], default=True)
    ecosystems.save(db, inera)
    assert ecosystems.get(db, None).code == "inera"                     # nytt förval
    assert ecosystems.get(db, "standard").default is False
    assert ecosystems.get(db, "finns-inte").code == "inera"
    cfg = ecosystems.config_for(CFG, ecosystems.get(db, "inera"))
    assert cfg.canonical_base == "https://fhir.region-exempel.se/formular"
    assert cfg.allowed_extensions == frozenset({REGEX_EXTENSION})

    for bad, message in ((dict(code="Ä!"), "Koden"), (dict(canonical_base="ftp://x"), "http"),
                         (dict(display=" "), "namn"), (dict(answer_options="xml"), "inline"),
                         (dict(allowed_extensions=["regex"]), "URL")):
        with pytest.raises(ecosystems.EcosystemError, match=message):
            ecosystems.save(db, ecosystems.Ecosystem(**({"code": "x", "display": "X",
                                                        "canonical_base": "https://x.se"} | bad)))

    codesystem, valueset = ecosystems.fhir_resources(db)
    assert codesystem["count"] == 2 and valueset["compose"]["include"][0]["system"] == codesystem["url"]
    props = {p["code"]: p for p in next(c for c in codesystem["concept"] if c["code"] == "inera")["property"]}
    assert props["canonical-base"]["valueString"] == "https://fhir.region-exempel.se/formular"
    assert props["allowed-extension"]["valueString"] == REGEX_EXTENSION and props["default"]["valueBoolean"]


def test_existing_database_is_migrated(tmp_path):
    path = tmp_path / "gammal.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE forms (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'utkast', "
                       "created TEXT NOT NULL, updated TEXT NOT NULL, created_by TEXT NOT NULL, approved_by TEXT, "
                       "approved_at TEXT, derived_from TEXT, fingerprint TEXT, search_text TEXT);"
                       "CREATE TABLE valuesets (url TEXT PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, "
                       "concepts TEXT NOT NULL, source_form TEXT, created TEXT NOT NULL, uses INTEGER NOT NULL DEFAULT 0);"
                       "INSERT INTO valuesets VALUES ('u', 'N', 'T', '[]', NULL, 'x', 1);")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.valuesets()[0]["url"] == "u" and db.valuesets()[0]["kind"] == "svar"
    assert "ecosystem" in {r["name"] for r in db.all("PRAGMA table_info(forms)")}


def test_ecosystem_per_form_in_web(tmp_path):
    pytest.importorskip("anthropic")
    from starlette.testclient import TestClient
    from form_assistant.web import create_app
    cfg = Config(db_path=tmp_path / "db.sqlite")
    with TestClient(create_app(cfg, Database(cfg.db_path), ai_client_factory=FakeClaude(draft_dict())),
                    follow_redirects=True) as client:
        client.cookies.set("anvandare", "anna")
        _ecosystem_flow(client)


def _ecosystem_flow(client):
    r = client.post("/ekosystem", data={"code": "region", "display": "Region Exempel", "answer_options": "valueset",
                                        "canonical_base": "https://fhir.region-exempel.se/formular",
                                        "publisher": "Region Exempel", "allowed_extensions": REGEX_EXTENSION})
    assert "Region Exempel är sparat" in r.text and "exempeladress" in r.text   # standard har kvar example.org
    assert client.post("/ekosystem", data={"code": "x", "display": "X", "canonical_base": "inte en url"}).status_code == 400

    page = client.get("/").text
    assert 'value="region"' in page and 'value="standard" selected' in page
    r = follow_job(client, client.post("/ladda-upp", files={"files": ("s.png", form_image(0), "image/png")},
                                       data={"action": "skapa", "ecosystem": "region"}))
    fid = r.url.path.split("/")[2]
    assert "exempeladress" not in r.text
    bundle = client.get(f"/f/{fid}/bundle.json").json()
    q = bundle["entry"][0]["resource"]
    assert q["url"].startswith("https://fhir.region-exempel.se/formular/Questionnaire/")
    assert q["publisher"] == "Region Exempel"
    assert "ValueSet" in [e["resource"]["resourceType"] for e in bundle["entry"]]   # ekosystemets valueset-läge
    personnummer = q["item"][0]["item"][0]
    assert personnummer["extension"][0]["url"] == REGEX_EXTENSION               # tillåten i det här ekosystemet

    r = client.post(f"/f/{fid}/ekosystem", data={"ecosystem": "standard"})
    assert "hör nu till Standard" in r.text and "exempeladress" in r.text
    q = client.get(f"/f/{fid}/questionnaire.json").json()
    assert q["url"].startswith("http://example.org/fhir/") and "extension" not in str(q)
    assert "finns inte" in client.post(f"/f/{fid}/ekosystem", data={"ecosystem": "nej"}).text

    r = client.post(f"/f/{fid}/kopia", data={"ecosystem": "region"})
    copy = r.url.path.split("/")[2]
    q = client.get(f"/f/{copy}/questionnaire.json").json()
    assert q["url"].startswith("https://fhir.region-exempel.se/") and q["derivedFrom"][0].startswith("http://example.org/")
    codes = [c["code"] for c in client.get("/ekosystem/resurs").json()["entry"][0]["resource"]["concept"]]
    assert codes == ["standard", "region"]
    assert re.search(r"Region Exempel.*?<td>1</td>", client.get("/ekosystem").text, re.S)
