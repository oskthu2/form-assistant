"""Formulärutkast -> FHIR: inga extensions som standard, svenska basprofiler, villkor, urval, Provenance, Bundle."""

from form_assistant import fhir
from form_assistant.config import REGEX_EXTENSION, Config
from form_assistant.model import FormDraft

from conftest import draft_dict, item

CFG = Config(canonical_base="https://fhir.region-exempel.se/formular", publisher="Region Exempel")


def flat(items):
    for i in items:
        yield i
        yield from flat(i.get("item", []))


def test_questionnaire_core_only_with_swedish_base_profiles(draft):
    build = fhir.questionnaire("abc123def456", draft, 2, CFG)
    q = build.questionnaire
    assert q["resourceType"] == "Questionnaire" and q["status"] == "draft" and q["version"] == "0.2.0"
    assert q["url"] == "https://fhir.region-exempel.se/formular/Questionnaire/halsodeklaration-infor-vaccination-abc123"
    assert q["language"] == "sv" and q["publisher"] == "Region Exempel" and q["text"]["status"] == "generated"
    assert fhir.extensions_used(q) == set()                                 # inga extensions som standard
    assert build.extensions_skipped == {REGEX_EXTENSION}
    items = {i["linkId"]: i for i in flat(q["item"])}
    assert items["personnummer"] == {
        "linkId": "personnummer",
        "definition": "http://hl7.se/fhir/ig/base/StructureDefinition/SEBasePatient#Patient.identifier:personnummer.value",
        "prefix": "1", "text": "Personnummer", "type": "string", "required": True, "maxLength": 13}
    assert items["hsa"]["definition"].endswith("SEBaseOrganization#Organization.identifier:hsaid.value")
    assert items["hsa"]["readOnly"] is True
    assert items["mottagning"]["definition"].endswith("SEBaseOrganization#Organization.name")
    allergi = items["allergi"]
    assert [o["valueCoding"]["code"] for o in allergi["answerOption"]] == ["ja", "nej", "vet-inte"]
    system = allergi["answerOption"][0]["valueCoding"]["system"]
    assert system.endswith("/CodeSystem/halsodeklaration-infor-vaccination-abc123-allergi")
    vad = items["allergi-vad"]
    assert vad["enableWhen"] == [{"question": "allergi", "operator": "=",
                                  "answerCoding": {"system": system, "code": "ja", "display": "Ja"}}]
    assert vad["item"] == [{"linkId": "allergi-vad-hjalp", "text": "Ange läkemedel, livsmedel eller annat.",
                            "type": "display"}]                              # hjälptext utan extension
    assert items["info"]["type"] == "display" and "required" not in items["info"]
    assert build.warnings == []


def test_allowed_extension_and_valueset_mode(draft):
    cfg = Config(canonical_base=CFG.canonical_base, allowed_extensions=frozenset({REGEX_EXTENSION}),
                 answer_options="valueset")
    build = fhir.questionnaire("abc123def456", draft, 1, cfg, approved=True)
    items = {i["linkId"]: i for i in flat(build.questionnaire["item"])}
    assert items["personnummer"]["extension"] == [{"url": REGEX_EXTENSION, "valueString": fhir.PERSONAL_ID}]
    assert "answerOption" not in items["allergi"] and items["allergi"]["answerValueSet"].endswith("-allergi")
    assert build.questionnaire["status"] == "active" and build.questionnaire["version"] == "1.0.1"
    valueset, codesystem = fhir.valueset_resources(build.valuesets[0], cfg)
    assert valueset["compose"]["include"][0]["system"] == codesystem["url"] and codesystem["count"] == 3


