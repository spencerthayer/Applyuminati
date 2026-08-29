"""FastAPI application factory.

The app serves the built React bundle at ``/`` and the API at ``/api/v1`` from
the same origin — this is what makes the Portainer-pasteable single-image
compose file work without CORS or a second container.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from applyuminati import __version__
from applyuminati.api.errors import applyuminati_error_handler
from applyuminati.api.routers.applications import router as applications_router
from applyuminati.api.routers.health import router as health_router
from applyuminati.api.routers.jobs import router as jobs_router
from applyuminati.api.routers.profile import router as profile_router
from applyuminati.api.routers.settings import dashboard_router, settings_router
from applyuminati.api.routers.sources import router as sources_router
from applyuminati.core.errors import ApplyuminatiError
from applyuminati.core.logging import get_logger
from applyuminati.core.settings import Settings
from applyuminati.services.container import ServiceContainer, get_container, set_container

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = get_container()
    log.info("api.starting", version=__version__, data_dir=str(container.settings.data_dir))
    yield
    log.info("api.shutting_down")
    await container.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Called by uvicorn in the entrypoint."""
    container = ServiceContainer(settings) if settings else get_container()
    if settings is not None:
        set_container(container)

    app = FastAPI(
        title="Applyuminati",
        description="Local-first, autonomous, LLM-powered job search and application platform.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        openapi_url="/api/v1/openapi.json",
    )

    # CORS: empty by default because the bundled UI is same-origin. The env
    # var exists for a separate dev-server setup.
    if container.settings.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=container.settings.server.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_exception_handler(ApplyuminatiError, applyuminati_error_handler)

    app.include_router(health_router)
    app.include_router(profile_router)
    app.include_router(jobs_router)
    app.include_router(sources_router)
    app.include_router(applications_router)
    app.include_router(dashboard_router)
    app.include_router(settings_router)

    _mount_static(app, container)
    return app


def _mount_static(app: FastAPI, container: ServiceContainer) -> None:
    """Serve the built React bundle at ``/`` when it exists.

    In development the web_dist path is unset and the SPA is served by Vite;
    in Docker the bundle is baked into the image and served here.
    """
    web_dist = container.settings.server.web_dist
    if web_dist is None:
        return
    dist = Path(web_dist)
    if not dist.is_dir():
        log.warning("api.web_dist_missing", path=str(dist))
        return

    # Static assets (JS, CSS, images) under /assets.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse | JSONResponse:
        # Try to serve a real file first; fall back to index.html for client-side routing.
        candidate = dist / path
        if path and candidate.is_file():
            return FileResponse(str(candidate))
        if index.is_file():
            return FileResponse(str(index))
        return JSONResponse({"detail": "web UI not built"}, status_code=404)


app = create_app()
