"""De två lägena: öppet läge (ladda upp och hämta, utan inloggning) och inloggat läge (arbetsytan)."""

import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

pytest.importorskip("anthropic")
jwt = pytest.importorskip("jwt")

from starlette.testclient import TestClient  # noqa: E402

from form_assistant import auth, service  # noqa: E402
from form_assistant.config import Config  # noqa: E402
from form_assistant.db import Database  # noqa: E402
from form_assistant.model import FormDraft  # noqa: E402
from form_assistant.web import create_app  # noqa: E402

from conftest import FakeClaude, draft_dict, follow_job, form_image  # noqa: E402
from reportlab_free_pdf import simple_pdf  # noqa: E402


def make(tmp_path, **cfg_kw):
    cfg = Config(db_path=tmp_path / "db.sqlite", canonical_base="https://fhir.region-exempel.se/formular", **cfg_kw)
    db = Database(cfg.db_path)
    claude = FakeClaude(draft_dict())
    return cfg, db, claude, create_app(cfg, db, ai_client_factory=claude, auth_cfg=auth.AuthConfig())


def test_open_mode_upload_preview_download_and_claim(tmp_path):
    cfg, db, claude, app = make(tmp_path)
    with TestClient(app, follow_redirects=True) as client:
        page = client.get("/").text
        assert "Formulär till FHIR Questionnaire" in page and "Logga in" in page
        r = follow_job(client, client.post("/oppen/tolka", files={"files": ("s.png", form_image(0), "image/png")},
                                           data={"hint": "Hälsodeklaration"}))
        assert "/oppen/r/" in str(r.url) and "AI-tolkat, inte granskat" in r.text and "Personnummer" in r.text
        token = str(r.url).rsplit("/", 1)[1]
        q = client.get(f"/oppen/r/{token}/questionnaire.json").json()
        assert q["resourceType"] == "Questionnaire" and q["status"] == "draft"
        bundle = client.get(f"/oppen/r/{token}/bundle.json").json()
        prov = bundle["entry"][-1]["resource"]
        assert ("enterer", "anonym (öppet läge)") in [(a["type"]["coding"][0]["code"], a["who"]["display"])
                                                      for a in prov["agent"]]
        uid = re.search(r'/oppen/r/[^/]+/fil/([0-9a-f]+)', r.text).group(1)
        assert client.get(f"/oppen/r/{token}/fil/{uid}").status_code == 200
        assert client.get(f"/oppen/r/{'x' * 32}").status_code == 404

        # Öppna resultat är privata: inte i arbetsytan, inte via formulär- eller fil-länkar där.
        fid = db.open_form(token)["id"]
        client.cookies.set("anvandare", "anna")
        assert f"/f/{fid}" not in client.get("/").text
        assert client.get(f"/f/{fid}").status_code == 404 and client.get(f"/fil/{uid}").status_code == 404
        assert not claude.requests[0]["messages"][0]["content"][-1]["text"].count("Godkända formulär som exempel")

        r = client.post(f"/oppen/r/{token}/ta-in")
        assert "taget in i arbetsytan" in r.text and r.url.path == f"/f/{fid}"
        assert db.open_form(token) is None and db.workspace_form(fid)["created_by"] == "anna"
        assert db.workspace_form(fid)["fingerprint"]                          # indexerad för igenkänning


