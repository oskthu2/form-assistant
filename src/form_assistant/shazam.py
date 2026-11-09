"""Formulär-shazam: känn igen ett formulär som redan finns, så att tjänsten kan skapa en kopia i stället
för att tolka från början.

Signalerna, från billigast och säkrast till bredast:

1. **Samma fil** – SHA-256 av den uppladdade filen.
2. **Samma bild** – perceptuell hash (dHash, 64 bitar) av bilden eller PDF:ens första sida. Den tål
   ny skanning, annan upplösning, komprimering och små förskjutningar, men inte ett annat formulär.
3. **Samma text** – textfingeravtryck (hashade ordtripplar) av PDF:ens textlager eller av ett
   tolkat formulärs frågor, jämfört med Jaccard-likhet.
4. **Samma beskrivning** – fritext ("hälsodeklaration inför vaccination") mot formulärens titlar,
   beskrivningar och frågor, viktat så att ovanliga ord betyder mer (BM25-liknande).

Igenkänningen kör före AI-tolkningen (1, 2 och 3 för PDF med textlager), så att en känd blankett
inte kostar ett AI-anrop, och efter tolkningen (3) som en sista kontroll.
"""

from __future__ import annotations

import io
import json
import math
import re
import zlib
from collections import Counter
from dataclasses import dataclass

from .db import Database
from .model import FormDraft
from .valuesets import normalize

IMAGE_STRONG = 0.90   # dHash-likhet (1 - Hamming/64) som räknas som samma blankett
TEXT_STRONG = 0.60    # Jaccard för textfingeravtryck som räknas som samma formulär


def dhash(image_bytes: bytes) -> str | None:
    """64-bitars differenshash som hex. None om bilden inte går att läsa."""
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            gray = ImageOps.exif_transpose(img).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(gray.tobytes())
    except Exception:  # noqa: BLE001 – trasig eller okänd bildfil
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def pdf_first_page_png(pdf_bytes: bytes) -> bytes | None:
    """PDF:ens första sida som bild (för bildhash), om pypdfium2 finns."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
        page = doc[0].render(scale=1).to_pil()
        out = io.BytesIO()
        page.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def pdf_text(pdf_bytes: bytes, max_pages: int = 20) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
    except Exception:  # noqa: BLE001
        return ""


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return 1 - bin(int(a, 16) ^ int(b, 16)).count("1") / 64


def words(text: str) -> list[str]:
    return [w for w in normalize(text).split() if len(w) > 1]


def text_fingerprint(text: str) -> list[int]:
    """Hashade ordtripplar (shingles); kort text får ordpar och enstaka ord."""
    tokens = words(text)
    if len(tokens) < 3:
        grams = tokens
    else:
        grams = [" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)]
    return sorted({zlib.crc32(g.encode()) for g in grams})


def draft_text(draft: FormDraft) -> str:
    parts = [draft.title, draft.description or ""]
    for item in draft.items:
        parts.append(item.text)
        parts += [o.display for o in item.options]
    return "\n".join(p for p in parts if p)


def jaccard(a: list[int] | set[int], b: list[int] | set[int]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Hit:
    form_id: str
    title: str
    status: str
    score: float
    reason: str
    rank: float = 0.0     # för sortering inom samma score (sökning)


def recognize(db: Database, uploads: list[dict], exclude: str | None = None) -> list[Hit]:
    """Formulär som de uppladdade filerna verkar vara (samma fil, samma bild eller samma text)."""
    best: dict[str, Hit] = {}

    def offer(form_id: str, score: float, reason: str) -> None:
        if form_id == exclude:
            return
        form = db.form(form_id)
        if form is None:
            return
        if form_id not in best or score > best[form_id].score:
            best[form_id] = Hit(form_id, form["title"], form["status"], round(score, 2), reason)

    known = db.all("SELECT u.id, u.sha256, u.dhash, u.text_fingerprint, f.form_id FROM uploads u "
                   "JOIN form_uploads f ON f.upload_id = u.id")
    forms = db.all("SELECT id, fingerprint FROM forms WHERE fingerprint IS NOT NULL")
    for up in uploads:
        for row in known:
            if row["sha256"] == up["sha256"]:
                offer(row["form_id"], 1.0, f"samma fil ({up['filename']})")
            elif up.get("dhash") and row["dhash"]:
                score = similarity(up["dhash"], row["dhash"])
                if score >= IMAGE_STRONG:
                    offer(row["form_id"], score, f"samma blankett i bild ({up['filename']}, {score:.0%} lika)")
        fingerprint = up.get("text_fingerprint")
        if isinstance(fingerprint, str):
            fingerprint = json.loads(fingerprint)
        if fingerprint:
            for row in forms:
                score = jaccard(fingerprint, json.loads(row["fingerprint"]))
                if score >= TEXT_STRONG:
                    offer(row["id"], score, f"samma text ({score:.0%} av textavtrycket)")
    return sorted(best.values(), key=lambda h: -h.score)


def recognize_draft(db: Database, draft: FormDraft, exclude: str | None = None) -> list[Hit]:
    """Efter tolkning: finns ett formulär med nästan samma frågor?"""
    fingerprint = text_fingerprint(draft_text(draft))
    hits = []
    for row in db.all("SELECT id, title, status, fingerprint FROM forms WHERE fingerprint IS NOT NULL"):
        if row["id"] == exclude:
            continue
        score = jaccard(fingerprint, json.loads(row["fingerprint"]))
        if score >= TEXT_STRONG:
            hits.append(Hit(row["id"], row["title"], row["status"], round(score, 2),
                            f"samma frågor ({score:.0%} av textavtrycket)"))
    return sorted(hits, key=lambda h: -h.score)


def search(db: Database, query: str, limit: int = 5) -> list[Hit]:
    """Fritextsökning bland formulären: titel och beskrivning väger tyngst, sedan frågorna."""
    terms = set(words(query))
    if not terms:
        return []
    rows = db.all("SELECT id, title, status, search_text FROM forms")
    docs = []
    for row in rows:
        head = words(row["title"])
        body = words(row["search_text"] or "")
        docs.append((row, Counter(head * 3 + body)))
    n = len(docs)
    avg = sum(sum(c.values()) for _, c in docs) / n if n else 1
    df = Counter(t for _, c in docs for t in set(c) if t in terms)
    hits = []
    for row, counts in docs:
        length = sum(counts.values()) or 1
        score = 0.0
        for t in terms:
            tf = counts.get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
        matched = sum(1 for t in terms if counts.get(t))
        if score > 0:
            relative = matched / len(terms)
            hits.append(Hit(row["id"], row["title"], row["status"], round(relative, 2),
                            f"{matched} av {len(terms)} sökord", rank=score))
    hits.sort(key=lambda h: (-h.score, -h.rank))
    return hits[:limit]


def search_text(draft: FormDraft) -> str:
    return re.sub(r"\s+", " ", draft_text(draft))[:20000]
