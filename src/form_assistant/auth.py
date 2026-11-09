"""Inloggning för arbetsytan (det inloggade läget). Det öppna läget kräver ingen inloggning.

    FORM_AUTH                demo (standard) | oidc | proxy
    FORM_PRODUCTION          1 = drift: demoläget och osäkra inställningar stoppar starten
    FORM_SESSION_SECRET      signeringsnyckel för sessionscookien (minst 32 tecken; krävs för oidc)

    OIDC (OpenID Connect, t.ex. Entra ID, Keycloak, Ineras IdP):
    FORM_OIDC_ISSUER, FORM_OIDC_CLIENT_ID, FORM_OIDC_CLIENT_SECRET, FORM_OIDC_SCOPES (openid profile email),
    FORM_OIDC_USERNAME_CLAIM (preferred_username), FORM_OIDC_GROUPS_CLAIM (groups), FORM_PUBLIC_URL

    Proxy (oauth2-proxy, mod_auth_openidc, API-gateway framför verktyget):
    FORM_PROXY_USER_HEADER (X-Forwarded-User), FORM_PROXY_GROUPS_HEADER (X-Forwarded-Groups),
    FORM_TRUSTED_PROXIES (127.0.0.1,::1), FORM_PROXY_SECRET (delad hemlighet i X-Proxy-Secret)

    FORM_ALLOWED_GROUPS      grupper som får använda arbetsytan (kommaseparerade); tomt = alla inloggade

Demoläget har ingen riktig inloggning: man anger bara sitt namn. Det är till för demo och test.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

SAFE_ALGORITHMS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}


class AuthError(Exception):
    """Inloggningen misslyckades (visas för användaren)."""


class AuthConfigError(ValueError):
    """Inloggningen är felkonfigurerad – verktyget startar inte."""


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "demo"
    production: bool = False
    session_secret: str | None = None
    session_minutes: int = 480
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    public_url: str | None = None
    proxy_user_header: str = "x-forwarded-user"
    proxy_groups_header: str = "x-forwarded-groups"
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    proxy_secret: str | None = None
    allowed_groups: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "AuthConfig":
        env = os.environ.get
        return cls(
            mode=(env("FORM_AUTH") or "demo").strip().lower(),
            production=env("FORM_PRODUCTION") == "1",
            session_secret=env("FORM_SESSION_SECRET") or None,
            session_minutes=int(env("FORM_SESSION_MINUTES") or 480),
            issuer=(env("FORM_OIDC_ISSUER") or "").rstrip("/") or None,
            client_id=env("FORM_OIDC_CLIENT_ID") or None,
            client_secret=env("FORM_OIDC_CLIENT_SECRET") or None,
            scopes=_split(env("FORM_OIDC_SCOPES")) or ("openid", "profile", "email"),
            username_claim=env("FORM_OIDC_USERNAME_CLAIM") or "preferred_username",
            groups_claim=env("FORM_OIDC_GROUPS_CLAIM") or "groups",
            public_url=(env("FORM_PUBLIC_URL") or "").rstrip("/") or None,
            proxy_user_header=(env("FORM_PROXY_USER_HEADER") or "X-Forwarded-User").lower(),
            proxy_groups_header=(env("FORM_PROXY_GROUPS_HEADER") or "X-Forwarded-Groups").lower(),
            trusted_proxies=_split(env("FORM_TRUSTED_PROXIES")) or ("127.0.0.1", "::1"),
            proxy_secret=env("FORM_PROXY_SECRET") or None,
            allowed_groups=frozenset(_split(env("FORM_ALLOWED_GROUPS"))),
        )


@dataclass
class User:
    username: str
    name: str | None = None
    email: str | None = None
    groups: list[str] = field(default_factory=list)
    source: str = "demo"

    @property
    def display(self) -> str:
        return self.name or self.username


def check(cfg: AuthConfig) -> list[str]:
    """Stoppa farliga inställningar innan verktyget startar. Returnerar varningar."""
    errors, warnings = [], []
    if cfg.mode not in ("demo", "oidc", "proxy"):
        errors.append("FORM_AUTH ska vara demo, oidc eller proxy.")
    if cfg.mode == "demo":
        if cfg.production:
            errors.append("Demoläget (inloggning med bara namn) är spärrat när FORM_PRODUCTION=1.")
        warnings.append("Demoläge: ingen riktig inloggning – använd bara för demo och test.")
    if cfg.mode == "oidc":
        if not cfg.issuer or not cfg.client_id:
            errors.append("FORM_AUTH=oidc kräver FORM_OIDC_ISSUER och FORM_OIDC_CLIENT_ID.")
        elif not cfg.issuer.startswith("https://") and (cfg.production or not cfg.issuer.startswith(
                ("http://localhost", "http://127.0.0.1"))):
            errors.append("IdP:ns issuer ska använda https.")
        if len(cfg.session_secret or "") < 32:
            errors.append("FORM_SESSION_SECRET saknas eller är kortare än 32 tecken.")
        if cfg.production and not (cfg.public_url or "").startswith("https://"):
            errors.append("FORM_PUBLIC_URL ska vara en https-adress i drift.")
        if not cfg.client_secret:
            warnings.append("FORM_OIDC_CLIENT_SECRET saknas – fungerar bara med en publik klient (PKCE).")
    if cfg.mode == "proxy":
        if any(p in ("0.0.0.0/0", "::/0") for p in cfg.trusted_proxies):
            errors.append("FORM_TRUSTED_PROXIES får inte godta alla adresser – då kan vem som helst logga in.")
        if not cfg.proxy_secret:
            warnings.append("FORM_PROXY_SECRET saknas – användarhuvudet godtas från betrodda adresser utan hemlighet.")
    if errors:
        raise AuthConfigError("; ".join(errors))
    return warnings


def allowed(cfg: AuthConfig, user: User) -> bool:
    return not cfg.allowed_groups or bool(cfg.allowed_groups & set(user.groups))


# -- proxy ------------------------------------------------------------------------------------------


def trusted(host: str | None, proxies: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    for entry in proxies:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def proxy_user(headers: Any, client_host: str | None, cfg: AuthConfig) -> User | None:
    """Användaren enligt inloggningsproxyn – bara från en betrodd adress och med rätt hemlighet."""
    if not trusted(client_host, cfg.trusted_proxies):
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    if cfg.proxy_secret and not hmac.compare_digest(lowered.get("x-proxy-secret", ""), cfg.proxy_secret):
        return None
    username = (lowered.get(cfg.proxy_user_header) or "").strip()
    if not username:
        return None
    groups = [g.strip() for g in (lowered.get(cfg.proxy_groups_header) or "").split(",") if g.strip()]
    return User(username=username, groups=groups, source="proxy")


# -- OpenID Connect -------------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Oidc:
    """Authorization code-flödet med PKCE, state och nonce. ``transport`` gör det testbart utan nätverk."""

    def __init__(self, cfg: AuthConfig, transport=None):
        self.cfg = cfg
        self.transport = transport
        self._metadata: dict | None = None
        self._jwks: dict | None = None

    def _http(self):
        import httpx2
        return httpx2.AsyncClient(timeout=15, transport=self.transport, follow_redirects=False)

    async def metadata(self) -> dict:
        if self._metadata is None:
            async with self._http() as http:
                response = await http.get(f"{self.cfg.issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            meta = response.json()
            if meta.get("issuer", "").rstrip("/") != self.cfg.issuer:
                raise AuthError("IdP:ns issuer stämmer inte med konfigurationen.")
            self._metadata = meta
        return self._metadata

    async def login(self, redirect_uri: str) -> tuple[str, dict]:
        """(adress till IdP:n, det som sparas i sessionen till återkomsten)."""
        meta = await self.metadata()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        params = {"response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": redirect_uri,
                  "scope": " ".join(self.cfg.scopes), "state": state, "nonce": nonce,
                  "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
                  "code_challenge_method": "S256"}
        return (f"{meta['authorization_endpoint']}?{urlencode(params)}",
                {"state": state, "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
                 "started": int(time.time())})

    async def finish(self, code: str, state: str, pending: dict | None) -> tuple[User, str]:
        """Växla koden mot token och validera ID-tokenet. Returnerar användaren och ID-tokenet (för utloggning)."""
        if not pending or not hmac.compare_digest(state or "", pending.get("state", "")) \
                or time.time() - pending.get("started", 0) > 600:
            raise AuthError("Inloggningen kunde inte verifieras eller har gått ut – försök igen.")
        meta = await self.metadata()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
                "client_id": self.cfg.client_id, "code_verifier": pending["verifier"]}
        if self.cfg.client_secret:
            data["client_secret"] = self.cfg.client_secret
        async with self._http() as http:
            response = await http.post(meta["token_endpoint"], data=data, headers={"Accept": "application/json"})
        if response.status_code != 200 or not response.json().get("id_token"):
            raise AuthError(f"IdP:n avvisade inloggningen ({response.status_code}).")
        id_token = response.json()["id_token"]
        claims = await self.validate(id_token, pending["nonce"])
        username = _claim(claims, self.cfg.username_claim) or claims.get("email") or claims["sub"]
        raw = _claim(claims, self.cfg.groups_claim) or []
        groups = [raw] if isinstance(raw, str) else [str(g) for g in raw]
        return User(username=str(username), name=claims.get("name"), email=claims.get("email"), groups=groups,
                    source="oidc"), id_token

    async def validate(self, id_token: str, nonce: str) -> dict:
        import jwt
        meta = await self.metadata()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise AuthError("ID-tokenet går inte att läsa.") from exc
        algorithm = header.get("alg")
        supported = set(meta.get("id_token_signing_alg_values_supported") or ["RS256"])
        if algorithm not in SAFE_ALGORITHMS & supported:
            raise AuthError(f"ID-tokenet har en otillåten signaturalgoritm ({algorithm}).")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(id_token, key, algorithms=[algorithm], audience=self.cfg.client_id,
                                issuer=meta["issuer"], leeway=60,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            raise AuthError(f"ID-tokenet godkändes inte ({type(exc).__name__}).") from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AuthError("ID-tokenet hör inte till den här inloggningen.")
        return claims

    async def _key(self, kid: str | None):
        import jwt
        for refresh in (False, True):            # vid nyckelbyte hos IdP:n hämtas nycklarna igen
            if self._jwks is None or refresh:
                meta = await self.metadata()
                async with self._http() as http:
                    response = await http.get(meta["jwks_uri"])
                response.raise_for_status()
                self._jwks = response.json()
            for jwk in jwt.PyJWKSet.from_dict(self._jwks).keys:
                if jwk.key_id == kid:
                    return jwk.key
        raise AuthError("ID-tokenet är signerat med en okänd nyckel.")

    async def logout_url(self, id_token: str | None, return_to: str) -> str | None:
        meta = await self.metadata()
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return None
        params = {"post_logout_redirect_uri": return_to, "client_id": self.cfg.client_id}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{endpoint}?{urlencode(params)}"


def _claim(claims: dict, path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value
