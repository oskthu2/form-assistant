"""Formulärekosystem: de formulärtjänster (eller organisationer) formulären skapas för.

Ett ekosystem bestämmer canonical-basen för formulärets URL:er, utgivaren, hur svarsalternativ
uttrycks och vilka extensions som får användas. Ekosystemen lagras som ett eget **urval** i
biblioteket (kind = 'ekosystem'): varje ekosystem är ett begrepp med egenskaperna nedan, och
urvalet kan hämtas som FHIR CodeSystem (med egenskaper) och ValueSet.

Första gången skapas ekosystemet "standard" från miljövariablerna (FORM_CANONICAL_BASE,
FORM_PUBLISHER, FORM_ANSWER_OPTIONS, FORM_ALLOWED_EXTENSIONS). Fler läggs till i webbklienten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .config import Config
from .db import Database

KIND = "ekosystem"
PROPERTIES = {  # egenskap -> (FHIR-typ, beskrivning)
    "canonical-base": ("string", "Bas för canonical URL:er (Questionnaire, ValueSet, CodeSystem)"),
    "publisher": ("string", "Utgivare i resurserna"),
    "answer-options": ("code", "inline = answerOption, valueset = answerValueSet + ValueSet i bundlen"),
    "allowed-extension": ("string", "Extension som formulärtjänsten stöder (en per förekomst)"),
    "default": ("boolean", "Förvalt ekosystem för nya formulär"),
}


class EcosystemError(ValueError):
    pass


@dataclass
class Ecosystem:
    code: str
    display: str
    canonical_base: str
    publisher: str | None = None
    answer_options: str = "inline"
    allowed_extensions: list[str] = field(default_factory=list)
    default: bool = False

    @property
    def example(self) -> bool:
        return bool(re.search(r"//(www\.)?example\.(org|com|net)\b", self.canonical_base))

    def to_concept(self, system: str) -> dict:
        return {"system": system, "code": self.code, "display": self.display, "canonical_base": self.canonical_base,
                "publisher": self.publisher, "answer_options": self.answer_options,
                "allowed_extensions": list(self.allowed_extensions), "default": self.default}

    @classmethod
    def from_concept(cls, c: dict) -> "Ecosystem":
        return cls(code=c["code"], display=c["display"], canonical_base=c["canonical_base"],
                   publisher=c.get("publisher"), answer_options=c.get("answer_options") or "inline",
                   allowed_extensions=list(c.get("allowed_extensions") or []), default=bool(c.get("default")))


def _row(db: Database) -> dict | None:
    rows = db.valuesets(KIND)
    return rows[0] if rows else None


def ensure_default(db: Database, cfg: Config) -> None:
    """Skapa urvalet med ekosystemet 'standard' från miljövariablerna, om det inte finns."""
    if _row(db):
        return
    url = f"{cfg.canonical_base}/ValueSet/formularekosystem"
    standard = Ecosystem(code="standard", display="Standard", canonical_base=cfg.canonical_base,
                         publisher=cfg.publisher, answer_options=cfg.answer_options,
                         allowed_extensions=sorted(cfg.allowed_extensions), default=True)
    db.put_valueset(url, "Formularekosystem", "Formulärekosystem", [standard.to_concept(system_url(url))], KIND)


def system_url(valueset_url: str) -> str:
    return valueset_url.replace("/ValueSet/", "/CodeSystem/")


def all_ecosystems(db: Database) -> list[Ecosystem]:
    row = _row(db)
    return [Ecosystem.from_concept(c) for c in row["concepts"]] if row else []


def get(db: Database, code: str | None) -> Ecosystem:
    """Ekosystemet med koden, annars det förvalda."""
    ecos = all_ecosystems(db)
    if not ecos:
        raise EcosystemError("Det finns inga formulärekosystem.")
    return next((e for e in ecos if e.code == code), None) or next((e for e in ecos if e.default), ecos[0])


def validate(eco: Ecosystem) -> Ecosystem:
    eco.code = eco.code.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", eco.code):
        raise EcosystemError("Koden ska bestå av små bokstäver, siffror och bindestreck (högst 40 tecken).")
    eco.display = eco.display.strip()
    if not eco.display:
        raise EcosystemError("Ange ett namn.")
    eco.canonical_base = eco.canonical_base.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", eco.canonical_base):
        raise EcosystemError("Canonical-basen ska vara en http(s)-adress utan frågetecken eller fragment.")
    if eco.answer_options not in ("inline", "valueset"):
        raise EcosystemError("Svarsalternativen ska vara inline eller valueset.")
    bad = [e for e in eco.allowed_extensions if not re.fullmatch(r"https?://\S+", e)]
    if bad:
        raise EcosystemError(f"Extensions anges med sin URL: {', '.join(bad)}")
    eco.publisher = (eco.publisher or "").strip() or None
    return eco


def save(db: Database, eco: Ecosystem) -> None:
    """Lägg till eller uppdatera ett ekosystem. Är det förvalt blir de andra inte det."""
    eco = validate(eco)
    row = _row(db)
    if row is None:
        raise EcosystemError("Urvalet med ekosystem saknas.")
    ecos = [e for e in all_ecosystems(db) if e.code != eco.code] + [eco]
    if eco.default:
        for e in ecos:
            e.default = e is eco
    elif not any(e.default for e in ecos):
        ecos[0].default = True
    system = system_url(row["url"])
    db.put_valueset(row["url"], row["name"], row["title"], [e.to_concept(system) for e in ecos], KIND)


def config_for(cfg: Config, eco: Ecosystem) -> Config:
    return replace(cfg, canonical_base=eco.canonical_base, publisher=eco.publisher,
                   answer_options=eco.answer_options, allowed_extensions=frozenset(eco.allowed_extensions))


def fhir_resources(db: Database) -> tuple[dict, dict]:
    """Urvalet som FHIR: CodeSystem med egenskaperna och ValueSet med alla ekosystem."""
    row = _row(db)
    if row is None:
        raise EcosystemError("Urvalet med ekosystem saknas.")
    system = system_url(row["url"])
    concepts = []
    for e in all_ecosystems(db):
        props = [{"code": "canonical-base", "valueString": e.canonical_base},
                 {"code": "answer-options", "valueCode": e.answer_options},
                 {"code": "default", "valueBoolean": e.default}]
        if e.publisher:
            props.insert(1, {"code": "publisher", "valueString": e.publisher})
        props += [{"code": "allowed-extension", "valueString": x} for x in e.allowed_extensions]
        concepts.append({"code": e.code, "display": e.display, "property": props})
    codesystem = {"resourceType": "CodeSystem", "id": "formularekosystem", "url": system, "name": "Formularekosystem",
                  "title": "Formulärekosystem", "status": "active", "content": "complete", "caseSensitive": True,
                  "valueSet": row["url"], "count": len(concepts),
                  "property": [{"code": k, "type": t, "description": d} for k, (t, d) in PROPERTIES.items()],
                  "concept": concepts}
    valueset = {"resourceType": "ValueSet", "id": "formularekosystem", "url": row["url"], "name": "Formularekosystem",
                "title": "Formulärekosystem", "status": "active", "compose": {"include": [{"system": system}]}}
    return codesystem, valueset
