"""Inställningar, från miljövariabler.

    FORM_DB                  SQLite-databasen (standard ./data/form-assistant.db)
    FORM_MODEL               Claude-modell (standard claude-opus-5-5)
    FORM_PDF_INPUT           hur en PDF med textlager skickas till Claude:
                             dokument (standard) = hela PDF:en (text och en bild per sida),
                             text = bara textlagret (minst tokens, layouten syns inte),
                             text-bild = textlagret plus en liten bild av varje sida
                             Inskannade sidor skickas alltid som bilder.
    FORM_IMAGE_EDGE          bilder skalas ned till så många pixlar på längsta sidan (standard 1600)

Följande fyra blir det förvalda formulärekosystemet "standard" när databasen skapas; därefter
förvaltas ekosystemen i webbklienten (se ecosystems.py):

    FORM_CANONICAL_BASE      bas för canonical URL:er (Questionnaire, ValueSet, CodeSystem)
    FORM_PUBLISHER           utgivare i resurserna
    FORM_ALLOWED_EXTENSIONS  extensions som får användas, kommaseparerade (standard: inga)
    FORM_ANSWER_OPTIONS      inline (standard) = svarsalternativ i formuläret (answerOption),
                             valueset = answerValueSet + ValueSet/CodeSystem i bundlen

Öppet läge (ladda upp och hämta Questionnaire utan inloggning):

    FORM_OPEN                1 (standard) = öppet läge på, 0 = av (bara inloggat läge)
    FORM_OPEN_RETENTION_HOURS  hur länge ett öppet resultat (och dess filer) sparas (standard 24)
    FORM_OPEN_PER_HOUR       tolkningar per klientadress och timme (standard 5)
    FORM_OPEN_PER_DAY        tolkningar totalt per dygn i öppet läge (standard 100)
    FORM_OPEN_MAX_FILES      filer per uppladdning (standard 5)
    FORM_OPEN_MAX_PAGES      sidor per PDF (standard 30)

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
    pdf_input: str = "dokument"
    image_edge: int = 1600
    open_mode: bool = True
    open_retention_hours: float = 24
    open_per_hour: int = 5
    open_per_day: int = 100
    open_max_files: int = 5
    open_max_pages: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.environ.get("FORM_ANSWER_OPTIONS", "inline").strip().lower()
        if mode not in ("inline", "valueset"):
            raise ValueError("FORM_ANSWER_OPTIONS ska vara inline eller valueset.")
        pdf_input = os.environ.get("FORM_PDF_INPUT", "dokument").strip().lower()
        if pdf_input not in ("dokument", "text", "text-bild"):
            raise ValueError("FORM_PDF_INPUT ska vara dokument, text eller text-bild.")
        image_edge = int(os.environ.get("FORM_IMAGE_EDGE", "1600"))
        if not 400 <= image_edge <= 2576:
            raise ValueError("FORM_IMAGE_EDGE ska vara mellan 400 och 2576 pixlar.")
        allowed = {e.strip() for e in os.environ.get("FORM_ALLOWED_EXTENSIONS", "").split(",") if e.strip()}
        return cls(
            db_path=Path(os.environ.get("FORM_DB", "data/form-assistant.db")),
            canonical_base=os.environ.get("FORM_CANONICAL_BASE", "http://example.org/fhir").rstrip("/"),
            publisher=os.environ.get("FORM_PUBLISHER") or None,
            allowed_extensions=frozenset(allowed),
            answer_options=mode,
            model=os.environ.get("FORM_MODEL", "claude-opus-5-5"),
            pdf_input=pdf_input,
            image_edge=image_edge,
            open_mode=os.environ.get("FORM_OPEN", "1").strip() != "0",
            open_retention_hours=float(os.environ.get("FORM_OPEN_RETENTION_HOURS", "24")),
            open_per_hour=int(os.environ.get("FORM_OPEN_PER_HOUR", "5")),
            open_per_day=int(os.environ.get("FORM_OPEN_PER_DAY", "100")),
            open_max_files=int(os.environ.get("FORM_OPEN_MAX_FILES", "5")),
            open_max_pages=int(os.environ.get("FORM_OPEN_MAX_PAGES", "30")),
        )
