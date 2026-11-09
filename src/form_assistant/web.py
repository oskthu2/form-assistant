"""Webbklienten (Starlette + Jinja2, serverrenderad).

    form-assistant                         # http://127.0.0.1:8000
    form-assistant --host 0.0.0.0          # i Codespaces – låt porten vara privat

Ladda upp bilder eller en PDF av ett formulär. Tjänsten känner först efter om formuläret redan finns
(och erbjuder en kopia), annars tolkar Claude det. Formuläret förhandsvisas som det blir i FHIR, kan
itereras med instruktioner, får förslag på befintliga urval, godkänns och laddas ner som en FHIR-Bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import ai, auth, ecosystems, service, shazam, valuesets
from .config import Config
from . import fhir
from .db import Database, hours_ahead
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


def create_app(cfg: Config | None = None, db: Database | None = None, ai_client_factory=None,
               auth_cfg: auth.AuthConfig | None = None, oidc_transport=None) -> Starlette:
    """``ai_client_factory`` ger en AsyncAnthropic-klient och ``oidc_transport`` når IdP:n (byts i testerna)."""
    import hashlib
    import hmac
    import secrets as _secrets

    from itsdangerous import BadSignature, URLSafeTimedSerializer
    cfg = cfg or Config.from_env()
    auth_cfg = auth_cfg or auth.AuthConfig.from_env()
    auth_warnings = auth.check(auth_cfg)
    oidc = auth.Oidc(auth_cfg, oidc_transport) if auth_cfg.mode == "oidc" else None
    app_secret = auth_cfg.session_secret or _secrets.token_hex(32)
    signer = URLSafeTimedSerializer(app_secret, salt="oppna-uppladdningar")
    db = db or Database(cfg.db_path)
    ecosystems.ensure_default(db, cfg)
    db.interrupt_running_jobs()
    db.purge_expired()
    running: set[asyncio.Task] = set()   # håll referenser så att bakgrundsjobben inte städas bort
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(quote=quote, KIND_LABELS=KIND_LABELS, SEMANTIC_LABELS=SEMANTIC_LABELS, cfg=cfg,
                       ecosystems=lambda: ecosystems.all_ecosystems(db), auth_mode=auth_cfg.mode)

    def client():
        if ai_client_factory:
            return ai_client_factory()
        from anthropic import AsyncAnthropic
        return AsyncAnthropic()

    # -- inloggning -------------------------------------------------------------------------------

    def current(request: Request) -> auth.User | None:
        """Den inloggade användaren, eller None (öppet läge)."""
        if hasattr(request.state, "user"):
            return request.state.user
        found = None
        if auth_cfg.mode == "demo":
            name = (request.cookies.get("anvandare") or "").strip()[:80]
            found = auth.User(username=name, source="demo") if name else None
        elif auth_cfg.mode == "proxy":
            found = auth.proxy_user(request.headers, request.client.host if request.client else None, auth_cfg)
        else:
            data = request.session.get("user")
            if data and time.time() - request.session.get("login_at", 0) < auth_cfg.session_minutes * 60:
                found = auth.User(**data)
        if found is not None and not auth.allowed(auth_cfg, found):
            found = None
        request.state.user = found
        return found

    def user(request: Request) -> str:
        me = current(request)
        return me.username if me else "okänd"

    def login_required(endpoint):
        """Arbetsytans sidor: kräver inloggning, och ett formulär måste höra till arbetsytan."""
        async def guarded(request: Request):
            if current(request) is None:
                if request.method == "GET":
                    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse(f"/logga-in?{urlencode({'next': target})}", status_code=303)
                return render(request, "error.html", status=401, message="Logga in för att använda arbetsytan.")
            fid = request.path_params.get("fid")
            if fid and db.workspace_form(fid) is None:
                return render(request, "error.html", status=404, message="Formuläret finns inte.")
            return await endpoint(request)
        return guarded

    def client_key(request: Request) -> str:
        """Klientadressen, hashad (för gränserna i öppet läge – adressen sparas inte i klartext)."""
        host = request.client.host if request.client else "okänd"
        return "klient:" + hmac.new(app_secret.encode(), host.encode(), hashlib.sha256).hexdigest()[:16]

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("errors", [])
        page = env.get_template(name).render(request=request, user=user(request), me=current(request),
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
        eco = ecosystems.get(db, form.get("ecosystem")).code
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
                          searched=True, ecosystem=eco)
        if hits:
            return render(request, "match.html", hits=hits, search_hits=search_hits, uploads=stored, hint=hint,
                          searched=False, ecosystem=eco)
        return await interpret(request, [u["id"] for u in stored], hint, eco)

    # -- bakgrundsjobb: tolkning och revidering tar ofta minuter, längre än en proxy väntar --------

    def progress_writer(jid: str):
        last = {"items": -1, "at": 0.0}

        def write(text: str) -> None:
            items = ai.count_items(text)
            if items != last["items"] or time.monotonic() - last["at"] > 5:
                last.update(items=items, at=time.monotonic())
                db.update_job(jid, progress=f"Claude har beskrivit {items} frågor och rubriker"
                              if items else f"Claude läser underlaget ({len(text)} tecken skrivna)")
        return write

    def start_job(kind: str, request: Request, work, form_id: str | None = None, owner: str | None = None,
                  page: str = "/jobb") -> RedirectResponse:
        jid = db.create_job(kind, owner or user(request), form_id)
        db.update_job(jid, progress="Claude läser underlaget …")

        async def run():
            try:
                result = await work(progress_writer(jid))
                db.update_job(jid, status="klar", result=result)
            except Exception as exc:  # noqa: BLE001
                db.update_job(jid, status="fel", error=friendly(exc))
        task = asyncio.create_task(run())
        running.add(task)
        task.add_done_callback(running.discard)
        return RedirectResponse(f"{page}/{jid}", status_code=303)

    async def job_page(request: Request):
        job = db.job(request.path_params["jid"])
        if job is not None and job["kind"] == "öppen-tolkning":
            job = None
        if job is None:
            return render(request, "error.html", status=404, message="Jobbet finns inte.")
        if job["status"] == "klar" and job["result"]:
            return RedirectResponse(job["result"], status_code=303)
        started = job["created"]
        return render(request, "job.html", job=job, started=started)

    async def job_json(request: Request):
        job = db.job(request.path_params["jid"])
        if job is not None and job["kind"] == "öppen-tolkning":
            job = None
        if job is None:
            return JSONResponse({"error": "Jobbet finns inte."}, status_code=404)
        return JSONResponse({k: job[k] for k in ("id", "kind", "status", "progress", "error", "result", "form_id")})

    async def interpret(request: Request, upload_ids: list[str], hint: str | None, ecosystem: str | None = None):
        author = user(request)

        async def work(progress):
            form_id = await service.create_form(db, cfg, client(), upload_ids, author, hint or None, ecosystem,
                                                progress)
            return f"/f/{form_id}?" + urlencode({"msg": "Formuläret är tolkat. Granska förhandsvisningen och "
                                                        "iterera vid behov."})
        return start_job("tolkning", request, work)

    async def interpret_anyway(request: Request):
        form = await request.form()
        return await interpret(request, form.getlist("upload_ids"), (form.get("hint") or "").strip(),
                               form.get("ecosystem"))

    async def search_page(request: Request):
        q = (request.query_params.get("q") or "").strip()
        return render(request, "match.html", hits=[], search_hits=shazam.search(db, q, limit=10) if q else [],
                      uploads=[], hint=q, searched=True, ecosystem=None)

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
                      ecosystem=service.form_config(db, cfg, fid)[1],
                      uncertain=[i for i in draft.items if i.uncertainty])

    async def revise(request: Request):
        fid = request.path_params["fid"]
        instruction = ((await request.form()).get("instruction") or "").strip()[:4000]
        if not instruction:
            return redirect(f"/f/{fid}", "Skriv vad som ska ändras.")
        if db.form(fid) is None:
            return render(request, "error.html", status=404, message="Formuläret finns inte.")
        author = user(request)

        async def work(progress):
            number = await service.revise_form(db, cfg, client(), fid, instruction, author, progress)
            return f"/f/{fid}?" + urlencode({"msg": f"Version {number} är klar."})
        return start_job("revidering", request, work, fid)

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
            new_id = service.copy_form(db, fid, user(request), form.getlist("upload_ids") or None,
                                       form.get("ecosystem") or None)
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

    def file_response(row) -> Response:
        return Response(row["data"], media_type=row["media_type"],
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f'inline; filename="{quote(row["filename"])}"'})

    async def file(request: Request):
        uid = request.path_params["uid"]
        in_workspace = db.one("SELECT 1 FROM form_uploads fu JOIN forms f ON f.id = fu.form_id "
                              "WHERE fu.upload_id = ? AND f.visibility = 'arbetsyta'", (uid,))
        row = db.upload(uid, with_data=True) if in_workspace else None
        if row is None:
            return Response(status_code=404)
        return Response(row["data"], media_type=row["media_type"],
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f'inline; filename="{quote(row["filename"])}"'})

    # -- urval och lärdomar ---------------------------------------------------------------------

    async def set_form_ecosystem(request: Request):
        fid = request.path_params["fid"]
        try:
            eco = service.set_ecosystem(db, fid, (await request.form()).get("ecosystem") or "")
        except service.ServiceError as exc:
            return redirect(f"/f/{fid}", str(exc))
        return redirect(f"/f/{fid}", f"Formuläret hör nu till {eco.display} ({eco.canonical_base}).")

    async def ecosystems_page(request: Request, errors=None, status=200, edit=None):
        code = request.query_params.get("redigera")
        current = edit or (next((e for e in ecosystems.all_ecosystems(db) if e.code == code), None) if code else None)
        counts = {}
        for f in db.forms():
            key = ecosystems.get(db, f["ecosystem"]).code
            counts[key] = counts.get(key, 0) + 1
        return render(request, "ecosystems.html", status=status, items=ecosystems.all_ecosystems(db), edit=current,
                      counts=counts, errors=errors or [])

    async def ecosystems_save(request: Request):
        form = await request.form()
        eco = ecosystems.Ecosystem(
            code=form.get("code") or "", display=form.get("display") or "",
            canonical_base=form.get("canonical_base") or "", publisher=form.get("publisher") or None,
            answer_options=form.get("answer_options") or "inline",
            allowed_extensions=[x.strip() for x in (form.get("allowed_extensions") or "").replace(",", "\n").splitlines()
                                if x.strip()],
            default=bool(form.get("default")))
        try:
            ecosystems.save(db, eco)
        except ecosystems.EcosystemError as exc:
            return await ecosystems_page(request, [str(exc)], 400, edit=eco)
        return redirect("/ekosystem", f"{eco.display} är sparat.")

    async def ecosystems_json(request: Request):
        codesystem, valueset = ecosystems.fhir_resources(db)
        return JSONResponse({"resourceType": "Bundle", "type": "collection",
                             "entry": [{"resource": codesystem}, {"resource": valueset}]})

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

    # -- inloggning och utloggning ----------------------------------------------------------------

    def safe_next(value: str | None) -> str:
        return value if value and value.startswith("/") and not value.startswith("//") else "/"

    def public_base(request: Request) -> str:
        return auth_cfg.public_url or str(request.base_url).rstrip("/")

    async def login_page(request: Request, errors=None, status=200):
        next_url = safe_next(request.query_params.get("next"))
        if current(request) is not None:
            return RedirectResponse(next_url, status_code=303)
        if auth_cfg.mode == "oidc":
            try:
                url, pending = await oidc.login(public_base(request) + "/auth/callback")
            except Exception as exc:  # noqa: BLE001 – IdP:n nås inte
                return render(request, "error.html", status=502, message=f"Inloggningstjänsten kunde inte nås ({exc}).")
            request.session["pending"] = pending | {"next": next_url}
            return RedirectResponse(url, status_code=303)
        return render(request, "login.html", status=status, next_url=next_url, errors=errors or [],
                      warnings=auth_warnings)

    async def login_submit(request: Request):
        if auth_cfg.mode != "demo":
            return RedirectResponse("/logga-in", status_code=303)
        form = await request.form()
        name = (form.get("name") or "").strip()[:80]
        if not name:
            return await login_page(request, ["Ange ditt namn."], 400)
        response = RedirectResponse(safe_next(form.get("next")), status_code=303)
        response.set_cookie("anvandare", name, httponly=True, samesite="lax", max_age=30 * 86400)
        return response

    async def callback(request: Request):
        if oidc is None:
            return RedirectResponse("/", status_code=303)
        pending = request.session.pop("pending", None)
        if request.query_params.get("error"):
            return render(request, "error.html", status=401,
                          message=f"Inloggningen avbröts ({request.query_params.get('error')}).")
        try:
            found, id_token = await oidc.finish(request.query_params.get("code", ""),
                                                request.query_params.get("state", ""), pending)
        except auth.AuthError as exc:
            return render(request, "error.html", status=401, message=str(exc))
        if not auth.allowed(auth_cfg, found):
            return render(request, "error.html", status=403, message="Du saknar behörighet till arbetsytan "
                          "(ingen av de tillåtna grupperna).")
        request.session.clear()
        request.session.update(user={"username": found.username, "name": found.name, "email": found.email,
                                     "groups": found.groups, "source": "oidc"},
                               login_at=int(time.time()), id_token=id_token)
        return RedirectResponse(safe_next((pending or {}).get("next")), status_code=303)

    async def logout(request: Request):
        if auth_cfg.mode == "demo":
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie("anvandare")
            return response
        if auth_cfg.mode == "oidc":
            id_token = request.session.get("id_token")
            request.session.clear()
            try:
                url = await oidc.logout_url(id_token, public_base(request) + "/")
            except Exception:  # noqa: BLE001 – lokal utloggning räcker
                url = None
            return RedirectResponse(url or "/", status_code=303)
        return render(request, "error.html", message="Logga ut i inloggningstjänsten (proxyn).")

    # -- öppet läge: ladda upp och hämta Questionnaire, utan inloggning ---------------------------

    def open_enabled(endpoint):
        async def wrapped(request: Request):
            if not cfg.open_mode:
                return RedirectResponse("/logga-in", status_code=303)
            db.purge_expired()
            return await endpoint(request)
        return wrapped

    async def start(request: Request):
        if current(request) is not None:
            return await index(request)
        if cfg.open_mode:
            return await open_index(request)
        return RedirectResponse("/logga-in", status_code=303)

    async def open_index(request: Request, errors=None, status=200):
        return render(request, "open_index.html", status=status, errors=errors or [])

    async def open_upload(request: Request):
        form = await request.form()
        files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        hint = (form.get("hint") or "").strip()[:1000]
        if not files:
            return await open_index(request, ["Ladda upp en bild eller PDF av formuläret."], 400)
        if len(files) > cfg.open_max_files:
            return await open_index(request, [f"Högst {cfg.open_max_files} filer åt gången."], 400)
        stored = []
        try:
            for f in files:
                up = service.store_upload(db, f.filename, f.content_type, await f.read())
                db.mark_open_upload(up["id"], hours_ahead(cfg.open_retention_hours))
                stored.append(up)
            service.check_open_limits(db, cfg, client_key(request), stored)
        except service.ServiceError as exc:
            return await open_index(request, [str(exc)], 400)
        ticket = signer.dumps({"uploads": [u["id"] for u in stored], "hint": hint})
        hits = shazam.recognize(db, stored, approved_only=True)
        if hits:
            return render(request, "open_match.html", hits=hits, ticket=ticket)
        return open_interpret(request, ticket)

    def open_interpret(request: Request, ticket: str):
        try:
            data = signer.loads(ticket, max_age=3600)
        except BadSignature:
            return render(request, "error.html", status=400, message="Uppladdningen har gått ut – ladda upp igen.")
        token = _secrets.token_urlsafe(24)

        async def work(progress):
            await service.create_open_form(db, cfg, client(), data["uploads"], data.get("hint") or None, token,
                                           progress)
            return f"/oppen/r/{token}"
        return start_job("öppen-tolkning", request, work, owner=client_key(request), page="/oppen/jobb")

    async def open_interpret_anyway(request: Request):
        ticket = (await request.form()).get("ticket") or ""
        try:
            data = signer.loads(ticket, max_age=3600)
            service.check_open_limits(db, cfg, client_key(request),
                                      [dict(db.upload(uid)) for uid in data["uploads"] if db.upload(uid)])
        except BadSignature:
            return render(request, "error.html", status=400, message="Uppladdningen har gått ut – ladda upp igen.")
        except service.ServiceError as exc:
            return await open_index(request, [str(exc)], 400)
        return open_interpret(request, ticket)

    async def open_job_page(request: Request):
        job = db.job(request.path_params["jid"])
        if job is None or job["kind"] != "öppen-tolkning":
            return render(request, "error.html", status=404, message="Jobbet finns inte.")
        if job["status"] == "klar" and job["result"]:
            return RedirectResponse(job["result"], status_code=303)
        return render(request, "job.html", job=job, started=job["created"])

    def open_form_or_404(request: Request):
        return db.open_form(request.path_params["token"])

    async def open_result(request: Request):
        form = open_form_or_404(request)
        if form is None:
            return render(request, "error.html", status=404, message="Resultatet finns inte längre – öppna resultat "
                          f"raderas efter {cfg.open_retention_hours:g} timmar.")
        exp = service.export(db, cfg, form["id"])
        draft = db.draft(form["id"])
        return render(request, "open_result.html", form=form, draft=draft, q=exp.build.questionnaire,
                      build=exp.build, uploads=db.form_uploads(form["id"]), token=request.path_params["token"],
                      uncertain=[i for i in draft.items if i.uncertainty], library={}, suggestions={},
                      is_latest=False)

    async def open_download(request: Request):
        form = open_form_or_404(request)
        if form is None:
            return JSONResponse({"error": "Resultatet finns inte längre."}, status_code=404)
        return download(service.export(db, cfg, form["id"]), request.path_params["kind"],
                        request.query_params.get("typ"))

    async def open_file(request: Request):
        form = open_form_or_404(request)
        uid = request.path_params["uid"]
        if form is None or not db.one("SELECT 1 FROM form_uploads WHERE form_id = ? AND upload_id = ?",
                                      (form["id"], uid)):
            return Response(status_code=404)
        return file_response(db.upload(uid, with_data=True))

    async def approved_download(request: Request):
        """Ett godkänt formulär som det öppna läget kände igen – kan hämtas direkt, utan AI-tolkning."""
        form = db.workspace_form(request.path_params["fid"])
        if form is None or form["status"] != "godkänd":
            return JSONResponse({"error": "Formuläret finns inte."}, status_code=404)
        return download(service.export(db, cfg, form["id"]), request.path_params["kind"],
                        request.query_params.get("typ"))

    def download(exp, kind: str, typ: str | None) -> Response:
        if kind == "questionnaire":
            content, name = exp.build.questionnaire, f"{exp.build.questionnaire['id']}.json"
        elif kind == "bundle":
            content = exp.bundle if typ != "collection" else fhir.bundle(
                exp.build.questionnaire, exp.build.valuesets, exp.provenance, exp.config, transaction=False)
            name = f"{exp.build.questionnaire['id']}-bundle.json"
        else:
            return Response(status_code=404)
        return Response(json.dumps(content, ensure_ascii=False, indent=2), media_type="application/fhir+json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def claim(request: Request):
        form = open_form_or_404(request)
        if form is None:
            return render(request, "error.html", status=404, message="Resultatet finns inte längre.")
        db.claim(form["id"], user(request))
        service.reindex(db, form["id"])
        return redirect(f"/f/{form['id']}", "Formuläret är taget in i arbetsytan – nu kan du iterera, välja urval "
                                            "och godkänna det.")

    async def health(request: Request):
        return JSONResponse({"ok": True})

    routes = [
        Route("/", start),
        Route("/health", health),
        Route("/logga-in", login_page, methods=["GET"]),
        Route("/logga-in", login_submit, methods=["POST"]),
        Route("/auth/callback", callback),
        Route("/logga-ut", logout, methods=["GET", "POST"]),
        # öppet läge
        Route("/oppen", open_enabled(open_index)),
        Route("/oppen/tolka", open_enabled(open_upload), methods=["POST"]),
        Route("/oppen/tolka-anda", open_enabled(open_interpret_anyway), methods=["POST"]),
        Route("/oppen/jobb/{jid}", open_enabled(open_job_page)),
        Route("/oppen/r/{token}", open_enabled(open_result)),
        Route("/oppen/r/{token}/fil/{uid}", open_enabled(open_file)),
        Route("/oppen/r/{token}/ta-in", login_required(claim), methods=["POST"]),
        Route("/oppen/r/{token}/{kind}.json", open_enabled(open_download)),
        Route("/oppen/godkand/{fid}/{kind}.json", open_enabled(approved_download)),
        # inloggat läge (arbetsytan)
        Route("/ladda-upp", login_required(upload), methods=["POST"]),
        Route("/tolka", login_required(interpret_anyway), methods=["POST"]),
        Route("/sok", login_required(search_page)),
        Route("/jobb/{jid}.json", login_required(job_json)),       # före /jobb/{jid}, som annars fångar .json
        Route("/jobb/{jid}", login_required(job_page)),
        Route("/f/{fid}", login_required(form_page)),
        Route("/f/{fid}/revidera", login_required(revise), methods=["POST"]),
        Route("/f/{fid}/godkann", login_required(approve), methods=["POST"]),
        Route("/f/{fid}/kopia", login_required(copy), methods=["POST"]),
        Route("/f/{fid}/urval", login_required(use_valueset), methods=["POST"]),
        Route("/f/{fid}/aterstall", login_required(restore), methods=["POST"]),
        Route("/f/{fid}/bundle.json", login_required(bundle)),
        Route("/f/{fid}/questionnaire.json", login_required(questionnaire_json)),
        Route("/fil/{uid}", login_required(file)),
        Route("/f/{fid}/ekosystem", login_required(set_form_ecosystem), methods=["POST"]),
        Route("/ekosystem", login_required(ecosystems_page), methods=["GET"]),
        Route("/ekosystem", login_required(ecosystems_save), methods=["POST"]),
        Route("/ekosystem/resurs", login_required(ecosystems_json)),
        Route("/urval", login_required(library_page)),
        Route("/urval/resurs", login_required(valueset_json)),
        Route("/lardomar", login_required(guidelines_page)),
        Route("/lardomar/foresla", login_required(guidelines_suggest), methods=["POST"]),
        Route("/lardomar/ny", login_required(guidelines_add), methods=["POST"]),
        Route("/lardomar/{gid:int}/{beslut}", login_required(guidelines_decide), methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    middleware = [Middleware(SecurityMiddleware)]
    if auth_cfg.mode == "oidc":
        middleware.append(Middleware(SessionMiddleware, secret_key=app_secret, session_cookie="formular_session",
                                     max_age=auth_cfg.session_minutes * 60, same_site="lax",
                                     https_only=(auth_cfg.public_url or "").startswith("https://")))
    return Starlette(routes=routes, middleware=middleware)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="form-assistant", description="AI-stödd tolkning av formulär till FHIR")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    missing = [name for name, module in (("itsdangerous", "itsdangerous"), ("pyjwt", "jwt"), ("anthropic", "anthropic"),
                                         ("pypdfium2", "pypdfium2"), ("uvicorn", "uvicorn"))
               if importlib.util.find_spec(module) is None]
    if missing:
        raise SystemExit(f"Paket saknas: {', '.join(missing)}. Installera om efter en uppdatering: "
                         "pip install -e '.[dev]' (i repots katalog).")
    import uvicorn
    cfg = Config.from_env()
    print(f"Formulärassistenten: http://{args.host}:{args.port}  (databas {cfg.db_path}, bas {cfg.canonical_base})")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