def test_open_mode_recognizes_only_approved_forms(tmp_path):
    cfg, db, claude, app = make(tmp_path)
    up = service.store_upload(db, "blankett.png", "image/png", form_image(0))
    draft = FormDraft.model_validate(draft_dict())
    fid = db.create_form(draft.title, "anna", [up["id"]])
    service.save_manual(db, fid, draft, "anna", "manuell", None)
    with TestClient(app, follow_redirects=True) as client:
        r = client.post("/oppen/tolka", files={"files": ("igen.png", form_image(0), "image/png")})
        assert "Formuläret finns redan" not in r.text                         # utkast räknas inte
        assert client.get(f"/oppen/godkand/{fid}/questionnaire.json").status_code == 404
        db.approve(fid, "bertil")
        r = client.post("/oppen/tolka", files={"files": ("igen.png", form_image(0), "image/png")})
        assert "Formuläret finns redan" in r.text and f"/oppen/godkand/{fid}/questionnaire.json" in r.text
        assert client.get(f"/oppen/godkand/{fid}/questionnaire.json").json()["status"] == "active"
        calls = len(claude.requests)
        ticket = re.search(r'name="ticket" value="([^"]+)"', r.text).group(1)
        r = follow_job(client, client.post("/oppen/tolka-anda", data={"ticket": ticket}))
        assert "/oppen/r/" in str(r.url) and len(claude.requests) == calls + 1
        forged = client.post("/oppen/tolka-anda", data={"ticket": ticket[:-3] + "abc"})
        assert forged.status_code == 400                                      # ingen tolkning av andras filer


def test_open_mode_limits_expiry_and_switch_off(tmp_path):
    cfg, db, claude, app = make(tmp_path, open_per_hour=1, open_max_files=2, open_max_pages=2,
                                open_retention_hours=0.0003)
    with TestClient(app, follow_redirects=True) as client:
        three = [("files", (f"{n}.png", form_image(n % 2), "image/png")) for n in range(3)]
        assert "Högst 2 filer" in client.post("/oppen/tolka", files=three).text
        pdf = simple_pdf(["sida"])
        many = pdf.replace(b"/Count 1", b"/Count 3").replace(b"/Kids [3 0 R]", b"/Kids [3 0 R 3 0 R 3 0 R]")
        assert "högst 2 sidor" in client.post("/oppen/tolka", files={"files": ("lang.pdf", many, "application/pdf")}).text
        r = follow_job(client, client.post("/oppen/tolka", files={"files": ("a.png", form_image(1), "image/png")}))
        token = str(r.url).rsplit("/", 1)[1]
        assert "vänta en stund" in client.post("/oppen/tolka", files={"files": ("b.png", form_image(0),
                                                                              "image/png")}).text
        time.sleep(1.2)                                                       # resultatet går ut
        assert client.get(f"/oppen/r/{token}").status_code == 404
        assert db.one("SELECT COUNT(*) AS n FROM forms")["n"] == 0
        assert db.one("SELECT COUNT(*) AS n FROM uploads")["n"] == 0          # filerna raderas också
    cfg, db, claude, app = make(tmp_path / "av", open_mode=False)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/").headers["location"] == "/logga-in"
        assert client.get("/oppen").headers["location"] == "/logga-in"


