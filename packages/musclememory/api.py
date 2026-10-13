from contextlib import asynccontextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Literal

import anyio
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from .config import Settings
from .core import (
    CreateRunInput,
    DomainError,
    EvaluateInput,
    InspectInput,
    LearnInput,
    ObserveInput,
    ProcedureDefinition,
    RecallInput,
    SessionUsageInput,
    Token,
)
from .mcp_server import build_mcp
from .service import MemoryService, conflict_error

SkillAgent = Literal["codex", "claude"]


API_REFERENCE = "https://thewariend.com/musclememory/api"


def dashboard_dir():
    """The built dashboard: the source checkout's build, else the copy in the wheel."""
    built = Path(__file__).resolve().parents[2] / "apps/dashboard/dist"
    return built if built.is_dir() else Path(__file__).with_name("static")


class SecurityBoundary:
    """Pure ASGI middleware preserves MCP streaming and enforces shared authentication."""

    def __init__(self, app, service):
        self.app, self.service = app, service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("ascii", errors="replace").lower()
        if not any(
            fnmatchcase(host, allowed.lower()) for allowed in self.service.settings.allowed_hosts
        ):
            return await Response(status_code=400)(scope, receive, send)
        original_send = send

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                        b"base-uri 'self'; frame-ancestors 'none'",
                    ),
                ]
                if path.startswith(("/v1", "/mcp")):
                    message["headers"].append((b"cache-control", b"no-store"))
            await original_send(message)

        send = secure_send
        if path.startswith(("/v1", "/mcp")):
            try:
                auth = headers.get(b"authorization", b"").decode("ascii")
                key = auth[7:] if auth.lower().startswith("bearer ") else ""
                principal = await anyio.to_thread.run_sync(self.service.authenticate, key)
                scope.setdefault("state", {})["mm_principal"] = principal
            except (DomainError, UnicodeError) as error:
                if not isinstance(error, DomainError):
                    error = DomainError("unauthorized", "Invalid bearer credential", 401)
                response = JSONResponse(
                    {"error": {"code": error.code, "message": error.message}},
                    status_code=error.status,
                    headers={"WWW-Authenticate": "Bearer"}
                    if error.status == 401
                    else {"Retry-After": "60"},
                )
                return await response(scope, receive, send)
        origin = headers.get(b"origin")
        if origin and path.startswith("/v1"):
            # REST is same-origin only. MCP additionally uses the SDK origin/host checks.
            if not any(
                fnmatchcase(origin.decode(), allowed)
                for allowed in self.service.settings.allowed_origins
            ):
                return await Response(status_code=403)(scope, receive, send)
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > 2 * 1024 * 1024:
                    return await JSONResponse({"error": {"code": "body_size"}}, status_code=413)(
                        scope, receive, send
                    )
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            sent = False

            async def buffered_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, buffered_receive, send)
        return await self.app(scope, receive, send)


