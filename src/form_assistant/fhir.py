"""Formulärutkast -> FHIR R4: Questionnaire, urval (ValueSet + CodeSystem), Provenance och Bundle.

Principer:

* **Få extensions.** Allt uttrycks med element i R4 core: ``type``, ``required``, ``repeats``,
  ``readOnly``, ``maxLength``, ``answerOption``/``answerValueSet``, ``enableWhen``, ``prefix`` och
  ``definition``. Hjälptexter blir ett underordnat ``display``-item i stället för en extension.
  Extensions läggs bara till om de finns i ``Config.allowed_extensions`` (standard: inga), så att
  formuläret går att ladda upp i Ineras formulärtjänst.
* **Svenska basprofiler.** Frågor om personnummer, samordningsnummer, namn, kontaktuppgifter,
  HSA-id och organisationsnummer pekar med ``item.definition`` på elementet i HL7 Sveriges
  SEBasePatient respektive SEBaseOrganization. Det är definitionsbaserad extraktion i core, utan
  extension, och svaren kan föras över till resurser enligt basprofilerna.
* **Koden bygger FHIR, inte AI:n.** AI:n beskriver formuläret (model.FormDraft). Här görs det om
  till FHIR enligt reglerna ovan, med varningar för det som måste anpassas.
"""

from __future__ import annotations

import html
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import REGEX_EXTENSION, Config
from .model import FormDraft, Item

SE_PATIENT = "http://hl7.se/fhir/ig/base/StructureDefinition/SEBasePatient"
SE_ORGANIZATION = "http://hl7.se/fhir/ig/base/StructureDefinition/SEBaseOrganization"
PERSONNUMMER = "http://electronichealth.se/identifier/personnummer"
SAMORDNINGSNUMMER = "http://electronichealth.se/identifier/samordningsnummer"
HSAID = "urn:oid:1.2.752.29.4.19"
ORGANISATIONSNUMMER = "urn:oid:2.5.4.97"

PERSONAL_ID = r"^(19|20)[0-9]{6}[-+]?[0-9]{4}$"   # ÅÅÅÅMMDDNNNN, samordningsnummer har dag + 60

# semantik -> (definition, max_length, regex)
SEMANTICS: dict[str, tuple[str | None, int | None, str | None]] = {
    "personnummer": (f"{SE_PATIENT}#Patient.identifier:personnummer.value", 13, PERSONAL_ID),
    "samordningsnummer": (f"{SE_PATIENT}#Patient.identifier:samordningsnummer.value", 13, PERSONAL_ID),
    "personnummer_eller_samordningsnummer": (f"{SE_PATIENT}#Patient.identifier.value", 13, PERSONAL_ID),
    "fornamn": (f"{SE_PATIENT}#Patient.name.given", None, None),
    "efternamn": (f"{SE_PATIENT}#Patient.name.family", None, None),
    "fullstandigt_namn": (f"{SE_PATIENT}#Patient.name.text", None, None),
    "telefon": (f"{SE_PATIENT}#Patient.telecom.value", None, None),
    "epost": (f"{SE_PATIENT}#Patient.telecom.value", None, None),
    "adress": (f"{SE_PATIENT}#Patient.address.line", None, None),
    "postnummer": (f"{SE_PATIENT}#Patient.address.postalCode", 6, r"^[0-9]{3} ?[0-9]{2}$"),
    "postort": (f"{SE_PATIENT}#Patient.address.city", None, None),
    "organisation_namn": (f"{SE_ORGANIZATION}#Organization.name", None, None),
    "hsaid": (f"{SE_ORGANIZATION}#Organization.identifier:hsaid.value", 31, r"^SE[0-9]{10}-[0-9A-Za-z]+$"),
    "organisationsnummer": (f"{SE_ORGANIZATION}#Organization.identifier:organizationIdentifier.value", 11,
                            r"^[0-9]{6}-?[0-9]{4}$"),
}
IDENTIFIER_SEMANTICS = {"personnummer", "samordningsnummer", "personnummer_eller_samordningsnummer", "hsaid",
                        "organisationsnummer", "postnummer"}
MAX_LENGTH_TYPES = {"string", "text", "url", "open-choice"}

PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
DATA_OPERATION = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"
USER_SYSTEM = "urn:form-assistant:anvandare"
SHA256 = "urn:form-assistant:sha256"
URI = "urn:ietf:rfc:3986"