def test_workspace_requires_login_and_demo_login(tmp_path):
    cfg, db, claude, app = make(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/urval")
        assert r.status_code == 303 and r.headers["location"] == "/logga-in?next=%2Furval"
        assert client.post("/ladda-upp", files={"files": ("s.png", form_image(0), "image/png")}).status_code == 401
        assert "demoläge" in client.get("/logga-in").text.lower()
        r = client.post("/logga-in", data={"name": "anna", "next": "/urval"})
        assert r.headers["location"] == "/urval" and client.get("/urval").status_code == 200
        assert client.post("/logga-in", data={"name": "anna", "next": "//evil.example"}).headers["location"] == "/"
        client.post("/logga-ut")
        assert client.get("/urval").status_code == 303


def test_auth_config_checks():
    assert auth.check(auth.AuthConfig())                                     # demo: varning
    with pytest.raises(auth.AuthConfigError, match="spärrat"):
        auth.check(auth.AuthConfig(production=True))
    with pytest.raises(auth.AuthConfigError, match="FORM_SESSION_SECRET"):
        auth.check(auth.AuthConfig(mode="oidc", issuer="https://idp.example.se", client_id="c"))
    with pytest.raises(auth.AuthConfigError, match="https"):
        auth.check(auth.AuthConfig(mode="oidc", issuer="http://idp.example.se", client_id="c", session_secret="s" * 32))
    with pytest.raises(auth.AuthConfigError, match="alla adresser"):
        auth.check(auth.AuthConfig(mode="proxy", trusted_proxies=("0.0.0.0/0",)))
    with pytest.raises(auth.AuthConfigError, match="demo, oidc eller proxy"):
        auth.check(auth.AuthConfig(mode="ldap"))


def test_proxy_mode(tmp_path, monkeypatch):
    cfg = Config(db_path=tmp_path / "db.sqlite")
    acfg = auth.AuthConfig(mode="proxy", allowed_groups=frozenset({"formular"}))
    app = create_app(cfg, Database(cfg.db_path), ai_client_factory=FakeClaude(draft_dict()), auth_cfg=acfg)
    with TestClient(app, follow_redirects=False) as client:
        # TestClient ansluter från "testclient", som inte är en betrodd adress: huvudet ignoreras
        assert client.get("/urval", headers={"X-Forwarded-User": "mallory"}).status_code == 303
        monkeypatch.setattr(auth, "trusted", lambda host, proxies: True)
        ok = {"X-Forwarded-User": "anna", "X-Forwarded-Groups": "formular,annat"}
        assert client.get("/urval", headers=ok).status_code == 200
        assert client.get("/urval", headers={"X-Forwarded-User": "bo", "X-Forwarded-Groups": "annat"}).status_code == 303
    assert auth.proxy_user({"x-forwarded-user": "a"}, "10.0.0.1", auth.AuthConfig(trusted_proxies=("10.0.0.0/8",)))
    assert auth.proxy_user({"x-forwarded-user": "a"}, "10.0.0.1", auth.AuthConfig(
        trusted_proxies=("10.0.0.0/8",), proxy_secret="hemligt")) is None


ISSUER = "https://idp.example.se/realms/vard"


class FakeIdp:
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce = None
        self.alg = "RS256"

    def handler(self, request):
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                                              "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/certs",
                                              "end_session_endpoint": f"{ISSUER}/logout",
                                              "id_token_signing_alg_values_supported": ["RS256", "none"]})
        if path.endswith("/certs"):
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx2.Response(200, json={"keys": [jwk | {"kid": "k1", "use": "sig", "alg": "RS256"}]})
        if path.endswith("/token"):
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": "formular", "sub": "u1", "iat": now, "exp": now + 300, "nonce": self.nonce,
                      "preferred_username": "anna", "name": "Anna Andersson", "groups": ["formular"]}
            token = jwt.encode(claims, None if self.alg == "none" else self.key, algorithm=self.alg,
                               headers={"kid": "k1"})
            return httpx2.Response(200, json={"access_token": "a", "token_type": "Bearer", "id_token": token})
        return httpx2.Response(404)


@pytest.mark.parametrize("alg", ["RS256", "none"])
def test_oidc_mode(tmp_path, alg):
    cfg = Config(db_path=tmp_path / "db.sqlite")
    acfg = auth.AuthConfig(mode="oidc", issuer=ISSUER, client_id="formular", client_secret="hemligt",
                           session_secret="s" * 40, allowed_groups=frozenset({"formular"}))
    idp = FakeIdp()
    idp.alg = alg
    app = create_app(cfg, Database(cfg.db_path), ai_client_factory=FakeClaude(draft_dict()), auth_cfg=acfg,
                     oidc_transport=httpx2.MockTransport(idp.handler))
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/logga-in", params={"next": "/urval"})
        location = urlsplit(r.headers["location"])
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        assert location.path.endswith("/auth") and query["code_challenge_method"] == "S256"
        idp.nonce = query["nonce"]
        r = client.get("/auth/callback", params={"code": "c", "state": query["state"]})
        if alg == "none":
            assert r.status_code == 401 and "otillåten" in r.text                # osignerade token avvisas
            assert client.get("/urval").status_code == 303
            return
        assert r.headers["location"] == "/urval" and client.get("/urval").status_code == 200
        assert "Anna Andersson" in client.get("/urval").text
        assert client.get("/auth/callback", params={"code": "c", "state": "fel"}).status_code == 401
        r = client.post("/logga-ut")
        assert r.headers["location"].startswith(f"{ISSUER}/logout") and client.get("/urval").status_code == 303