def create_app(service=None):
    service = service or MemoryService(Settings.from_env())
    mcp = build_mcp(service)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=2 * 1024 * 1024,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(service.settings.allowed_hosts),
            allowed_origins=list(service.settings.allowed_origins),
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        # Startup verifies storage, but never silently changes a production schema.
        with service.db.session() as session:
            session.execute(text("SELECT key_id FROM credentials LIMIT 1"))
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="MuscleMemory by TheWariend",
        version="0.1.1",
        lifespan=lifespan,
        description="Your AI agents learn by doing. REST contract v1.",
        # The API reference lives on the website; the strict CSP here would block
        # Swagger's CDN scripts. /openapi.json stays available locally.
        docs_url=None,
        redoc_url=None,
    )
    app.state.memory = service

    @app.get("/docs", include_in_schema=False)
    def docs():
        return RedirectResponse(API_REFERENCE)

    app.add_middleware(SecurityBoundary, service=service)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, error):
        return await domain_error(request, conflict_error(error))

    @app.exception_handler(RequestValidationError)
    async def input_error(request, error):
        # Never echo invalid input values (which may contain credentials) into logs/responses.
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_input",
                    "fields": [list(item["loc"]) for item in error.errors()],
                }
            },
            status_code=422,
        )

    bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Scoped API key")

    def principal(
        request: Request,
        _bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    ):
        return request.state.mm_principal

    auth = Depends(principal)

    @app.get("/health")
    def health():
        with service.db.session() as session:
            session.execute(text("SELECT key_id FROM credentials LIMIT 1"))
        return {"status": "ok", "product": "MuscleMemory", "contract_version": "1"}

    @app.get("/v1/session")
    def identity(actor=auth):
        return {"tenant": actor.tenant, "grants": actor.grants, "key_id": actor.key_id}

    @app.post("/v1/runs", status_code=201)
    def create_run(body: CreateRunInput, actor=auth):
        return service.create_run(actor, body)

    @app.post("/v1/observe")
    def observe(body: ObserveInput, actor=auth):
        return service.observe(actor, body)

    @app.post("/v1/learn")
    def learn(body: LearnInput, actor=auth):
        return service.learn(actor, body)

    @app.post("/v1/recall")
    def recall(
        body: RecallInput,
        observation_run: Annotated[Token | None, Header(alias="X-MuscleMemory-Run")] = None,
        actor=auth,
    ):
        return service.recall(actor, body, observation_run=observation_run)

    @app.post("/v1/evaluate")
    def evaluate(body: EvaluateInput, actor=auth):
        return service.evaluate(actor, body)

    @app.post("/v1/inspect")
    def inspect(body: InspectInput, actor=auth):
        return service.inspect(actor, body)

    @app.get("/v1/procedures")
    def procedures(
        visibility_scope: Token,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        actor=auth,
    ):
        return service.listing(actor, visibility_scope, "procedures", limit, offset)

    @app.get("/v1/runs")
    def runs(
        visibility_scope: Token,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        actor=auth,
    ):
        return service.listing(actor, visibility_scope, "runs", limit, offset)

    @app.get("/v1/runs/{run_token}")
    def run(run_token: Token, actor=auth):
        return service.get_run(actor, run_token)

    @app.get("/v1/procedures/{procedure_token}")
    def procedure(procedure_token: Token, revision_selector: str = "latest", actor=auth):
        try:
            selector = "latest" if revision_selector == "latest" else int(revision_selector)
            request = InspectInput(procedure_token=procedure_token, revision_selector=selector)
        except ValueError:
            raise DomainError(
                "revision_selector", "Revision must be latest or a positive integer", 422
            ) from None
        return service.inspect(actor, request)

    @app.get("/v1/procedures/{procedure_token}/revisions")
    def revisions(procedure_token: Token, actor=auth):
        return {
            "items": service.inspect(actor, InspectInput(procedure_token=procedure_token))[
                "version_history"
            ]
        }

    @app.post("/v1/procedures/{procedure_token}/export")
    def export_skill(procedure_token: Token, agent: SkillAgent, actor=auth):
        return service.export_skill(actor, procedure_token, agent)

    @app.post("/v1/procedures/{procedure_token}/approve")
    def approve(procedure_token: Token, actor=auth):
        return service.approve(actor, procedure_token)

    @app.post("/v1/runs/{run_token}/auto-learn")
    def auto_learn(run_token: Token, hint: Token | None = None, actor=auth):
        return service.auto_learn(actor, run_token, hint)

    @app.post("/auth/sign-in")
    async def sign_in(request: Request):
        # Swaps a one-time code from `musclememory dashboard` for a dashboard key.
        origin = request.headers.get("origin")
        if origin and not any(
            fnmatchcase(origin, allowed) for allowed in service.settings.allowed_origins
        ):
            return Response(status_code=403)
        body = await request.json()
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or len(code) > 200:
            raise DomainError("sign_in_expired", "This sign-in link expired. Run it again.", 401)
        return await anyio.to_thread.run_sync(service.redeem_sign_in_code, code)

    @app.post("/v1/procedures/{procedure_token}/retire")
    def retire(procedure_token: Token, actor=auth):
        return service.retire(actor, procedure_token)

    @app.delete("/v1/procedures/{procedure_token}")
    def delete_procedure(procedure_token: Token, actor=auth):
        return service.delete_memory(actor, procedure_token)

    @app.delete("/v1/runs/{run_token}")
    def delete_run(run_token: Token, actor=auth):
        return service.delete_memory(actor, run_token, "run")

    @app.get("/v1/usage")
    def usage(visibility_scope: Token, actor=auth):
        return service.usage(actor, visibility_scope)

    @app.post("/v1/runs/{run_token}/usage")
    def record_session_usage(run_token: Token, body: SessionUsageInput, actor=auth):
        return service.record_session_usage(actor, run_token, body)

    @app.get("/v1/usage/sessions")
    def session_usage(
        visibility_scope: Token,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        actor=auth,
    ):
        return service.session_usage(actor, visibility_scope, limit, offset)

    @app.get("/v1/retrieval/config")
    def retrieval_config(actor=auth):
        return {
            "embedding": service.embeddings.metadata(),
            "guidance_token_budget": service.settings.guidance_token_budget,
            "semantic_min_similarity": service.settings.semantic_min_similarity,
        }

    @app.get("/v1/keys")
    def keys(visibility_scope: Token, actor=auth):
        return service.list_keys(actor, visibility_scope)

    @app.delete("/v1/keys/{key_id}")
    def revoke(key_id: Token, visibility_scope: Token, actor=auth):
        return service.revoke_key(actor, key_id, visibility_scope)

    dashboard = dashboard_dir()
    if dashboard.is_dir():
        # The server's own address and /dashboard without a slash both lead to the dashboard.
        @app.get("/", include_in_schema=False)
        @app.get("/dashboard", include_in_schema=False)
        def open_dashboard():
            return RedirectResponse("/dashboard/")

        app.mount("/dashboard", StaticFiles(directory=dashboard, html=True))
    app.mount("/", mcp_app)
    default_openapi = app.openapi

    def openapi_with_definition():
        schema = default_openapi()
        definition = ProcedureDefinition.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definition.pop("$defs", {}))
        components["ProcedureDefinition"] = definition
        return schema

    app.openapi = openapi_with_definition
    return app
