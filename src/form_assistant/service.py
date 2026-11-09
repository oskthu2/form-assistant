"""Arbetsflödet: ladda upp, känn igen, tolka, iterera, godkänn och exportera."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import ai, ecosystems, fhir, shazam, valuesets
from .config import Config
from .db import Database, now
from .model import FormDraft

ACCEPTED = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
MAX_FILE = 20 * 1024 * 1024
MAX_FILES = 10


class ServiceError(Exception):
    pass


def store_upload(db: Database, filename: str, media_type: str, data: bytes) -> dict[str, Any]:
    """Spara en fil med fingeravtryck för igenkänning (filhash, bildhash, textavtryck för PDF)."""
    media_type = (media_type or "").split(";")[0].strip().lower()
    if data[:4] == b"%PDF":
        media_type = "application/pdf"
    if media_type not in ACCEPTED:
        raise ServiceError(f"{filename}: bara bilder (PNG, JPEG, WebP, GIF) och PDF kan läsas in.")
    if len(data) > MAX_FILE:
        raise ServiceError(f"{filename}: filen är större än {MAX_FILE // (1024 * 1024)} MB.")
    if not data:
        raise ServiceError(f"{filename}: filen är tom.")
    sha = hashlib.sha256(data).hexdigest()
    text_fp = None
    if media_type == "application/pdf":
        page = shazam.pdf_first_page_png(data)
        image_hash = shazam.dhash(page) if page else None
        text = shazam.pdf_text(data)
        text_fp = shazam.text_fingerprint(text) if len(shazam.words(text)) >= 8 else None
    else:
        image_hash = shazam.dhash(data)
        if image_hash is None:
            raise ServiceError(f"{filename}: bilden går inte att läsa.")
    uid = db.add_upload(filename, media_type, data, sha, image_hash, text_fp)
    return {"id": uid, "filename": filename, "media_type": media_type, "sha256": sha, "dhash": image_hash,
            "text_fingerprint": text_fp}


def uploads_with_data(db: Database, upload_ids: list[str]) -> list[dict]:
    rows = [db.upload(uid, with_data=True) for uid in upload_ids]
    if any(r is None for r in rows):
        raise ServiceError("En uppladdad fil finns inte längre.")
    return [dict(r) for r in rows]


def guideline_texts(db: Database) -> list[str]:
    return [g["text"] for g in db.guidelines("aktiv")]


def examples_for(db: Database, hint: str | None, exclude: str | None = None, limit: int = 2) -> list[FormDraft]:
    """Godkända formulär att visa som exempel: de som liknar beskrivningen, annars de senast godkända."""
    approved = {f["id"] for f in db.forms("godkänd")} - {exclude}
    chosen = [h.form_id for h in shazam.search(db, hint or "", limit=10) if h.form_id in approved][:limit]
    if len(chosen) < limit:
        chosen += [fid for fid in (f["id"] for f in db.forms("godkänd")) if fid in approved and fid not in chosen]
    return [d for fid in chosen[:limit] if (d := db.draft(fid))]


def _index(draft: FormDraft) -> tuple[list[int], str]:
    return shazam.text_fingerprint(shazam.draft_text(draft)), shazam.search_text(draft)


def form_config(db: Database, cfg: Config, form_id: str) -> tuple[Config, ecosystems.Ecosystem]:
    """Inställningarna för formulärets ekosystem (canonical-bas, utgivare, svarsalternativ, extensions)."""
    form = db.form(form_id)
    eco = ecosystems.get(db, form["ecosystem"] if form else None)
    return ecosystems.config_for(cfg, eco), eco


async def create_form(db: Database, cfg: Config, client, upload_ids: list[str], author: str,
                      hint: str | None = None, ecosystem: str | None = None, progress=None) -> str:
    uploads = uploads_with_data(db, upload_ids)
    eco = ecosystems.get(db, ecosystem)
    draft, meta = await ai.interpret(client, cfg, uploads, db.valuesets(), guideline_texts(db),
                                     examples_for(db, hint), hint, progress)
    form_id = db.create_form(draft.title, author, upload_ids, ecosystem=eco.code)
    fingerprint, text = _index(draft)
    db.add_revision(form_id, draft, author, "ai-tolkning", hint, provenance=meta, fingerprint=fingerprint,
                    search_text=text)
    return form_id


async def revise_form(db: Database, cfg: Config, client, form_id: str, instruction: str, author: str,
                      progress=None) -> int:
    draft = _draft(db, form_id)
    uploads = uploads_with_data(db, [u["id"] for u in db.form_uploads(form_id)])
    revised, meta = await ai.revise(client, cfg, uploads, db.valuesets(), guideline_texts(db), draft, instruction,
                                    progress)
    fingerprint, text = _index(revised)
    return db.add_revision(form_id, revised, author, "ai-revidering", instruction.strip(), provenance=meta,
                           fingerprint=fingerprint, search_text=text)


def save_manual(db: Database, form_id: str, draft: FormDraft, author: str, kind: str, note: str | None) -> int:
    fingerprint, text = _index(draft)
    return db.add_revision(form_id, draft, author, kind, note, fingerprint=fingerprint, search_text=text)


def copy_form(db: Database, form_id: str, author: str, upload_ids: list[str] | None = None,
              ecosystem: str | None = None) -> str:
    """Ett nytt formulär som utgår från ett befintligt (derivedFrom) – i stället för att tolka från början.
    Kopian kan skapas för ett annat ekosystem än originalet."""
    source = db.form(form_id)
    draft = _draft(db, form_id)
    uploads = upload_ids or [u["id"] for u in db.form_uploads(form_id)]
    eco = ecosystems.get(db, ecosystem or source["ecosystem"])
    new_id = db.create_form(draft.title, author, uploads, derived_from=form_id, ecosystem=eco.code)
    save_manual(db, new_id, draft, author, "kopia", f"Kopia av {source['title']} ({form_id})")
    return new_id


def restore(db: Database, form_id: str, number: int, author: str) -> int:
    draft = db.draft(form_id, number)
    if draft is None:
        raise ServiceError("Versionen finns inte.")
    return save_manual(db, form_id, draft, author, "återställd", f"Återställd från version {number}")


def use_valueset(db: Database, form_id: str, link_id: str, url: str, author: str) -> int:
    vs = db.valueset(url)
    if vs is None:
        raise ServiceError("Urvalet finns inte i biblioteket.")
    draft = valuesets.apply(_draft(db, form_id), link_id, vs)
    return save_manual(db, form_id, draft, author, "urval", f"{link_id}: urvalet {vs['title']} ({url})")


def set_ecosystem(db: Database, form_id: str, code: str) -> ecosystems.Ecosystem:
    if db.form(form_id) is None:
        raise ServiceError("Formuläret finns inte.")
    eco = ecosystems.get(db, code)
    if eco.code != code:
        raise ServiceError(f"Ekosystemet {code} finns inte.")
    db.set_ecosystem(form_id, code)
    return eco


def approve(db: Database, cfg: Config, form_id: str, user: str) -> list[str]:
    """Godkänn: urvalen sparas i biblioteket (eller kopplas till befintliga) och formuläret blir aktivt.
    Nya urval får canonical URL:er under formulärets ekosystem."""
    draft = _draft(db, form_id)
    updated, log = valuesets.save_from_draft(db, form_id, draft, form_config(db, cfg, form_id)[0])
    if updated != draft:
        save_manual(db, form_id, updated, user, "urval", "Urvalen kopplade till biblioteket vid godkännande")
    db.approve(form_id, user)
    return log


@dataclass
class Export:
    build: fhir.Build
    provenance: dict
    bundle: dict
    revision: int


def export(db: Database, cfg: Config, form_id: str, number: int | None = None, transaction: bool = True) -> Export:
    form = db.form(form_id)
    if form is None:
        raise ServiceError("Formuläret finns inte.")
    row = db.revision(form_id, number)
    if row is None:
        raise ServiceError("Formuläret har ingen version.")
    draft = FormDraft.model_validate_json(row["draft"])
    cfg, _ = form_config(db, cfg, form_id)
    approved = form["status"] == "godkänd" and number is None
    library = {vs["url"]: vs for vs in db.valuesets()}
    origin = db.form(form["derived_from"]) if form["derived_from"] else None
    origin_url = None
    if origin is not None:
        origin_draft = db.draft(origin["id"])
        if origin_draft:
            origin_cfg, _ = form_config(db, cfg, origin["id"])
            origin_url = fhir.questionnaire(origin["id"], origin_draft, 0, origin_cfg).questionnaire["url"]
    build = fhir.questionnaire(form_id, draft, row["number"], cfg, library, approved=approved,
                               derived_from=origin_url, date=row["created"][:10])
    revisions = [dict(r) | {"provenance": json.loads(r["provenance"]) if r["provenance"] else None}
                 for r in db.revisions(form_id) if r["number"] <= row["number"]]
    uploads = [dict(u) for u in db.form_uploads(form_id)]
    prov = fhir.provenance(build.questionnaire, revisions, uploads, form["approved_by"] if approved else None,
                           {"url": origin_url, "title": origin["title"]} if origin is not None and origin_url
                           else None, now())
    return Export(build, prov, fhir.bundle(build.questionnaire, build.valuesets, prov, cfg, transaction),
                  row["number"])


def _draft(db: Database, form_id: str) -> FormDraft:
    draft = db.draft(form_id)
    if draft is None:
        raise ServiceError("Formuläret finns inte.")
    return draft