def test_conditions_and_structure_are_repaired_with_warnings():
    d = draft_dict()
    d["items"] = [
        item("rok", "Röker du?", "boolean"),
        item("antal", "Hur många per dag?", "integer",
             enable_when=[{"question": "rok", "operator": "=", "answer": "ja"},
                          {"question": "finns-inte", "operator": "=", "answer": "x"}]),
        item("tom", "Tom grupp", "group"),
        item("val", "Välj", "choice"),
        item("pnr", "Personnummer", "integer", semantic="personnummer"),
        item("a", "A", "string", enable_when=[{"question": "rok", "operator": "exists", "answer": None},
                                              {"question": "antal", "operator": ">", "answer": "10"}],
             enable_behavior="any"),
    ]
    build = fhir.questionnaire("f0f0f0f0f0f0", FormDraft.model_validate(d), 1, CFG)
    items = {i["linkId"]: i for i in flat(build.questionnaire["item"])}
    assert items["antal"]["enableWhen"] == [{"question": "rok", "operator": "=", "answerBoolean": True}]
    assert "enableBehavior" not in items["antal"]
    assert items["a"]["enableWhen"][1] == {"question": "antal", "operator": ">", "answerInteger": 10}
    assert items["a"]["enableBehavior"] == "any"
    assert items["tom"]["type"] == "display" and items["val"]["type"] == "string" and items["pnr"]["type"] == "string"
    assert any("finns-inte" in w for w in build.warnings) and any("saknar alternativ" in w for w in build.warnings)


def test_library_valueset_is_used(draft):
    library = {"https://ex.se/ValueSet/janej": {"url": "https://ex.se/ValueSet/janej", "name": "JaNej",
                                                 "title": "Ja/Nej", "uses": 4, "concepts": [
        {"system": "https://ex.se/CodeSystem/janej", "code": "J", "display": "Ja"},
        {"system": "https://ex.se/CodeSystem/janej", "code": "N", "display": "Nej"}]}}
    d = draft.model_copy(deep=True)
    d.item("graviditet").value_set = "https://ex.se/ValueSet/janej"
    items = {i["linkId"]: i for i in flat(fhir.questionnaire("abc123def456", d, 1, CFG, library).questionnaire["item"])}
    assert [o["valueCoding"]["code"] for o in items["graviditet"]["answerOption"]] == ["J", "N"]


def test_provenance_and_transaction_bundle(draft):
    build = fhir.questionnaire("abc123def456", draft, 2, CFG, approved=True)
    revisions = [{"number": 1, "created": "2026-11-09T10:00:00+00:00", "author": "anna", "instruction": None,
                  "provenance": {"model": "claude-opus-5-5"}},
                 {"number": 2, "created": "2026-11-09T10:05:00+00:00", "author": "anna",
                  "instruction": "Gör fråga 3 obligatorisk", "provenance": {"model": "claude-opus-5-5"}}]
    uploads = [{"sha256": "ab" * 32, "filename": "blankett.png", "media_type": "image/png"}]
    prov = fhir.provenance(build.questionnaire, revisions, uploads, "bertil",
                           {"url": "https://ex.se/Questionnaire/original", "title": "Original"}, "2026-11-09T11:00:00Z")
    roles = [(a["type"]["coding"][0]["code"], a["who"].get("display")) for a in prov["agent"]]
    assert roles == [("author", "AI: claude-opus-5-5"), ("enterer", "anna"), ("verifier", "bertil")]
    assert prov["contained"][0]["deviceName"] == [{"name": "claude-opus-5-5", "type": "model-name"}]
    assert prov["activity"]["coding"][0]["code"] == "CREATE"
    shown = [e["what"]["display"] for e in prov["entity"]]
    assert "Underlag: blankett.png (image/png)" in shown and "Kopia av Original" in shown
    assert any("Gör fråga 3 obligatorisk" in d for d in shown)
    bundle = fhir.bundle(build.questionnaire, build.valuesets, prov, CFG)
    assert bundle["type"] == "transaction"
    q_entry, p_entry = bundle["entry"]
    assert q_entry["request"] == {"method": "PUT", "url": f"Questionnaire/{build.questionnaire['id']}"}
    assert p_entry["request"] == {"method": "POST", "url": "Provenance"}
    assert p_entry["resource"]["target"][0]["reference"] == q_entry["fullUrl"]   # löses i transaktionen
    collection = fhir.bundle(build.questionnaire, build.valuesets, prov, CFG, transaction=False)
    assert collection["type"] == "collection" and "request" not in collection["entry"][0]
