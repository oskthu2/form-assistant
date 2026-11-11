"""Formulärutkastet: det AI:n beskriver och det som versioneras. FHIR byggs av koden (fhir.py).

Strukturerad output tillåter inte rekursiva scheman, så frågorna är en platt lista där
``parent`` pekar på gruppens ``link_id``.

``FormDraft`` är det som lagras: alla fält finns, med ``null`` där inget finns. Claude svarar i ett
kompaktare schema (``AiDraft``, ``DraftPatch``) där fält med standardvärde utelämnas – ``null``,
``false`` och tomma listor kostar annars tokens för varje fråga. Det kompakta schemat har inga
unionstyper och håller sig under API:ets gränser för strukturerad output (högst 24 valfria fält och
16 unionstyper per förfrågan, se ``tests/test_model.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

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


# -- Claudes kompakta svarsformat ---------------------------------------------------------------------
# Valfria fält har typen utan null och standardvärdet None: de är inte unionstyper i schemat, och det
# Claude utelämnar blir None här. Ett uttryckligt null i svaret tas bort innan valideringen.

class _Compact(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class AiOption(_Compact):
    display: str = Field(description="Svarsalternativets text, som på formuläret")
    code: str = Field(None, description="Kod om formuläret anger en; utelämna annars")


class AiCondition(_Compact):
    question: str = Field(description="link_id för frågan villkoret gäller")
    operator: Operator
    answer: str = Field(None, description="Svaret som text (alternativets text, true/false, ett tal); "
                                          "utelämna för exists")


class AiItem(_Compact):
    link_id: str = Field(description="Stabilt, unikt id: små bokstäver, siffror och bindestreck, t.ex. 'allergier'")
    text: str = Field(description="Frågans eller rubrikens text, ordagrant som på formuläret")
    type: ItemType
    parent: str = Field(None, description="link_id för gruppen frågan hör till; utelämna på översta nivån")
    prefix: str = Field(None, description="Numrering på formuläret, t.ex. '1' eller '2b'")
    required: bool = False
    repeats: bool = Field(False, description="Flera svar tillåtna (t.ex. kryssrutor där flera får väljas)")
    read_only: bool = False
    max_length: int = None
    semantic: Semantic = Field("ingen", description="Vad frågan betyder när den motsvarar en uppgift om patient "
                                                     "eller organisation")
    options: list[AiOption] = Field(default_factory=list, description="Svarsalternativ för choice/open-choice, "
                                                                      "i formulärets ordning")
    value_set: str = Field(None, description="Canonical URL för ett befintligt urval i biblioteket som "
                                             "motsvarar alternativen")
    enable_when: list[AiCondition] = Field(default_factory=list)
    enable_behavior: Literal["all", "any"] = None
    help_text: str = Field(None, description="Hjälptext eller instruktion som hör till frågan")
    source: str = Field(None, description="Var på formuläret frågan finns, t.ex. 'sida 1, överst'")
    uncertainty: str = Field(None, description="Vad som är osäkert i tolkningen")

    def to_item(self) -> Item:
        return Item.model_validate(self.model_dump())


class AiDraft(_Compact):
    title: str
    language: str = Field(description="BCP-47, oftast 'sv'")
    items: list[AiItem]
    notes: list[str] = Field(description="Iakttagelser om formuläret: oläsliga partier, antaganden, förslag")
    description: str = None
    purpose: str = None

    def to_draft(self) -> FormDraft:
        return FormDraft(title=self.title, description=self.description, purpose=self.purpose,
                         language=self.language, items=[i.to_item() for i in self.items], notes=self.notes)


DEFAULTS = {"parent": None, "prefix": None, "required": False, "repeats": False, "read_only": False,
            "max_length": None, "semantic": "ingen", "options": [], "value_set": None, "enable_when": [],
            "enable_behavior": None, "help_text": None, "source": None, "uncertainty": None}


def compact_item(item: Item) -> dict:
    """Frågan utan fält som har standardvärdet – samma form som Claude svarar i."""
    data = item.model_dump()
    out = {k: v for k, v in data.items() if k not in DEFAULTS or v != DEFAULTS[k]}
    if "options" in out:
        out["options"] = [{k: v for k, v in o.items() if v is not None} for o in out["options"]]
    if "enable_when" in out:
        out["enable_when"] = [{k: v for k, v in c.items() if v is not None} for c in out["enable_when"]]
    return out


def compact(draft: FormDraft, limit: int | None = None) -> dict:
    out = {"title": draft.title, "language": draft.language}
    if draft.description:
        out["description"] = draft.description
    if draft.purpose:
        out["purpose"] = draft.purpose
    out["items"] = [compact_item(i) for i in draft.items[:limit]]
    out["notes"] = draft.notes
    return out


# -- revidering som ändringar ------------------------------------------------------------------------

class HeaderChange(BaseModel):
    field: Literal["title", "description", "purpose"]
    value: str = Field(description="Det nya värdet; tom text tar bort beskrivning eller syfte")


class Change(BaseModel):
    action: Literal["ändra", "lägg_till"]
    after: str = Field(description="lägg_till: link_id för frågan den nya ska stå direkt efter (en grupps "
                                   "link_id = först i gruppen), eller tom text = sist i sin grupp. ändra: tom text")
    item: AiItem = Field(description="Hela frågan efter ändringen (ändra: samma link_id som tidigare)")


class DraftPatch(BaseModel):
    """En revidering som ändringar: bara det som ändras skrivs ut, inte hela formuläret."""
    header: list[HeaderChange] = Field(description="Ändrad titel, beskrivning eller syfte; tom lista annars")
    remove: list[str] = Field(description="link_id för frågor som tas bort (en grupps frågor följer med)")
    changes: list[Change]
    notes: list[str] = Field(description="Hela listan med iakttagelser efter ändringen; behåll de som "
                                         "fortfarande gäller")

    def apply(self, draft: FormDraft) -> tuple[FormDraft, list[str]]:
        """Utkastet med ändringarna, och en logg över det som inte kunde göras som det var tänkt."""
        log: list[str] = []
        items = list(draft.items)

        def descendants(link_id: str) -> set[str]:
            found, frontier = set(), {link_id}
            while frontier:
                frontier = {i.link_id for i in items if i.parent in frontier} - found
                found |= frontier
            return found

        gone: set[str] = set()
        for link_id in self.remove:
            if not any(i.link_id == link_id for i in items):
                log.append(f"{link_id} skulle tas bort men finns inte.")
                continue
            drop = {link_id} | descendants(link_id)
            gone |= drop
            items = [i for i in items if i.link_id not in drop]

        for change in self.changes:
            new = change.item.to_item()
            at = next((n for n, i in enumerate(items) if i.link_id == new.link_id), None)
            if at is not None:
                items[at] = new
                continue
            if change.action == "ändra":
                log.append(f"{new.link_id} skulle ändras men fanns inte – den lades till i stället.")
            items.insert(self._position(items, new, change.after, log), new)

        known = {i.link_id for i in items}
        for n, item in enumerate(items):
            if item.parent and item.parent not in known:
                log.append(f"{item.link_id}: gruppen {item.parent} finns inte – frågan flyttades till översta nivån.")
                items[n] = item = item.model_copy(update={"parent": None})
            kept = [c for c in item.enable_when if c.question in known]
            if len(kept) != len(item.enable_when):
                log.append(f"{item.link_id}: villkor mot borttagna frågor togs bort.")
                items[n] = item.model_copy(update={"enable_when": kept,
                                                   "enable_behavior": item.enable_behavior if kept else None})

        header = {"title": draft.title, "description": draft.description, "purpose": draft.purpose}
        for h in self.header:
            if h.field == "title":
                header["title"] = h.value.strip() or draft.title
            else:
                header[h.field] = h.value.strip() or None
        result = FormDraft(language=draft.language, items=items, notes=self.notes, **header)
        return result, log

    @staticmethod
    def _position(items: list[Item], new: Item, after: str, log: list[str]) -> int:
        if after:
            at = next((n for n, i in enumerate(items) if i.link_id == after), None)
            if at is not None:
                return at + 1
            log.append(f"{new.link_id}: {after} finns inte – frågan lades sist i sin grupp.")
        siblings = [n for n, i in enumerate(items) if i.parent == new.parent]
        if siblings:
            return siblings[-1] + 1
        parent = next((n for n, i in enumerate(items) if i.link_id == new.parent), None)
        return parent + 1 if parent is not None else len(items)
