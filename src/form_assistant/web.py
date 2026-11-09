"""Webbklienten (Starlette + Jinja2, serverrenderad).

    form-assistant                         # http://127.0.0.1:8000
    form-assistant --host 0.0.0.0          # i Codespaces – låt porten vara privat

Ladda upp bilder eller en PDF av ett formulär. Tjänsten känner först efter om formuläret redan finns
(och erbjuder en kopia), annars tolkar Claude det. Formuläret förhandsvisas som det blir i FHIR, kan
itereras med instruktioner, får förslag på befintliga urval, godkänns och laddas ner som en FHIR-Bundle.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import ai, service, shazam, valuesets
from .config import Config
from .db import Database
from .model import FormDraft

HERE = Path(__file__).parent
KIND_LABELS = {"ai-tolkning": "AI-tolkning", "ai-revidering": "AI-revidering", "kopia": "kopia", "urval": "urval",
               "återställd": "återställd", "manuell": "manuell"}
SEMANTIC_LABELS = {"personnummer": "personnummer (SEBasePatient)", "samordningsnummer": "samordningsnummer (SEBasePatient)",
                   "personnummer_eller_samordningsnummer": "personnummer/samordningsnummer (SEBasePatient)",
                   "fornamn": "förnamn", "efternamn": "efternamn", "fullstandigt_namn": "namn", "telefon": "telefon",
                   "epost": "e-post", "adress": "adress", "postnummer": "postnummer", "postort": "postort",
                   "organisation_namn": "organisation (SEBaseOrganization)", "hsaid": "HSA-id (SEBaseOrganization)",
                   "organisationsnummer": "organisationsnummer (SEBaseOrganization)", "underskrift": "underskrift"}


class SecurityMiddleware:
    """Säkerhetshuvuden och skydd mot formulär som postas från andra webbplatser (som annars kunde
    starta AI-anrop i användarens namn)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            from urllib.parse import urlsplit
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin") or headers.get("referer")
            if origin and origin != "null":
                parts = urlsplit(origin)
                if parts.netloc != headers.get("host", ""):
                    return await HTMLResponse("Begäran kom från en annan webbplats och avvisades.", 403)(
                        scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"), (b"content-security-policy",
                                                           b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                           b"frame-ancestors 'none'")]
            await send(message)
        await self.app(scope, receive, send_with_headers)


