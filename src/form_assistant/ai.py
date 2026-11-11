"""AI-delen: Claude tolkar formulärbilder eller PDF till ett formulärutkast och reviderar det på instruktion.

* **Strukturerad output.** Claude svarar i ett kompakt schema (``AiDraft``) där fält med standardvärde
  utelämnas; koden gör om det till ett ``FormDraft`` och bygger FHIR. Schemat är platt eftersom
  strukturerad output inte tillåter rekursion. Om API:et skulle avvisa schemat används det fullständiga
  (``FormDraft``) i stället.
* **Revidering som ändringar.** Vid revidering skriver Claude bara det som ändras (``DraftPatch``), och
  originalfilerna skickas bara med när användaren ber om det – annars PDF:ernas textlager.
* **Få tokens i underlaget.** Se ``underlag.py``: nedskalade bilder, valbart PDF-läge
  (dokument/text/text-bild), inskannade sidor som egna bilder.
* **Instruktionsprompten tränas.** Systemprompten består av grundinstruktionen plus de *lärdomar* som
  har godkänts (från användarnas återkoppling). Godkända formulär som liknar det nya skickas med som
  exempel. Urvalsbiblioteket skickas med så att befintliga urval återanvänds.
* **Cachning.** Systemprompten (grundinstruktion + lärdomar) och underlaget ligger först och cachas; det
  som varierar (bibliotek, exempel, utkast, instruktion) ligger sist.
* **Fallback.** Server-side fallback är påslaget (``fallbacks: "default"``): om modellen avböjer tar en
  annan modell över i samma anrop.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import anthropic

from . import underlag
from .config import Config
from .db import now
from .model import AiDraft, DraftPatch, FormDraft, GuidelineSuggestions, compact

MAX_IMAGE_EDGE = underlag.DEFAULT_IMAGE_EDGE
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 64000               # stora formulär (100+ frågor) blir långa svar; kräver strömning

Progress = Callable[[str], None]  # får hela texten hittills, för att visa hur långt Claude har kommit

BASE_INSTRUCTION = """\
Du tolkar svenska formulär och blanketter från bilder eller PDF och beskriver dem strukturerat, så att de kan
bli en FHIR Questionnaire i Ineras formulärtjänst. Beskriv innehållet troget – koden bygger FHIR av din beskrivning.

Så här beskriver du formuläret:
- Ta med varje rubrik, fråga, fält, kryssruta och instruktionstext, i formulärets ordning. Använd formulärets egen
  text ordagrant; rätta inte formuleringar och hitta inte på frågor som inte står där.
- link_id: kort och unikt, små bokstäver, siffror och bindestreck, gärna efter frågans innehåll (t.ex.
  "allergi", "allergi-vad"). Vid revidering: behåll link_id för frågor som finns kvar.
- parent: gruppens link_id. En rubrik med frågor under blir type "group"; frågorna får gruppen som parent.
  En instruktion eller informationstext blir type "display".
- Typ: ja/nej-ruta → boolean. Alternativ där ett väljs → choice. Kryssrutor där flera får väljas → choice med
  repeats true. Alternativ plus "Annat: ____" → open-choice. Kort fritext → string, flera rader → text.
  Datum → date, klockslag → time, tal → integer eller decimal.
- prefix: formulärets numrering ("1", "2b"). required: när formuläret anger att fältet måste fyllas i
  (t.ex. asterisk), annars false.
- semantic: när fältet avser patienten (den formuläret gäller) – personnummer, samordningsnummer, förnamn,
  efternamn, fullständigt namn, telefon, e-post, adress, postnummer, postort. När fältet avser vårdgivaren eller
  mottagningen – organisation_namn, hsaid, organisationsnummer. Underskriftsrad → underskrift. Annars "ingen".
  Ett fält för "personnummer/samordningsnummer" → personnummer_eller_samordningsnummer.
- Villkor: "Om ja, ange …" blir en följdfråga med enable_when mot frågan den beror på (svaret som text).
- Urval: biblioteket med befintliga urval följer nedan. Om ett befintligt urval har samma alternativ som
  formuläret, eller samma betydelse med en mer standardiserad formulering, sätt value_set till dess URL och
  använd dess alternativ. Annars value_set null och formulärets egna alternativ.
- uncertainty: skriv vad som är osäkert (svårläst text, otydligt om flera val är tillåtna). notes: övergripande
  iakttagelser, t.ex. delar som inte går att läsa eller förslag som kräver ett beslut.
- source: var på formuläret frågan finns (sida och läge), så att en människa kan kontrollera tolkningen.
- Utelämna fält som har sitt standardvärde (false, tom lista, semantic "ingen", inget värde) – skriv bara ut
  det som gäller frågan.
