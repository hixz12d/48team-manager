"""48 Team Manager application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.application.auth import initialize_admin_password
from app.core.config import Settings, load_settings
from app.persistence.database import create_engine, create_session_factory
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.web.deps import html_login_redirect, session_dependency
from app.web.routes.auth import build_auth_router
from app.web.routes.api import build_api_router
from app.web.routes.pages import build_pages_router

WEB_DIR = Path(__file__).resolve().parent / "web"


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings)
    logger = logging.getLogger("team48")

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    get_db = session_dependency(session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.secret_key == "your-secret-key-here-change-in-production":
            logger.warning("SECRET_KEY is still the example value")
        if settings.admin_password == "admin123":
            logger.warning("ADMIN_PASSWORD is still the example value")
        if settings.auto_rotate_enabled or settings.force_refill:
            logger.warning("auto rotate / force refill must stay off until explicitly approved")
        await bootstrap_schema(engine)
        async with session_factory() as session:
            await initialize_admin_password(session, settings)
            from app.application.operations import recover_stale_operations

            await recover_stale_operations(session)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.settings = settings
        from app.application.jobs.scheduler import start_scheduler, stop_scheduler
        from app.application.jobs.dispatcher import reauth_dispatcher

        start_scheduler(settings, session_factory)
        reauth_dispatcher.start(session_factory, deployment_allowed=settings.auto_reauth_enabled)
        yield
        stop_scheduler()
        await reauth_dispatcher.stop()
        await engine.dispose()

    app = FastAPI(
        title="48 Team Manager",
        description="Self-hosted operations console for ChatGPT Team / Workspace accounts.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        redirected = html_login_redirect(request, HTTPException(status_code=exc.status_code, detail=exc.detail))
        if redirected is not None:
            return redirected
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.effective_session_secret_key,
        session_cookie="session",
        max_age=14 * 24 * 60 * 60,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.globals["app_version"] = __version__

    app.include_router(build_pages_router(templates, __version__))
    app.include_router(build_auth_router(get_db, settings))
    app.include_router(build_api_router(get_db))

    @app.get("/health")
    async def health():
        from app.application.jobs.dispatcher import reauth_dispatcher

        runtime = await reauth_dispatcher.summary()
        return {
            "status": "healthy",
            "app": "48 Team Manager",
            "version": __version__,
            "runtime": runtime,
        }

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(WEB_DIR / "static" / "logo.svg")

    return app


app = create_app()
