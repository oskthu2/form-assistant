import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from form_assistant.model import FormDraft  # noqa: E402


def item(link_id, text, type, parent=None, **kw):
    base = {"link_id": link_id, "parent": parent, "text": text, "type": type, "prefix": None, "required": False,
            "repeats": False, "read_only": False, "max_length": None, "semantic": "ingen", "options": [],
            "value_set": None, "enable_when": [], "enable_behavior": None, "help_text": None, "source": None,
            "uncertainty": None}
    return base | kw


def draft_dict():
    return {
        "title": "Hälsodeklaration inför vaccination", "description": "Fylls i av patienten före vaccination.",
        "purpose": None, "language": "sv", "notes": ["Fråga 4 var delvis oläslig."],
        "items": [
            item("patient", "Personuppgifter", "group"),
            item("personnummer", "Personnummer", "string", "patient", required=True, semantic="personnummer",
                 prefix="1"),
            item("fornamn", "Förnamn", "string", "patient", semantic="fornamn"),
            item("efternamn", "Efternamn", "string", "patient", semantic="efternamn"),
            item("halsa", "Hälsa", "group"),
            item("feber", "Har du feber idag?", "boolean", "halsa", required=True, prefix="2"),
            item("allergi", "Är du allergisk mot något?", "choice", "halsa", prefix="3",
                 options=[{"code": None, "display": "Ja"}, {"code": None, "display": "Nej"},
                          {"code": None, "display": "Vet inte"}]),
            item("allergi-vad", "Mot vad?", "text", "halsa", help_text="Ange läkemedel, livsmedel eller annat.",
                 enable_when=[{"question": "allergi", "operator": "=", "answer": "Ja"}]),
            item("graviditet", "Är du gravid?", "choice", "halsa", prefix="4",
                 options=[{"code": None, "display": "Ja"}, {"code": None, "display": "Nej"}],
                 uncertainty="Alternativen svårlästa."),
            item("vardgivare", "Vårdgivare", "group"),
            item("mottagning", "Mottagningens namn", "string", "vardgivare", semantic="organisation_namn"),
            item("hsa", "HSA-id", "string", "vardgivare", semantic="hsaid", read_only=True),
            item("info", "Tack för att du fyllt i formuläret.", "display"),
        ],
    }


@pytest.fixture
def draft():
    return FormDraft.model_validate(draft_dict())


# -- testbilder och fejkad Claude -------------------------------------------------------------------

import io  # noqa: E402
import json  # noqa: E402


def form_image(variant: int = 0, size=(1200, 1600), fmt="PNG") -> bytes:
    """En syntetisk blankett: rubrikfält och rutor i ett mönster som beror på variant."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle([w * 0.06, h * 0.04, w * 0.94, h * 0.12], fill=(30, 60, 120))
    rows = 9 if variant == 0 else 4
    for r in range(rows):
        top = h * (0.18 + r * (0.08 if variant == 0 else 0.18))
        draw.rectangle([w * 0.08, top, w * (0.45 if r % 2 else 0.7), top + h * 0.02], fill=(60, 60, 60))
        draw.rectangle([w * 0.75, top, w * 0.8, top + h * 0.03], outline=(0, 0, 0), width=4)
    if variant:
        draw.ellipse([w * 0.3, h * 0.7, w * 0.7, h * 0.95], fill=(200, 40, 40))
    out = io.BytesIO()
    img.save(out, format=fmt, quality=80)
    return out.getvalue()


class FakeClaude:
    """AsyncAnthropic mot en MockTransport: svarar med de utkast som ställts i kö, och sparar förfrågningarna."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self):
        import httpx2
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
        return AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(self.handler)))

    def handler(self, request):
        import httpx2
        body = json.loads(request.content)
        self.requests.append(body | {"_headers": dict(request.headers)})
        payload = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        stop = "end_turn"
        if isinstance(payload, tuple):
            payload, stop = payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return httpx2.Response(200, json={
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 0}})
