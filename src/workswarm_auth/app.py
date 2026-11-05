import re
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .manager import Manager
from .providers import AuthError, Registry
from .vault import Vault
from .security import BINDING_COOKIE_PATH, Gateway, OwnerLogin, Verifier, bind_browser


# The publisher-built client wheel (code + public profile only) that the market launcher installs.
CLIENT_WHEEL = re.compile(r"workswarm_auth-\d+(\.\d+)*-py3-none-any\.whl")


class ConnectRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    connection_id: str | None = Field(default=None, max_length=64)
    label: str = Field(default="", max_length=120)


class OperationRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


class CancelRequest(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=64)


def create_app(settings, client_factory=None, registry_factory=Registry):
    vault = Vault(settings.root)
    verifier = Verifier(settings.remote, None)
    owner_login = OwnerLogin(settings.remote, verifier, vault)

    @asynccontextmanager
    async def lifespan(app):
        async with (client_factory() if client_factory else httpx.AsyncClient(timeout=25, follow_redirects=False)) as client:
            registry = registry_factory(settings, client)
            app.state.manager = Manager(settings, vault, registry)
            verifier.client = client
            yield

    app = FastAPI(title="WorkSwarm Authorization Manager", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.vault = vault
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", urlsplit(settings.origin).hostname])

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'", "X-Content-Type-Options": "nosniff"})
        return response

    async def principal(authorization: str = Header(default="")):
        token = await verifier.verify_token(authorization.removeprefix("Bearer ")) if authorization.startswith("Bearer ") else None
        if token is None:
            raise AuthError("authentication_required", 401)
        return verifier.principal(token)

    def gateway(identity):
        return Gateway(app.state.manager, identity)

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return JSONResponse({"success": False, "error": exc.code}, status_code=exc.status,
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"success": False, "error": "invalid_request"}, status_code=422)

    @app.get("/health")
    def health():
        return {"ok": True, "service": "workswarm-auth", "version": __version__}

    @app.get("/api/auth/providers")
    async def providers(identity=Depends(principal)):
        return await gateway(identity).call("providers")

    @app.get("/api/auth/session")
    async def session(identity=Depends(principal)):
        # The desktop bridge verifies that its ID token and broker access token
        # resolve to the same owner, without exposing an issuer subject or token.
        return {"authenticated": True, "user_id": identity.owner}

    @app.get("/api/auth/connections")
    async def connections(identity=Depends(principal)):
        return await gateway(identity).call("status")

    @app.post("/api/auth/connections")
    async def connect(body: ConnectRequest, identity=Depends(principal)):
        return await gateway(identity).call("connect", cid=body.connection_id or "", provider=body.provider, label=body.label)

    @app.get("/api/auth/connections/{cid}")
    async def status(cid: str, identity=Depends(principal)):
        return await gateway(identity).call("status", cid=cid)

    @app.post("/api/auth/connections/{cid}/validate")
    async def validate(cid: str, identity=Depends(principal)):
        return await gateway(identity).call("validate", cid=cid)

    @app.post("/api/auth/connections/{cid}/operations")
    async def operation(cid: str, body: OperationRequest, identity=Depends(principal)):
        return await gateway(identity).call("call", cid=cid, operation=body.operation, arguments=body.arguments)

    @app.post("/api/auth/connections/{cid}/disconnect")
    async def disconnect(cid: str, identity=Depends(principal)):
        return await gateway(identity).call("disconnect", cid=cid)

    @app.post("/api/auth/connections/{cid}/cancel")
    async def cancel(cid: str, body: CancelRequest, identity=Depends(principal)):
        return await gateway(identity).call("cancel", cid=cid, attempt_id=body.attempt_id)

    @app.get("/api/auth/audit")
    async def audit(identity=Depends(principal)):
        return await gateway(identity).call("audit")

    @app.get("/client/{name}")
    def client_download(name: str):
        path = settings.root / "client" / name
        if not CLIENT_WHEEL.fullmatch(name) or not path.is_file():
            raise AuthError("not_found", 404)
        return FileResponse(path, media_type="application/octet-stream")

    @app.get("/connect/{ticket}")
    def redirect(ticket: str):
        flow, cookie, secret = bind_browser(app.state.manager.open_link(ticket))
        target = owner_login.start(flow)
        response = RedirectResponse(target, status_code=302)
        # Lax: the vendor/issuer callbacks are top-level GET navigations back to this origin.
        response.set_cookie(cookie, secret, max_age=600, path=BINDING_COOKIE_PATH, httponly=True,
                            secure=settings.origin.startswith("https://"), samesite="lax")
        return response

    @app.get(OwnerLogin.CALLBACK)
    async def owner_callback(request: Request, state: str = "", code: str = ""):
        flow = await owner_login.finish(state, code, request.cookies)
        if not app.state.manager.flow_active(flow):
            raise AuthError("connection_link_cancelled")
        return RedirectResponse(app.state.manager.provider_redirect(flow), status_code=302)

    @app.get("/oauth/providers/{provider}/callback")
    async def callback(request: Request, provider: str, state: str = "", code: str = "", error: str = ""):
        try:
            await app.state.manager.callback(provider, state, code, error, request.cookies)
            # Callback states carry the authenticated owner; no browser owner input is trusted.
            vault.audit(None, None, "oauth_callback", "success")
        except AuthError as exc:
            vault.audit(None, None, "oauth_callback", exc.code)
            raise
        return HTMLResponse("<!doctype html><meta charset='utf-8'><h1>授权成功 / Authorization saved</h1><p>返回 WorkSwarm 执行 auth_validate；实际 API 可用性尚未验证。</p>")

    return app

