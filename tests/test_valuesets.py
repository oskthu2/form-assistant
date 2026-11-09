"""Urvalsbiblioteket: spara vid godkännande, återanvänd likadana, föreslå befintliga."""

from form_assistant import valuesets
from form_assistant.config import Config
from form_assistant.db import Database
from form_assistant.model import FormDraft

from conftest import draft_dict, item

CFG = Config(canonical_base="https://fhir.region-exempel.se/formular")


def test_normalize_and_compare():
    assert valuesets.normalize(" 1. Vet  inte! ") == "vet inte"
    assert valuesets.normalize("Ja,") == "ja"
    score, coverage = valuesets.compare(["Ja", "Nej"], [{"display": "ja"}, {"display": "nej"}, {"display": "Vet ej"}])
    assert (round(score, 2), coverage) == (0.67, 1.0)


def test_save_reuse_and_suggest(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    first = FormDraft.model_validate(draft_dict())
    updated, log = valuesets.save_from_draft(db, "aaaaaaaaaaaa", first, CFG)
    assert len(db.valuesets()) == 2 and all("nytt urval" in line for line in log)
    allergi_url = updated.item("allergi").value_set
    assert allergi_url.endswith("/ValueSet/halsodeklaration-infor-vaccination-aaaaaa-allergi")

    # Ett annat formulär med samma alternativ (annan stavning) återanvänder urvalet.
    d = draft_dict()
    d["title"] = "Annat formulär"
    d["items"] = [item("q1", "Har du allergier?", "choice", options=[{"code": None, "display": "ja"},
                                                                       {"code": None, "display": "NEJ"},
                                                                       {"code": None, "display": "Vet inte."}]),
                  item("q2", "Kön", "choice", options=[{"code": None, "display": "Kvinna"},
                                                       {"code": None, "display": "Man"}])]
    other = FormDraft.model_validate(d)
    library = db.valuesets()
    suggestions = valuesets.suggestions(other, library)
    assert suggestions["q1"][0].kind == "samma" and suggestions["q1"][0].url == allergi_url
    assert "q2" not in suggestions

    # Ja/Nej mot ett bibliotek med Ja/Nej/Vet inte: ett mer heltäckande urval föreslås.
    janej = FormDraft.model_validate(draft_dict() | {"items": [item("g", "Gravid?", "choice", options=[
        {"code": None, "display": "Ja"}, {"code": None, "display": "Nej"}])]})
    kinds = {m.url: m.kind for m in valuesets.matches(janej.items[0], library)}
    assert kinds[allergi_url] == "mer heltäckande"

    updated_other, log = valuesets.save_from_draft(db, "bbbbbbbbbbbb", other, CFG)
    assert updated_other.item("q1").value_set == allergi_url and "återanvänt" in log[0]
    assert [o.display for o in updated_other.item("q1").options] == ["Ja", "Nej", "Vet inte"]
    assert db.valueset(allergi_url)["uses"] == 2 and len(db.valuesets()) == 3

    applied = valuesets.apply(janej, "g", db.valueset(allergi_url))
    assert applied.items[0].value_set == allergi_url and len(applied.items[0].options) == 3