- En PDF kan komma som utläst text (textlager) i stället för som dokument. Då syns inte layouten: kryssrutor,
  rutor och linjer kan saknas eller synas som enstaka tecken. Använd sidbilderna när de finns, och skriv i
  uncertainty när frågetypen inte går att avgöra från texten."""

REVISE_INSTRUCTION = """\
Du får formulärets nuvarande beskrivning (JSON, fält med standardvärde är utelämnade) och en instruktion från
användaren. Returnera bara ändringarna, inte hela formuläret:
- changes: varje fråga som ändras (action "ändra", hela frågan efter ändringen, samma link_id, after tom) eller
  läggs till (action "lägg_till", after = link_id för frågan den nya ska stå direkt efter, en grupps link_id för
  först i gruppen, tom text för sist i sin grupp).
- remove: link_id för frågor som tas bort; en grupps frågor tas bort med den. Flytta en fråga: ta bort den och
  lägg till den på den nya platsen med samma link_id.
- header: ändrad titel, beskrivning eller syfte, annars tom lista.
- notes: hela listan med iakttagelser efter ändringen; behåll de som fortfarande gäller.
Ändra bara det instruktionen gäller; behåll link_id för allt annat. Om instruktionen strider mot det som står på
formuläret, gör ändringen men skriv det i notes. Om originalet inte följer med och instruktionen kräver att du
ser formuläret, gör det du kan och skriv i notes att originalfilerna behöver skickas med."""

REVISE_FULL_INSTRUCTION = """\
Du får formulärets nuvarande beskrivning (JSON) och en instruktion från användaren. Gör ändringen och returnera
hela formuläret. Ändra bara det instruktionen gäller; behåll link_id, ordning och allt annat. Om instruktionen
strider mot det som står på formuläret, gör ändringen men skriv det i notes."""

GUIDELINE_INSTRUCTION = """\
Du förbättrar instruktionerna till en AI som tolkar formulär till FHIR Questionnaire. Du får användarnas
återkoppling (instruktioner de gett när de rättat AI:ns tolkningar) och de lärdomar som redan gäller. Föreslå
högst fem nya, allmänna lärdomar: korta instruktioner som gör att samma rättelse inte behövs nästa gång. Bara
mönster som återkommer eller tydligt gäller fler formulär – inga engångsrättelser, och inget som redan står i
lärdomarna."""


class AiError(Exception):
    """AI-anropet gav inget användbart svar (visas för användaren)."""


class SchemaRejected(AiError):
    """API:et avvisade svarsschemat (t.ex. för komplext) – då används det fullständiga schemat."""


def system_prompt(guidelines: list[str]) -> list[dict[str, Any]]:
    text = BASE_INSTRUCTION
    if guidelines:
        text += "\n\nLärdomar från tidigare formulär – följ dem:\n" + "\n".join(f"- {g}" for g in guidelines)
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def prepare_image(data: bytes, media_type: str, edge: int = MAX_IMAGE_EDGE) -> tuple[bytes, str]:
    try:
        return underlag.prepare_image(data, media_type, edge)
    except underlag.UnderlagError as exc:
        raise AiError(str(exc)) from exc


def file_blocks(uploads: list[dict], cfg: Config | None = None, text_only: bool = False) -> underlag.Underlag:
    """Bilder och PDF:er som innehållsblock enligt inställningarna (PDF-läge, bildstorlek)."""
    cfg = cfg or Config()
    try:
        return underlag.build(uploads, cfg.pdf_input, cfg.image_edge, text_only)
    except underlag.UnderlagError as exc:
        raise AiError(str(exc)) from exc


def count_items(text: str) -> int:
    """Antal frågor i ett delvis skrivet svar (för framstegsvisning)."""
    return text.count('"link_id"')


def library_text(library: list[dict], limit: int = 80) -> str:
    if not library:
        return "Urvalsbiblioteket är tomt."
    lines = ["Urvalsbiblioteket (url: titel – alternativ):"]
    for vs in library[:limit]:
        options = ", ".join(c["display"] for c in vs["concepts"][:25])
        lines.append(f"- {vs['url']}: {vs['title']} – {options} (används i {vs['uses']} formulär)")
    return "\n".join(lines)


def examples_text(examples: list[FormDraft]) -> str:
    if not examples:
        return ""
    parts = ["Godkända formulär som exempel på hur liknande formulär har beskrivits:"]
    for ex in examples:
        short = compact(ex, limit=30)
        parts.append(json.dumps({"title": short["title"], "items": short["items"]}, ensure_ascii=False))
    return "\n".join(parts)


async def _parse(client, cfg: Config, system: list[dict], content: list[dict], output: type,
                 progress: Progress | None = None) -> tuple[Any, dict]:
    """Strömmat anrop med strukturerad output: stora formulär tar minuter och blir långa svar."""
    try:
        async with client.beta.messages.stream(
            model=cfg.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=output,
            output_config={"effort": "high"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            written = []
            async for text in stream.text_stream:
                written.append(text)
                if progress:
                    progress("".join(written))
            response = await stream.get_final_message()
    except anthropic.BadRequestError as exc:
        if "schema" in str(exc).lower():
            raise SchemaRejected(f"Svarsschemat avvisades: {exc}") from exc
        raise
    if response.stop_reason == "refusal":
        raise AiError("Claude avböjde att tolka underlaget.")
    if response.stop_reason == "max_tokens":
        raise AiError("Svaret blev för långt även med den högsta gränsen. Dela upp formuläret (t.ex. några sidor i "
                      "taget) och slå ihop resultaten genom att iterera.")
    if response.parsed_output is None:
        raise AiError("Claude gav inget tolkningsbart svar.")
    usage = getattr(response, "usage", None)
    meta = {"model": getattr(response, "model", None) or cfg.model, "request_id": getattr(response, "id", None),
            "created": now(), "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                                        "output_tokens": getattr(usage, "output_tokens", None),
                                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                                        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens",
                                                                               None)}
            if usage else None}
    return response.parsed_output, meta


async def interpret(client, cfg: Config, uploads: list[dict], library: list[dict], guidelines: list[str],
                    examples: list[FormDraft], hint: str | None = None,
                    progress: Progress | None = None) -> tuple[FormDraft, dict]:
    """Tolka uppladdade bilder/PDF till ett formulärutkast."""
    if not uploads:
        raise AiError("Ladda upp minst en bild eller PDF.")
    files = file_blocks(uploads, cfg)
    context = [library_text(library), examples_text(examples)]
    if hint:
        context.append(f"Användarens beskrivning av formuläret: {hint}")
    context.append("Beskriv formuläret i filerna ovan.")
    content = files.blocks + [{"type": "text", "text": "\n\n".join(c for c in context if c)}]
    system = system_prompt(guidelines)
    try:
        result, meta = await _parse(client, cfg, system, content, AiDraft, progress)
        draft = result.to_draft()
    except SchemaRejected:
        draft, meta = await _parse(client, cfg, system, content, FormDraft, progress)
        meta["schema"] = "fullständigt"
    return draft, meta | {"kind": "ai-tolkning", "underlag": files.summary}


async def revise(client, cfg: Config, uploads: list[dict], library: list[dict], guidelines: list[str],
                 draft: FormDraft, instruction: str, progress: Progress | None = None,
                 with_files: bool = False) -> tuple[FormDraft, dict]:
    """Revidera utkastet enligt användarens instruktion. Claude skriver bara ändringarna.

    Underlaget är utkastet och PDF:ernas textlager; originalfilerna (bilder, sidbilder) skickas bara med när
    ``with_files`` är satt – det är det som kostar mest vid upprepade revideringar."""
    if not instruction.strip():
        raise AiError("Skriv vad som ska ändras.")
    files = file_blocks(uploads, cfg, text_only=not with_files)
    current = json.dumps(compact(draft), ensure_ascii=False)
    context = [REVISE_INSTRUCTION, library_text(library)]
    if not with_files:
        context.append("Originalfilerna skickas inte med den här gången"
                       + (" (bara PDF:ernas textlager ovan)." if files.blocks else ".")
                       + " Utgå från beskrivningen och dess source-fält.")
    context += [f"Nuvarande beskrivning:\n{current}", f"Instruktion: {instruction.strip()}"]
    content = files.blocks + [{"type": "text", "text": "\n\n".join(context)}]
    system = system_prompt(guidelines)
    log: list[str] = []
    try:
        patch, meta = await _parse(client, cfg, system, content, DraftPatch, progress)
        revised, log = patch.apply(draft)
        meta["changes"] = {"ändrade": sum(c.action == "ändra" for c in patch.changes),
                           "tillagda": sum(c.action == "lägg_till" for c in patch.changes),
                           "borttagna": len(patch.remove)}
    except SchemaRejected:
        context[0] = REVISE_FULL_INSTRUCTION
        context[-2] = f"Nuvarande beskrivning:\n{draft.model_dump_json()}"
        content = files.blocks + [{"type": "text", "text": "\n\n".join(context)}]
        revised, meta = await _parse(client, cfg, system, content, FormDraft, progress)
        meta["schema"] = "fullständigt"
    if log:
        revised = revised.model_copy(update={"notes": revised.notes + [f"Revideringen: {m}" for m in log]})
    return revised, meta | {"kind": "ai-revidering", "instruction": instruction.strip(), "underlag": files.summary,
                            "originalfiler": with_files}


async def suggest_guidelines(client, cfg: Config, feedback: list[dict], guidelines: list[str]
                             ) -> tuple[GuidelineSuggestions, dict]:
    """Föreslå nya lärdomar ur användarnas återkoppling (de godkänns av en människa innan de används)."""
    if not feedback:
        raise AiError("Det finns ingen återkoppling ännu – iterera på några formulär först.")
    lines = [f"- [{f['title']}] {f['instruction']}" for f in feedback]
    text = ("Återkoppling:\n" + "\n".join(lines) + "\n\nLärdomar som redan gäller:\n"
            + ("\n".join(f"- {g}" for g in guidelines) or "(inga)"))
    system = [{"type": "text", "text": GUIDELINE_INSTRUCTION}]
    return await _parse(client, cfg, system, [{"type": "text", "text": text}], GuidelineSuggestions)
