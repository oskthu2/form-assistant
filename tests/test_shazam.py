"""Formulär-shazam: samma fil, samma blankett i ny bild, samma text och fritextsökning."""

from form_assistant import service, shazam
from form_assistant.db import Database
from form_assistant.model import FormDraft

from conftest import draft_dict, form_image


def test_dhash_tolerates_rescan_but_not_another_form():
    original = shazam.dhash(form_image(0))
    rescanned = shazam.dhash(form_image(0, size=(900, 1200), fmt="JPEG"))
    other = shazam.dhash(form_image(1))
    assert shazam.similarity(original, rescanned) >= shazam.IMAGE_STRONG
    assert shazam.similarity(original, other) < shazam.IMAGE_STRONG
    assert shazam.dhash(b"inte en bild") is None


def test_text_fingerprint_and_draft_recognition(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    draft = FormDraft.model_validate(draft_dict())
    fid = db.create_form(draft.title, "anna", [])
    service.save_manual(db, fid, draft, "anna", "manuell", None)
    same = FormDraft.model_validate(draft_dict() | {"title": "Hälsodeklaration (vaccin)"})
    hits = shazam.recognize_draft(db, same)
    assert hits and hits[0].form_id == fid and hits[0].score >= shazam.TEXT_STRONG
    assert shazam.recognize_draft(db, same, exclude=fid) == []
    different = FormDraft.model_validate(draft_dict() | {"items": [], "title": "Reseräkning"})
    assert shazam.recognize_draft(db, different) == []


def test_recognize_uploads_and_search(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    up = service.store_upload(db, "blankett.png", "image/png", form_image(0))
    draft = FormDraft.model_validate(draft_dict())
    fid = db.create_form(draft.title, "anna", [up["id"]])
    service.save_manual(db, fid, draft, "anna", "manuell", None)

    same_file = service.store_upload(db, "kopia.png", "image/png", form_image(0))
    assert same_file["id"] == up["id"]                                  # samma fil sparas en gång
    hits = shazam.recognize(db, [same_file])
    assert hits[0].form_id == fid and hits[0].score == 1.0 and "samma fil" in hits[0].reason

    rescan = service.store_upload(db, "skanning.jpg", "image/jpeg", form_image(0, size=(1000, 1333), fmt="JPEG"))
    hits = shazam.recognize(db, [rescan])
    assert hits and hits[0].form_id == fid and "samma blankett" in hits[0].reason

    other = service.store_upload(db, "annan.png", "image/png", form_image(1))
    assert shazam.recognize(db, [other]) == []

    found = shazam.search(db, "hälsodeklaration vaccination")
    assert found[0].form_id == fid and found[0].score == 1.0
    assert shazam.search(db, "reseräkning") == []


def test_pdf_text_layer_is_recognized(tmp_path):
    from reportlab_free_pdf import simple_pdf
    db = Database(tmp_path / "db.sqlite")
    draft = FormDraft.model_validate(draft_dict())
    fid = db.create_form(draft.title, "anna", [])
    service.save_manual(db, fid, draft, "anna", "manuell", None)
    pdf = simple_pdf(shazam.draft_text(draft).splitlines())
    up = service.store_upload(db, "blankett.pdf", "application/octet-stream", pdf)
    assert up["media_type"] == "application/pdf" and up["text_fingerprint"]
    hits = shazam.recognize(db, [up])
    assert hits and hits[0].form_id == fid and "samma text" in hits[0].reason


def test_pdf_with_text_is_not_matched_on_shared_cover_page(tmp_path):
    """Två olika PDF:er med samma försättsblad (samma bildhash) men olika text är inte samma formulär."""
    from reportlab_free_pdf import simple_pdf
    db = Database(tmp_path / "db.sqlite")
    first = service.store_upload(db, "a.pdf", "application/pdf", simple_pdf(["Hälsodeklaration inför operation"] * 3 + [
        "Har du feber idag", "Är du allergisk mot något", "Röker du dagligen"]))
    draft = FormDraft.model_validate(draft_dict())
    fid = db.create_form(draft.title, "anna", [first["id"]])
    service.save_manual(db, fid, draft, "anna", "manuell", None)
    other = {"id": "x", "filename": "b.pdf", "media_type": "application/pdf", "sha256": "0" * 64,
             "dhash": first["dhash"], "text_fingerprint": shazam.text_fingerprint("Reseräkning för resor i tjänsten "
                                                                                   "med bil tåg och flyg samt traktamente")}
    assert shazam.recognize(db, [other]) == []
    scanned = other | {"text_fingerprint": None}                         # skannad PDF utan text: bildhashen gäller
    assert shazam.recognize(db, [scanned])[0].form_id == fid