def slug(text: str, limit: int = 48) -> str:
    """'Hälsodeklaration – vaccin' -> 'halsodeklaration-vaccin' (FHIR-id och URL-del)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].strip("-")
    return value or "formular"


def pascal(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode())
    name = "".join(w[:1].upper() + w[1:] for w in words) or "Formular"
    return name if name[0].isalpha() else f"F{name}"


def form_slug(form_id: str, title: str) -> str:
    return f"{slug(title, 40)}-{form_id[:6]}"


def option_code(option_code_value: str | None, display: str) -> str:
    return (option_code_value or slug(display, 32)).strip() or "alternativ"


@dataclass
class LocalValueSet:
    """Ett urval som hör till ett formulär (sparas i biblioteket när formuläret godkänns)."""

    url: str
    system: str
    name: str
    title: str
    concepts: list[dict[str, str]]

    @property
    def id(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def local_valueset(item: Item, fslug: str, base: str) -> LocalValueSet:
    rid = f"{fslug}-{item.link_id}"[:64].strip("-")
    concepts, used = [], set()
    for opt in item.options:
        code = option_code(opt.code, opt.display)
        while code in used:
            code = f"{code}-{len(used)}"
        used.add(code)
        concepts.append({"code": code, "display": opt.display})
    system = f"{base}/CodeSystem/{rid}"
    return LocalValueSet(url=f"{base}/ValueSet/{rid}", system=system, name=pascal(item.text)[:60],
                         title=item.text, concepts=[c | {"system": system} for c in concepts])


@dataclass
class Build:
    """Resultatet av en generering: Questionnaire, urvalen den använder och varningar."""

    questionnaire: dict[str, Any]
    valuesets: list[LocalValueSet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extensions_skipped: set[str] = field(default_factory=set)


class _Builder:
    def __init__(self, draft: FormDraft, fslug: str, cfg: Config, library: dict[str, dict]):
        self.draft, self.fslug, self.cfg, self.library = draft, fslug, cfg, library
        self.valuesets: dict[str, LocalValueSet] = {}
        self.warnings: list[str] = []
        self.skipped: set[str] = set()
        self.options_by_item: dict[str, list[dict[str, str]]] = {}

    def extension(self, url: str, value: dict) -> list[dict]:
        if url in self.cfg.allowed_extensions:
            return [{"url": url} | value]
        self.skipped.add(url)
        return []

    def options(self, item: Item) -> tuple[list[dict[str, str]], LocalValueSet | None]:
        """Svarsalternativen som kodade begrepp: från biblioteket om frågan pekar på ett urval där."""
        if item.value_set and item.value_set in self.library:
            vs = self.library[item.value_set]
            concepts = [{"system": c["system"], "code": c["code"], "display": c["display"]} for c in vs["concepts"]]
            return concepts, LocalValueSet(url=vs["url"], system=concepts[0]["system"] if concepts else "",
                                           name=vs["name"], title=vs["title"], concepts=concepts)
        if item.value_set:
            self.warnings.append(f"{item.link_id}: urvalet {item.value_set} finns inte i biblioteket – "
                                 "formulärets egna alternativ används.")
        if not item.options:
            return [], None
        local = local_valueset(item, self.fslug, self.cfg.canonical_base)
        return local.concepts, local

    def items(self, item: Item) -> list[dict[str, Any]]:
        """Ett item i FHIR – och, om en rubrik har underfrågor, de underfrågorna som syskon efter den."""
        out = self.item(item)
        if out["type"] == "display":
            return [out] + [x for child in self.draft.children(item.link_id) for x in self.items(child)]
        return [out]

    def item(self, item: Item) -> dict[str, Any]:
        out: dict[str, Any] = {"linkId": item.link_id}
        definition, max_length, regex = SEMANTICS.get(item.semantic, (None, None, None))
        if definition:
            out["definition"] = definition
        if item.prefix:
            out["prefix"] = item.prefix
        out["text"] = item.text
        kind = item.type
        if item.semantic in IDENTIFIER_SEMANTICS and kind not in ("string", "text"):
            kind = "string"
        if item.semantic == "underskrift":
            self.warnings.append(f"{item.link_id}: underskrift – formulärtjänsten signerar normalt med "
                                 "e-legitimation; frågan blir ett textfält.")
            kind = "string"
        children = self.draft.children(item.link_id)
        if kind == "group" and not children:
            self.warnings.append(f"{item.link_id}: gruppen saknar frågor och blev en rubrik (display).")
            kind = "display"
        concepts, vs = ([], None)
        if kind in ("choice", "open-choice"):
            concepts, vs = self.options(item)
            if not concepts:
                self.warnings.append(f"{item.link_id}: flervalsfrågan saknar alternativ och blev ett textfält.")
                kind = "string"
        out["type"] = kind
        if kind != "display":
            if item.required:
                out["required"] = True
            if item.repeats:
                out["repeats"] = True
            if item.read_only and kind != "group":
                out["readOnly"] = True
        limit = item.max_length or max_length
        if limit and kind in MAX_LENGTH_TYPES:
            out["maxLength"] = limit
        if regex and kind in ("string", "text"):
            ext = self.extension(REGEX_EXTENSION, {"valueString": regex})
            if ext:
                out.setdefault("extension", []).extend(ext)
        if concepts:
            self.options_by_item[item.link_id] = concepts
            if self.cfg.answer_options == "valueset" and vs:
                out["answerValueSet"] = vs.url
            else:
                out["answerOption"] = [{"valueCoding": dict(c)} for c in concepts]
            if vs:
                self.valuesets[vs.url] = vs
        nested = [x for c in children for x in self.items(c)] if kind == "group" else []
        if item.help_text:
            if kind == "display":
                out["text"] = f"{item.text}\n{item.help_text}"
            else:
                nested.insert(0, {"linkId": f"{item.link_id}-hjalp", "text": item.help_text, "type": "display"})
        if kind == "display" and children:
            self.warnings.append(f"{item.link_id}: en rubrik kan inte ha underfrågor – de lyftes upp en nivå.")
        if nested:
            out["item"] = nested
        return out

    def enable_when(self, item: Item, out: dict[str, Any]) -> None:
        conditions = []
        for cond in item.enable_when:
            target = self.draft.item(cond.question)
            if target is None:
                self.warnings.append(f"{item.link_id}: villkoret pekar på {cond.question}, som inte finns.")
                continue
            entry: dict[str, Any] = {"question": cond.question, "operator": cond.operator}
            answer = self.answer(target, cond.operator, cond.answer)
            if answer is None:
                self.warnings.append(f"{item.link_id}: villkorets svar '{cond.answer}' passar inte frågan "
                                     f"{cond.question} och togs bort.")
                continue
            entry.update(answer)
            conditions.append(entry)
        if conditions:
            out["enableWhen"] = conditions
            if len(conditions) > 1:
                out["enableBehavior"] = item.enable_behavior or "all"

    def answer(self, target: Item, operator: str, value: str | None) -> dict[str, Any] | None:
        if operator == "exists":
            return {"answerBoolean": str(value).strip().lower() not in ("false", "nej", "0")}
        if value is None:
            return None
        text = str(value).strip()
        kind = target.type
        if target.semantic in IDENTIFIER_SEMANTICS:
            kind = "string"
        if kind == "boolean":
            lowered = text.lower()
            if lowered in ("true", "ja", "yes", "1"):
                return {"answerBoolean": True}
            if lowered in ("false", "nej", "no", "0"):
                return {"answerBoolean": False}
            return None
        if kind == "integer":
            return {"answerInteger": int(text)} if re.fullmatch(r"-?[0-9]+", text) else None
        if kind == "decimal":
            try:
                return {"answerDecimal": float(text.replace(",", "."))}
            except ValueError:
                return None
        if kind in ("date", "dateTime", "time"):
            return {f"answer{kind[0].upper()}{kind[1:]}": text}
        if kind in ("choice", "open-choice"):
            concepts = self.options_by_item.get(target.link_id) or self.options(target)[0]
            match = next((c for c in concepts if text.lower() in (c["display"].lower(), c["code"].lower())), None)
            if match:
                return {"answerCoding": dict(match)}
            return {"answerString": text} if kind == "open-choice" else None
        return {"answerString": text}

    def apply_conditions(self, items: list[dict[str, Any]]) -> None:
        for out in items:
            source = self.draft.item(out["linkId"])
            if source is not None and source.enable_when:
                self.enable_when(source, out)
            self.apply_conditions(out.get("item", []))


def questionnaire(form_id: str, draft: FormDraft, revision: int, cfg: Config, library: dict[str, dict] | None = None,
                  approved: bool = False, derived_from: str | None = None, date: str | None = None) -> Build:
    """Bygg en Questionnaire av utkastet. ``library``: urvalsbiblioteket (url -> urval)."""
    fslug = form_slug(form_id, draft.title)
    builder = _Builder(draft, fslug, cfg, library or {})
    _check_tree(draft, builder.warnings)
    items = [x for i in draft.children(None) for x in builder.items(i)]
    builder.apply_conditions(items)
    q: dict[str, Any] = {
        "resourceType": "Questionnaire", "id": fslug, "url": f"{cfg.canonical_base}/Questionnaire/{fslug}",
        "version": f"1.0.{revision}" if approved else f"0.{revision}.0", "name": pascal(draft.title)[:100],
        "title": draft.title, "status": "active" if approved else "draft", "subjectType": ["Patient"],
    }
    if derived_from:
        q["derivedFrom"] = [derived_from]
    if date:
        q["date"] = date
    if cfg.publisher:
        q["publisher"] = cfg.publisher
    if draft.description:
        q["description"] = draft.description
    if draft.purpose:
        q["purpose"] = draft.purpose
    q["language"] = draft.language or "sv"
    q["item"] = items
    lines = [draft.title] + [f"{i.prefix + ' ' if i.prefix else ''}{i.text}" for i in draft.items
                             if i.type not in ("group", "display")][:200]
    language = q.pop("language")
    q = {"resourceType": q.pop("resourceType"), "id": q.pop("id"), "language": language,
         "text": _narrative(lines, language)} | q
    return Build(questionnaire=q, valuesets=list(builder.valuesets.values()), warnings=builder.warnings,
                 extensions_skipped=builder.skipped)


def _check_tree(draft: FormDraft, warnings: list[str]) -> None:
    seen = set()
    for item in draft.items:
        if item.link_id in seen:
            warnings.append(f"linkId {item.link_id} förekommer flera gånger – svaren går inte att skilja åt.")
        seen.add(item.link_id)
        if item.parent and draft.item(item.parent) is None:
            warnings.append(f"{item.link_id}: gruppen {item.parent} finns inte – frågan visas inte.")


def extensions_used(resource: Any) -> set[str]:
    """Alla extension-URL:er i en resurs (för kontroll mot vitlistan)."""
    found: set[str] = set()
    if isinstance(resource, dict):
        for key, value in resource.items():
            if key in ("extension", "modifierExtension") and isinstance(value, list):
                found |= {e.get("url") for e in value if isinstance(e, dict)}
            found |= extensions_used(value)
    elif isinstance(resource, list):
        for value in resource:
            found |= extensions_used(value)
    return found


def valueset_resources(vs: LocalValueSet | dict, cfg: Config, status: str = "active") -> tuple[dict, dict]:
    """Ett urval som ValueSet + CodeSystem (för answerValueSet-läget och urvalsbiblioteket)."""
    url, name, title, concepts = (vs.url, vs.name, vs.title, vs.concepts) if isinstance(vs, LocalValueSet) else \
        (vs["url"], vs["name"], vs["title"], vs["concepts"])
    rid = url.rsplit("/", 1)[-1]
    systems = sorted({c["system"] for c in concepts})
    narrative = _narrative([f"{c['code']}: {c['display']}" for c in concepts])
    valueset = {"resourceType": "ValueSet", "id": rid, "text": narrative, "url": url, "name": name, "title": title,
                "status": status, "compose": {"include": [
                    {"system": s, "concept": [{"code": c["code"], "display": c["display"]}
                                              for c in concepts if c["system"] == s]} for s in systems]}}
    if cfg.publisher:
        valueset["publisher"] = cfg.publisher
    own = [s for s in systems if s.startswith(cfg.canonical_base)]
    codesystem = None
    if own:
        codesystem = {"resourceType": "CodeSystem", "id": own[0].rsplit("/", 1)[-1], "text": narrative, "url": own[0],
                      "name": name, "title": title, "status": status, "content": "complete",
                      "caseSensitive": True, "valueSet": url,
                      "concept": [{"code": c["code"], "display": c["display"]} for c in concepts
                                  if c["system"] == own[0]]}
        codesystem["count"] = len(codesystem["concept"])
        if cfg.publisher:
            codesystem["publisher"] = cfg.publisher
    return valueset, codesystem


def _narrative(lines: list[str], lang: str | None = None) -> dict[str, str]:
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines if line)
    attrs = f' lang="{lang}" xml:lang="{lang}"' if lang else ""
    return {"status": "generated", "div": f'<div xmlns="http://www.w3.org/1999/xhtml"{attrs}><ul>{items}</ul></div>'}


def _agent(role: str, who: dict) -> dict:
    return {"type": {"coding": [{"system": PARTICIPANT, "code": role}]}, "who": who}


def _person(name: str | None) -> dict:
    value = name or "okänd"
    return {"identifier": {"system": USER_SYSTEM, "value": value}, "display": value}


def full_url(resource: dict, cfg: Config) -> str:
    """Stabil urn:uuid per canonical URL – referenser inom bundlen löses mot den i en transaktion."""
    key = resource.get("url") or f"{cfg.canonical_base}/{resource['resourceType']}/{resource['id']}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def provenance(q: dict, revisions: list[dict], uploads: list[dict], approver: str | None,
               derived_from: dict | None, recorded: str, target: str | None = None) -> dict:
    """AI:ns författarskap: modellen som författare (Device), personerna som instruerade som registrerare,
    godkännaren som granskare och underlaget (uppladdade filer, instruktioner, ursprungsformulär)."""
    contained, agents = [], []
    models = list(dict.fromkeys(r["provenance"].get("model") for r in revisions
                                if r.get("provenance") and r["provenance"].get("model")))
    for n, model in enumerate(models, start=1):
        rid = f"ai-{n}"
        contained.append({"resourceType": "Device", "id": rid, "deviceName": [{"name": model, "type": "model-name"}],
                          "type": {"text": "AI-modell (språkmodell)"},
                          **({"manufacturer": "Anthropic"} if model.startswith("claude") else {})})
        agents.append(_agent("author", {"reference": f"#{rid}", "display": f"AI: {model}"}))
    people = list(dict.fromkeys(r["author"] for r in revisions if r.get("author")))
    if not models:
        agents += [_agent("author", _person(p)) for p in people]
    else:
        agents += [_agent("enterer", _person(p)) for p in people]
    if approver:
        agents.append(_agent("verifier", _person(approver)))
    entities = [{"role": "source", "what": {"identifier": {"system": SHA256, "value": u["sha256"]},
                                            "display": f"Underlag: {u['filename']} ({u['media_type']})"}}
                for u in uploads]
    if derived_from:
        entities.append({"role": "derivation", "what": {"identifier": {"system": URI, "value": derived_from["url"]},
                                                        "display": f"Kopia av {derived_from['title']}"}})
    for r in revisions:
        if r.get("instruction"):
            entities.append({"role": "source", "what": {
                "display": f"Instruktion till version {r['number']}: {r['instruction'][:500]}"}})
    lines = [f"{'Kopia av ' + derived_from['title'] + ' – ' if derived_from else ''}"
             f"{q['title']} version {q['version']}, skapad med AI-stöd"]
    lines += [a["who"].get("display", "") + f" ({a['type']['coding'][0]['code']})" for a in agents]
    lines += [e["what"]["display"] for e in entities]
    resource = {
        "resourceType": "Provenance", "id": f"prov-{q['id']}"[:64], "text": _narrative(lines),
        "contained": contained or None, "target": [{"reference": target or f"Questionnaire/{q['id']}",
                                                    "display": f"{q['url']}|{q['version']}"}],
        "occurredPeriod": {"start": revisions[0]["created"], "end": revisions[-1]["created"]} if revisions else None,
        "recorded": recorded,
        "activity": {"coding": [{"system": DATA_OPERATION, "code": "CREATE"}],
                     "text": "Formulär skapat med AI-stöd" + (" som kopia" if derived_from else "")},
        "agent": agents or [_agent("author", _person(None))], "entity": entities or None,
    }
    return {k: v for k, v in resource.items() if v is not None}


def bundle(q: dict, valuesets: list[LocalValueSet], prov: dict, cfg: Config, transaction: bool = True,
           include_valuesets: bool | None = None) -> dict:
    """Allt som laddas upp till formulärtjänsten. Transaktion: PUT för Questionnaire och urval (idempotent),
    POST för Provenance."""
    include_valuesets = cfg.answer_options == "valueset" if include_valuesets is None else include_valuesets
    resources = [q]
    if include_valuesets:
        for vs in valuesets:
            valueset, codesystem = valueset_resources(vs, cfg, status=q["status"])
            resources.append(valueset)
            if codesystem:
                resources.append(codesystem)
    entries = []
    for r in resources:
        entry: dict[str, Any] = {"fullUrl": full_url(r, cfg), "resource": r}
        if transaction:
            entry["request"] = {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}
        entries.append(entry)
    prov = prov | {"target": [t | {"reference": full_url(q, cfg)} for t in prov["target"]]}
    prov_entry: dict[str, Any] = {"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": prov}
    if transaction:
        prov_entry["request"] = {"method": "POST", "url": "Provenance"}
    entries.append(prov_entry)
    return {"resourceType": "Bundle", "type": "transaction" if transaction else "collection", "entry": entries}