def create_app(cfg: Config | None = None, db: Database | None = None, ai_client_factory=None) -> Starlette:
    """``ai_client_factory`` ger en AsyncAnthropic-klient (byts mot en fejkad i testerna)."""
    cfg = cfg or Config.from_env()
    db = db or Database(cfg.db_path)
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(quote=quote, KIND_LABELS=KIND_LABELS, SEMANTIC_LABELS=SEMANTIC_LABELS, cfg=cfg,
                       example_base="example.org" in cfg.canonical_base or "example.com" in cfg.canonical_base)

    def client():
        if ai_client_factory:
            return ai_client_factory()
        from anthropic import AsyncAnthropic
        return AsyncAnthropic()

    def user(request: Request) -> str:
        return (request.cookies.get("anvandare") or "").strip()[:80] or "okänd"

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("errors", [])
        page = env.get_template(name).render(request=request, user=user(request),
                                             api_key=bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(ai_client_factory),
                                             **ctx)
        return HTMLResponse(page, status_code=status)

    def redirect(url: str, msg: str | None = None) -> RedirectResponse:
        return RedirectResponse(url + (f"?{urlencode({'msg': msg})}" if msg else ""), status_code=303)

    def friendly(exc: Exception) -> str:
        if isinstance(exc, (ai.AiError, service.ServiceError)):
            return str(exc)
        text = f"{type(exc).__name__}: {exc}"
        if "api_key" in text.lower() or "authentication" in text.lower():
            return ("Claude kunde inte anropas – ANTHROPIC_API_KEY saknas eller är ogiltig. Lägg nyckeln som "
                    f"miljövariabel (Codespaces-hemlighet) och starta om. ({text})")
        return text

    # -- start: ladda upp, känn igen, tolka ----------------------------------------------------

    async def index(request: Request, errors=None, status=200):
        forms = db.forms()
        return render(request, "index.html", status=status, forms=forms, errors=errors or [],
                      counts={"godkänd": sum(f["status"] == "godkänd" for f in forms),
                              "urval": len(db.valuesets()), "lärdomar": len(db.guidelines("aktiv"))})

    async def upload(request: Request):
        form = await request.form()
        files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        hint = (form.get("hint") or "").strip()[:1000]
        action = form.get("action") or "skapa"
        if not files and not hint:
            return await index(request, ["Ladda upp en bild eller PDF, eller beskriv formuläret du letar efter."], 400)
        if len(files) > service.MAX_FILES:
            return await index(request, [f"Högst {service.MAX_FILES} filer åt gången."], 400)
        stored, errors = [], []
        for f in files:
            try:
                stored.append(service.store_upload(db, f.filename, f.content_type, await f.read()))
            except service.ServiceError as exc:
                errors.append(str(exc))
        if errors:
            return await index(request, errors, 400)
        hits = shazam.recognize(db, stored)
        search_hits = [h for h in shazam.search(db, hint) if h.form_id not in {x.form_id for x in hits}] if hint else []
        if action == "sok" or not stored:
            return render(request, "match.html", hits=hits, search_hits=search_hits, uploads=stored, hint=hint,
                          searched=True)
        if hits:
            return render(request, "match.html", hits=hits, search_hits=search_hits, uploads=stored, hint=hint,
                          searched=False)
        return await interpret(request, [u["id"] for u in stored], hint)

    async def interpret(request: Request, upload_ids: list[str], hint: str | None):
        try:
            form_id = await service.create_form(db, cfg, client(), upload_ids, user(request), hint or None)
        except Exception as exc:  # noqa: BLE001
            return await index(request, [f"Tolkningen misslyckades: {friendly(exc)}"], 502)
        return redirect(f"/f/{form_id}", "Formuläret är tolkat. Granska förhandsvisningen och iterera vid behov.")

    async def interpret_anyway(request: Request):
        form = await request.form()
        return await interpret(request, form.getlist("upload_ids"), (form.get("hint") or "").strip())

    async def search_page(request: Request):
        q = (request.query_params.get("q") or "").strip()
        return render(request, "match.html", hits=[], search_hits=shazam.search(db, q, limit=10) if q else [],
                      uploads=[], hint=q, searched=True)

    # -- formuläret ------------------------------------------------------------------------------

    async def form_page(request: Request):
        fid = request.path_params["fid"]
        form = db.form(fid)
        if form is None:
            return render(request, "error.html", status=404, message="Formuläret finns inte.")
        number = int(request.query_params["v"]) if request.query_params.get("v", "").isdigit() else None
        row = db.revision(fid, number)
        if row is None:
            return render(request, "error.html", status=404, message="Versionen finns inte.")
        draft = FormDraft.model_validate_json(row["draft"])
        latest = db.revision(fid)
        try:
            exp = service.export(db, cfg, fid, number)
        except service.ServiceError as exc:
            return render(request, "error.html", status=404, message=str(exc))
        library = db.valuesets()
        similar = shazam.recognize_draft(db, draft, exclude=fid)
        origin = db.form(form["derived_from"]) if form["derived_from"] else None
        meta = json.loads(row["provenance"]) if row["provenance"] else {}
        return render(request, "form.html", form=form, draft=draft, row=row, latest=latest,
                      is_latest=row["number"] == latest["number"], q=exp.build.questionnaire, build=exp.build,
                      uploads=db.form_uploads(fid), revisions=db.revisions(fid),
                      suggestions=valuesets.suggestions(draft, library), library={v["url"]: v for v in library},
                      similar=similar, origin=origin, meta=meta,
                      uncertain=[i for i in draft.items if i.uncertainty])

    async def revise(request: Request):
        fid = request.path_params["fid"]
        instruction = ((await request.form()).get("instruction") or "").strip()[:4000]
        if not instruction:
            return redirect(f"/f/{fid}", "Skriv vad som ska ändras.")
        try:
            number = await service.revise_form(db, cfg, client(), fid, instruction, user(request))
        except Exception as exc:  # noqa: BLE001
            return redirect(f"/f/{fid}", f"Revideringen misslyckades: {friendly(exc)}")
        return redirect(f"/f/{fid}", f"Version {number} är klar.")

    async def approve(request: Request):
        fid = request.path_params["fid"]
        if user(request) == "okänd":
            return redirect(f"/f/{fid}", "Ange ditt namn uppe till höger innan du godkänner – det blir granskare i "
                                         "Provenance.")
        try:
            log = service.approve(db, cfg, fid, user(request))
        except service.ServiceError as exc:
            return redirect(f"/f/{fid}", str(exc))
        return redirect(f"/f/{fid}", "Formuläret är godkänt. " + ("; ".join(log) if log else ""))

    async def copy(request: Request):
        fid = request.path_params["fid"]
        form = await request.form()
        try:
            new_id = service.copy_form(db, fid, user(request), form.getlist("upload_ids") or None)
        except service.ServiceError as exc:
            return redirect("/", str(exc))
        return redirect(f"/f/{new_id}", "En kopia är skapad – iterera på den i stället för att börja från början.")

    async def use_valueset(request: Request):
        fid = request.path_params["fid"]
        form = await request.form()
        try:
            number = service.use_valueset(db, fid, form.get("link_id") or "", form.get("url") or "", user(request))
        except (service.ServiceError, KeyError) as exc:
            return redirect(f"/f/{fid}", f"Urvalet kunde inte användas: {exc}")
        return redirect(f"/f/{fid}", f"Version {number}: frågan använder nu urvalet från biblioteket.")

    async def restore(request: Request):
        fid = request.path_params["fid"]
        number = int((await request.form()).get("number") or 0)
        try:
            new = service.restore(db, fid, number, user(request))
        except service.ServiceError as exc:
            return redirect(f"/f/{fid}", str(exc))
        return redirect(f"/f/{fid}", f"Version {number} är återställd som version {new}.")

    async def bundle(request: Request):
        fid = request.path_params["fid"]
        number = int(request.query_params["v"]) if request.query_params.get("v", "").isdigit() else None
        transaction = request.query_params.get("typ", "transaction") != "collection"
        try:
            exp = service.export(db, cfg, fid, number, transaction)
        except service.ServiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        name = f"{exp.build.questionnaire['id']}-v{exp.revision}.json"
        return Response(json.dumps(exp.bundle, ensure_ascii=False, indent=2), media_type="application/fhir+json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def questionnaire_json(request: Request):
        fid = request.path_params["fid"]
        number = int(request.query_params["v"]) if request.query_params.get("v", "").isdigit() else None
        try:
            exp = service.export(db, cfg, fid, number)
        except service.ServiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return Response(json.dumps(exp.build.questionnaire, ensure_ascii=False, indent=2),
                        media_type="application/fhir+json")

    async def file(request: Request):
        row = db.upload(request.path_params["uid"], with_data=True)
        if row is None:
            return Response(status_code=404)
        return Response(row["data"], media_type=row["media_type"],
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f'inline; filename="{quote(row["filename"])}"'})

    # -- urval och lärdomar ---------------------------------------------------------------------

    async def library_page(request: Request):
        return render(request, "valuesets.html", valuesets=db.valuesets())

    async def valueset_json(request: Request):
        vs = db.valueset(request.query_params.get("url") or "")
        if vs is None:
            return JSONResponse({"error": "Urvalet finns inte."}, status_code=404)
        from .fhir import valueset_resources
        valueset, codesystem = valueset_resources(vs, cfg)
        return JSONResponse({"resourceType": "Bundle", "type": "collection",
                             "entry": [{"resource": r} for r in (valueset, codesystem) if r]})

    async def guidelines_page(request: Request, errors=None, status=200):
        return render(request, "guidelines.html", status=status, guidelines=db.guidelines(),
                      feedback=db.feedback(20), errors=errors or [])

    async def guidelines_suggest(request: Request):
        try:
            result, meta = await ai.suggest_guidelines(client(), cfg, [dict(f) for f in db.feedback()],
                                                       service.guideline_texts(db))
        except Exception as exc:  # noqa: BLE001
            return await guidelines_page(request, [f"Inga förslag: {friendly(exc)}"], 502)
        for g in result.guidelines:
            db.add_guideline(g.text, "förslag", f"AI ({meta['model']}): {g.rationale}")
        return redirect("/lardomar", f"{len(result.guidelines)} förslag på lärdomar – godkänn de som ska gälla.")

    async def guidelines_add(request: Request):
        text = ((await request.form()).get("text") or "").strip()[:1000]
        if not text:
            return redirect("/lardomar", "Skriv lärdomen.")
        db.add_guideline(text, "aktiv", "manuell", user(request))
        return redirect("/lardomar", "Lärdomen gäller från nästa tolkning.")

    async def guidelines_decide(request: Request):
        decision = request.path_params["beslut"]
        if decision not in ("aktiv", "avvisad"):
            return Response(status_code=404)
        db.decide_guideline(int(request.path_params["gid"]), decision, user(request))
        return redirect("/lardomar", "Lärdomen gäller från nästa tolkning." if decision == "aktiv" else "Avvisad.")

    async def set_name(request: Request):
        name = ((await request.form()).get("name") or "").strip()[:80]
        response = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
        response.set_cookie("anvandare", name, httponly=True, samesite="lax", max_age=365 * 86400)
        return response

    async def health(request: Request):
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/namn", set_name, methods=["POST"]),
        Route("/ladda-upp", upload, methods=["POST"]),
        Route("/tolka", interpret_anyway, methods=["POST"]),
        Route("/sok", search_page),
        Route("/f/{fid}", form_page),
        Route("/f/{fid}/revidera", revise, methods=["POST"]),
        Route("/f/{fid}/godkann", approve, methods=["POST"]),
        Route("/f/{fid}/kopia", copy, methods=["POST"]),
        Route("/f/{fid}/urval", use_valueset, methods=["POST"]),
        Route("/f/{fid}/aterstall", restore, methods=["POST"]),
        Route("/f/{fid}/bundle.json", bundle),
        Route("/f/{fid}/questionnaire.json", questionnaire_json),
        Route("/fil/{uid}", file),
        Route("/urval", library_page),
        Route("/urval/resurs", valueset_json),
        Route("/lardomar", guidelines_page),
        Route("/lardomar/foresla", guidelines_suggest, methods=["POST"]),
        Route("/lardomar/ny", guidelines_add, methods=["POST"]),
        Route("/lardomar/{gid:int}/{beslut}", guidelines_decide, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    from starlette.middleware import Middleware
    return Starlette(routes=routes, middleware=[Middleware(SecurityMiddleware)])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="form-assistant", description="AI-stödd tolkning av formulär till FHIR")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    import uvicorn
    cfg = Config.from_env()
    print(f"Formulärassistenten: http://{args.host}:{args.port}  (databas {cfg.db_path}, bas {cfg.canonical_base})")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
