"""AI-delen: Claude tolkar formulärbilder eller PDF till ett formulärutkast och reviderar det på instruktion.

* **Strukturerad output.** Svaret är ett ``FormDraft`` (pydantic) via ``messages.parse``; FHIR byggs av
  koden. Schemat är platt eftersom strukturerad output inte tillåter rekursion.
* **Instruktionsprompten tränas.** Systemprompten består av grundinstruktionen plus de *lärdomar* som
  har godkänts (från användarnas återkoppling). Godkända formulär som liknar det nya skickas med som
  exempel. Urvalsbiblioteket skickas med så att befintliga urval återanvänds.
* **Cachning.** Systemprompten (grundinstruktion + lärdomar) och de uppladdade filerna ligger först och
  cachas; det som varierar (bibliotek, exempel, utkast, instruktion) ligger sist. En revidering av
  samma formulär läser då bilderna ur cachen.
* **Fallback.** Server-side fallback är påslaget (``fallbacks: "default"``): om modellen avböjer tar en
  annan modell över i samma anrop.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any, Callable

from .config import Config
from .db import now
from .model import FormDraft, GuidelineSuggestions

MAX_IMAGE_EDGE = 2000            # pixlar; formulärtext ska gå att läsa
MAX_IMAGE_BYTES = 3_500_000      # base64 växer en tredjedel; API:ets gräns är 5 MB per bild
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
- source: var på formuläret frågan finns (sida och läge), så att en människa kan kontrollera tolkningen."""

REVISE_INSTRUCTION = """\
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


def system_prompt(guidelines: list[str]) -> list[dict[str, Any]]:
    text = BASE_INSTRUCTION
    if guidelines:
        text += "\n\nLärdomar från tidigare formulär – följ dem:\n" + "\n".join(f"- {g}" for g in guidelines)
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def prepare_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Skala ned stora bilder (längsta sidan högst MAX_IMAGE_EDGE) och komprimera vid behov."""
    from PIL import Image, ImageOps
    if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        media_type = "image/png"
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if max(img.size) <= MAX_IMAGE_EDGE and len(data) <= MAX_IMAGE_BYTES:
                return data, media_type
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=88)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        raise AiError(f"Bilden går inte att läsa ({exc}).") from exc


def file_blocks(uploads: list[dict]) -> list[dict[str, Any]]:
    """Bilder och PDF:er som innehållsblock; den sista får en cache-brytpunkt."""
    blocks: list[dict[str, Any]] = []
    for n, up in enumerate(uploads, start=1):
        if up["media_type"] == "application/pdf":
            blocks.append({"type": "text", "text": f"Fil {n}: {up['filename']} (PDF)"})
            blocks.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(up["data"]).decode("ascii")}})
        else:
            data, media_type = prepare_image(up["data"], up["media_type"])
            blocks.append({"type": "text", "text": f"Fil {n}: {up['filename']} (sida/bild {n})"})
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode("ascii")}})
    if blocks:
        blocks[-1] = blocks[-1] | {"cache_control": {"type": "ephemeral"}}
    return blocks


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
        compact = {"title": ex.title, "items": [
            {k: v for k, v in item.model_dump().items() if v not in (None, [], False, "ingen")}
            for item in ex.items[:30]]}
        parts.append(json.dumps(compact, ensure_ascii=False))
    return "\n".join(parts)


async def _parse(client, cfg: Config, system: list[dict], content: list[dict], output: type,
                 progress: Progress | None = None) -> tuple[Any, dict]:
    """Strömmat anrop med strukturerad output: stora formulär tar minuter och blir långa svar."""
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
                                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None)}
            if usage else None}
    return response.parsed_output, meta


async def interpret(client, cfg: Config, uploads: list[dict], library: list[dict], guidelines: list[str],
                    examples: list[FormDraft], hint: str | None = None,
                    progress: Progress | None = None) -> tuple[FormDraft, dict]:
    """Tolka uppladdade bilder/PDF till ett formulärutkast."""
    if not uploads:
        raise AiError("Ladda upp minst en bild eller PDF.")
    context = [library_text(library), examples_text(examples)]
    if hint:
        context.append(f"Användarens beskrivning av formuläret: {hint}")
    context.append("Beskriv formuläret i filerna ovan.")
    content = file_blocks(uploads) + [{"type": "text", "text": "\n\n".join(c for c in context if c)}]
    draft, meta = await _parse(client, cfg, system_prompt(guidelines), content, FormDraft, progress)
    return draft, meta | {"kind": "ai-tolkning"}


async def revise(client, cfg: Config, uploads: list[dict], library: list[dict], guidelines: list[str],
                 draft: FormDraft, instruction: str, progress: Progress | None = None) -> tuple[FormDraft, dict]:
    """Revidera utkastet enligt användarens instruktion (med originalet som underlag)."""
    if not instruction.strip():
        raise AiError("Skriv vad som ska ändras.")
    context = [REVISE_INSTRUCTION, library_text(library),
               f"Nuvarande beskrivning:\n{draft.model_dump_json()}", f"Instruktion: {instruction.strip()}"]
    content = file_blocks(uploads) + [{"type": "text", "text": "\n\n".join(context)}]
    revised, meta = await _parse(client, cfg, system_prompt(guidelines), content, FormDraft, progress)
    return revised, meta | {"kind": "ai-revidering", "instruction": instruction.strip()}


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
