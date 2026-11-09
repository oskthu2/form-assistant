"""Inställningar, från miljövariabler.

    FORM_DB                  SQLite-databasen (standard ./data/form-assistant.db)
    FORM_CANONICAL_BASE      bas för canonical URL:er (Questionnaire, ValueSet, CodeSystem)
    FORM_PUBLISHER           utgivare i resurserna
    FORM_ALLOWED_EXTENSIONS  extensions som får användas, kommaseparerade (standard: inga)
    FORM_ANSWER_OPTIONS      inline (standard) = svarsalternativ i formuläret (answerOption),
                             valueset = answerValueSet + ValueSet/CodeSystem i bundlen
    FORM_MODEL               Claude-modell (standard claude-opus-5-5)

Ineras formulärtjänst: IG:n för Questionnaire (Inera FHIR Forms) är ännu inte publicerad.
Därför används som standard bara element i FHIR R4 core och inga extensions. Den som vet att
tjänsten stöder en viss extension lägger till den i FORM_ALLOWED_EXTENSIONS, t.ex.
``http://hl7.org/fhir/StructureDefinition/regex`` för validering av personnummer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REGEX_EXTENSION = "http://hl7.org/fhir/StructureDefinition/regex"


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("data/form-assistant.db")
    canonical_base: str = "http://example.org/fhir"
    publisher: str | None = None
    allowed_extensions: frozenset[str] = field(default_factory=frozenset)
    answer_options: str = "inline"
    model: str = "claude-opus-5-5"

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.environ.get("FORM_ANSWER_OPTIONS", "inline").strip().lower()
        if mode not in ("inline", "valueset"):
            raise ValueError("FORM_ANSWER_OPTIONS ska vara inline eller valueset.")
        allowed = {e.strip() for e in os.environ.get("FORM_ALLOWED_EXTENSIONS", "").split(",") if e.strip()}
        return cls(
            db_path=Path(os.environ.get("FORM_DB", "data/form-assistant.db")),
            canonical_base=os.environ.get("FORM_CANONICAL_BASE", "http://example.org/fhir").rstrip("/"),
            publisher=os.environ.get("FORM_PUBLISHER") or None,
            allowed_extensions=frozenset(allowed),
            answer_options=mode,
            model=os.environ.get("FORM_MODEL", "claude-opus-5-5"),
        )
