"""Underlaget till Claude: bilder och PDF som innehållsblock, med så få tokens som tolkningen tillåter.

Bilder kostar efter yta (ungefär en token per 28×28 pixlar), och en PDF som skickas som dokument kostar
både text och en bild per sida. Därför:

* **Bilder** skalas ned till ``image_edge`` pixlar på längsta sidan (standard 1600, formulärtext går
  fortfarande att läsa).
* **PDF med textlager** skickas enligt ``pdf_input``:
  ``dokument`` (standard) som hela PDF:en; ``text`` som textlagret sida för sida; ``text-bild`` som
  textlagret plus en liten bild av varje sida, så att layout och kryssrutor syns.
* **Sidor utan textlager** (inskannade) blir bilder som appen själv renderar i lagom upplösning.
  En PDF helt utan textlager skickas alltid så – dokumentformen tillför inget då.
* **Revideringar** skickar normalt bara textlagren (``text_only``); originalfilerna följer med när
  användaren ber om det.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Any

PDF_MODES = ("dokument", "text", "text-bild")
DEFAULT_IMAGE_EDGE = 1600        # pixlar på längsta sidan för bilder och inskannade sidor
PAGE_PREVIEW_EDGE = 1100         # sidbild bredvid textlagret: layouten syns, ungefär 1 100 tokens per A4-sida
MAX_IMAGE_BYTES = 3_500_000      # base64 växer en tredjedel; API:ets gräns är 5 MB per bild
MIN_PAGE_WORDS = 8               # färre ord än så på en sida = inget användbart textlager


class UnderlagError(Exception):
    """En fil går inte att läsa."""


@dataclass
class Underlag:
    """Innehållsblocken och en sammanfattning av vad som skickades (sparas i härkomsten)."""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def _image_block(data: bytes, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _b64(data)}}


def prepare_image(data: bytes, media_type: str, edge: int = DEFAULT_IMAGE_EDGE) -> tuple[bytes, str]:
    """Skala ned stora bilder (längsta sidan högst ``edge``) och komprimera vid behov."""
    from PIL import Image, ImageOps
    if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        media_type = "image/png"
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if max(img.size) <= edge and len(data) <= MAX_IMAGE_BYTES:
                return data, media_type
            img.thumbnail((edge, edge))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=88)
            return out.getvalue(), "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        raise UnderlagError(f"Bilden går inte att läsa ({exc}).") from exc


def page_texts(data: bytes) -> list[str] | None:
    """Textlagret sida för sida, eller None om PDF:en inte går att läsa här (då skickas den som dokument)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        texts = [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # noqa: BLE001
        return None
    return [_tidy(t) for t in texts]


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def has_text(text: str) -> bool:
    return len([w for w in text.split() if any(c.isalpha() for c in w)]) >= MIN_PAGE_WORDS


def render_page(data: bytes, index: int, edge: int) -> bytes:
    """En PDF-sida som JPEG med längsta sidan ``edge`` pixlar."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(data)
        page = doc[index]
        width, height = page.get_size()
        image = page.render(scale=edge / max(width, height)).to_pil()
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=82)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise UnderlagError(f"Sidan {index + 1} går inte att visa som bild ({exc}).") from exc


def _pdf(n: int, up: dict, mode: str, edge: int, text_only: bool, out: Underlag) -> None:
    name, data = up["filename"], up["data"]
    texts = page_texts(data)
    if texts is None or not texts:
        if text_only:
            out.summary.append(f"{name}: utelämnad (går inte att läsa ut)")
            return
        out.blocks.append({"type": "text", "text": f"Fil {n}: {name} (PDF)"})
        out.blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": _b64(data)}})
        out.summary.append(f"{name}: PDF-dokument")
        return
    with_text = [has_text(t) for t in texts]
    if any(with_text) and mode == "dokument" and not text_only:
        out.blocks.append({"type": "text", "text": f"Fil {n}: {name} (PDF)"})
        out.blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": _b64(data)}})
        out.summary.append(f"{name}: PDF-dokument, {len(texts)} sidor")
        return
    if text_only and not any(with_text):
        out.summary.append(f"{name}: utelämnad (inskannad, inget textlager)")
        return
    label = "textlager" if any(with_text) else "inskannad, sidorna som bilder"
    out.blocks.append({"type": "text", "text": f"Fil {n}: {name} (PDF, {len(texts)} sidor, {label})"})
    counts = {"text": 0, "bild": 0, "utelämnad": 0}
    for i, (text, readable) in enumerate(zip(texts, with_text)):
        if readable:
            out.blocks.append({"type": "text", "text": f"Fil {n}, sida {i + 1} (textlager):\n{text}"})
            counts["text"] += 1
            if mode == "text-bild" and not text_only:
                out.blocks.append({"type": "text", "text": f"Fil {n}, sida {i + 1} (bild av sidan):"})
                out.blocks.append(_image_block(render_page(data, i, PAGE_PREVIEW_EDGE), "image/jpeg"))
                counts["bild"] += 1
        elif text_only:
            counts["utelämnad"] += 1
        else:
            out.blocks.append({"type": "text", "text": f"Fil {n}, sida {i + 1} (inskannad):"})
            out.blocks.append(_image_block(render_page(data, i, edge), "image/jpeg"))
            counts["bild"] += 1
    parts = [f"{v} sidor {k}" for k, v in counts.items() if v]
    out.summary.append(f"{name}: " + ", ".join(parts))


def build(uploads: list[dict], mode: str = "dokument", edge: int = DEFAULT_IMAGE_EDGE,
          text_only: bool = False) -> Underlag:
    """Innehållsblocken för de uppladdade filerna. Det sista blocket får en cache-brytpunkt.

    ``text_only``: bara PDF:ernas textlager (för revideringar utan originalfilerna); bilder och
    inskannade sidor utelämnas."""
    if mode not in PDF_MODES:
        raise ValueError(f"Okänt PDF-läge {mode!r} – välj {', '.join(PDF_MODES)}.")
    out = Underlag()
    for n, up in enumerate(uploads, start=1):
        if up["media_type"] == "application/pdf":
            _pdf(n, up, mode, edge, text_only, out)
        elif text_only:
            out.summary.append(f"{up['filename']}: utelämnad (bild)")
        else:
            data, media_type = prepare_image(up["data"], up["media_type"], edge)
            out.blocks.append({"type": "text", "text": f"Fil {n}: {up['filename']} (sida/bild {n})"})
            out.blocks.append(_image_block(data, media_type))
            out.summary.append(f"{up['filename']}: bild")
    if out.blocks:
        out.blocks[-1] = out.blocks[-1] | {"cache_control": {"type": "ephemeral"}}
    return out
