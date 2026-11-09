"""Formulärutkastet: det AI:n beskriver och det som versioneras. FHIR byggs av koden (fhir.py).

Strukturerad output tillåter inte rekursiva scheman, så frågorna är en platt lista där
``parent`` pekar på gruppens ``link_id``. Alla fält är obligatoriska (med ``null`` där inget
finns), så att samma modell kan användas som schema för Claude och för lagring.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ItemType = Literal["group", "display", "string", "text", "integer", "decimal", "date", "dateTime", "time",
                   "boolean", "choice", "open-choice", "url", "attachment"]

# Vad en fråga betyder när den motsvarar en uppgift i HL7 Sveriges basprofiler (patient, organisation).
Semantic = Literal["ingen", "personnummer", "samordningsnummer", "personnummer_eller_samordningsnummer",
                   "fornamn", "efternamn", "fullstandigt_namn", "telefon", "epost", "adress", "postnummer",
                   "postort", "organisation_namn", "hsaid", "organisationsnummer", "underskrift"]

Operator = Literal["=", "!=", "exists", ">", "<", ">=", "<="]


class Option(BaseModel):
    code: str | None = Field(description="Kod om formuläret anger en, annars null")
    display: str = Field(description="Svarsalternativets text, som på formuläret")


class Condition(BaseModel):
    question: str = Field(description="link_id för frågan villkoret gäller")
    operator: Operator
    answer: str | None = Field(description="Svaret som text (alternativets text, true/false, ett tal), null för exists")


class Item(BaseModel):
    link_id: str = Field(description="Stabilt, unikt id: små bokstäver, siffror och bindestreck, t.ex. 'allergier'")
    parent: str | None = Field(description="link_id för gruppen frågan hör till, null på översta nivån")
    text: str = Field(description="Frågans eller rubrikens text, ordagrant som på formuläret")
    type: ItemType
    prefix: str | None = Field(description="Numrering på formuläret, t.ex. '1' eller '2b'")
    required: bool
    repeats: bool = Field(description="Flera svar tillåtna (t.ex. kryssrutor där flera får väljas)")
    read_only: bool
    max_length: int | None
    semantic: Semantic = Field(description="Vad frågan betyder när den motsvarar en uppgift om patient eller "
                                           "organisation; annars 'ingen'")
    options: list[Option] = Field(description="Svarsalternativ för choice/open-choice, i formulärets ordning")
    value_set: str | None = Field(description="Canonical URL för ett befintligt urval i biblioteket som "
                                              "motsvarar alternativen, annars null")
    enable_when: list[Condition]
    enable_behavior: Literal["all", "any"] | None
    help_text: str | None = Field(description="Hjälptext eller instruktion som hör till frågan")
    source: str | None = Field(description="Var på formuläret frågan finns, t.ex. 'sida 1, överst'")
    uncertainty: str | None = Field(description="Vad som är osäkert i tolkningen, annars null")


class FormDraft(BaseModel):
    title: str
    description: str | None
    purpose: str | None
    language: str = Field(description="BCP-47, oftast 'sv'")
    items: list[Item]
    notes: list[str] = Field(description="Iakttagelser om formuläret: oläsliga partier, antaganden, förslag")

    def item(self, link_id: str) -> Item | None:
        return next((i for i in self.items if i.link_id == link_id), None)

    def children(self, parent: str | None) -> list[Item]:
        return [i for i in self.items if i.parent == parent]


class GuidelineSuggestion(BaseModel):
    text: str = Field(description="Lärdomen som en kort, allmän instruktion för framtida formulär")
    rationale: str = Field(description="Vilken återkoppling lärdomen bygger på")


class GuidelineSuggestions(BaseModel):
    guidelines: list[GuidelineSuggestion]
