"""Urvalsbiblioteket: svarsalternativ sparas som ValueSet och återanvänds.

* **Spara.** När ett formulär godkänns sparas varje flervalsfrågas alternativ som ett urval i
  biblioteket – om inte ett likadant urval redan finns, då återanvänds det i stället.
* **Kolla.** Nya svarsalternativ jämförs med biblioteket: samma alternativ (exakt), ett befintligt
  urval som täcker alla alternativ och fler (mer heltäckande), eller ett närliggande urval.
* **Föreslå bättre.** Ett befintligt urval som används i fler formulär, eller som täcker mer, föreslås
  i stället för formulärets egna alternativ. AI:n får också biblioteket i prompten och kan peka på det.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config
from .db import Database
from .fhir import form_slug, local_valueset
from .model import FormDraft, Item, Option

CHOICE_TYPES = ("choice", "open-choice")


def normalize(text: str) -> str:
    """Jämförbar text: gemener, utan numrering, skiljetecken och diakritiska tecken utom å/ä/ö."""
    value = text.lower().strip()
    value = re.sub(r"^\(?[0-9a-z]{1,2}[.)]\s+", "", value)
    value = "".join(c for c in unicodedata.normalize("NFC", value) if c.isalnum() or c.isspace() or c in "åäö")
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class Match:
    url: str
    title: str
    kind: str              # "samma", "mer heltäckande", "närliggande"
    score: float           # Jaccard-likhet 0–1
    coverage: float        # andel av frågans alternativ som finns i urvalet
    uses: int
    concepts: list[dict]

    @property
    def label(self) -> str:
        return {"samma": "samma alternativ", "mer heltäckande": "täcker alla alternativ och fler",
                "närliggande": "närliggande"}[self.kind]


def compare(options: list[str], concepts: list[dict]) -> tuple[float, float]:
    a = {normalize(o) for o in options if normalize(o)}
    b = {normalize(c["display"]) for c in concepts if normalize(c["display"])}
    if not a or not b:
        return 0.0, 0.0
    return len(a & b) / len(a | b), len(a & b) / len(a)


def matches(item: Item, library: list[dict], threshold: float = 0.5) -> list[Match]:
    """Urval i biblioteket som liknar frågans alternativ, bäst först."""
    if item.type not in CHOICE_TYPES or not item.options:
        return []
    displays = [o.display for o in item.options]
    found = []
    for vs in library:
        if vs["url"] == item.value_set:
            continue
        score, coverage = compare(displays, vs["concepts"])
        if score >= 0.999:
            kind = "samma"
        elif coverage >= 0.999:
            kind = "mer heltäckande"
        elif score >= threshold:
            kind = "närliggande"
        else:
            continue
        found.append(Match(url=vs["url"], title=vs["title"], kind=kind, score=round(score, 2),
                           coverage=round(coverage, 2), uses=vs["uses"], concepts=vs["concepts"]))
    order = {"samma": 0, "mer heltäckande": 1, "närliggande": 2}
    return sorted(found, key=lambda m: (order[m.kind], -m.score, -m.uses))


def suggestions(draft: FormDraft, library: list[dict]) -> dict[str, list[Match]]:
    """link_id -> förslag på befintliga urval, för de frågor som har några."""
    return {i.link_id: m for i in draft.items if (m := matches(i, library))}


def apply(draft: FormDraft, link_id: str, vs: dict) -> FormDraft:
    """Byt en frågas alternativ mot ett urval i biblioteket."""
    updated = draft.model_copy(deep=True)
    item = updated.item(link_id)
    if item is None:
        raise KeyError(link_id)
    item.value_set = vs["url"]
    item.options = [Option(code=c["code"], display=c["display"]) for c in vs["concepts"]]
    if item.type not in CHOICE_TYPES:
        item.type = "choice"
    return updated


def save_from_draft(db: Database, form_id: str, draft: FormDraft, cfg: Config) -> tuple[FormDraft, list[str]]:
    """Vid godkännande: varje flervalsfråga pekar på ett urval i biblioteket – ett befintligt med samma
    alternativ, eller ett nytt som sparas. Returnerar utkastet med value_set ifyllt och vad som hände."""
    updated = draft.model_copy(deep=True)
    library = db.valuesets()
    fslug = form_slug(form_id, draft.title)
    log = []
    for item in updated.items:
        if item.type not in CHOICE_TYPES or len(item.options) < 2:
            continue
        if item.value_set and db.valueset(item.value_set):
            db.use_valueset(item.value_set)
            log.append(f"{item.link_id}: använder urvalet {item.value_set}")
            continue
        same = next((m for m in matches(item, library) if m.kind == "samma"), None)
        if same:
            item.value_set = same.url
            item.options = [Option(code=c["code"], display=c["display"]) for c in same.concepts]
            db.use_valueset(same.url)
            log.append(f"{item.link_id}: samma alternativ som {same.title} – återanvänt")
            continue
        local = local_valueset(item, fslug, cfg.canonical_base)
        db.save_valueset(local.url, local.name, local.title, local.concepts, form_id)
        item.value_set = local.url
        library = db.valuesets()
        log.append(f"{item.link_id}: nytt urval {local.title} sparat")
    return updated, log
