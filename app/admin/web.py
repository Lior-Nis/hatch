"""Internal operator web UI. Functional, local-only, server-rendered.

It has no authentication: bind it to localhost (the default for ``hatch
serve``) and do not expose it.
"""

import getpass
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.bootstrap import build_asset_store
from app.config import get_settings
from app.db import make_engine, registry
from app.experiments.lineage import ExperimentNotFound, Lineage, get_lineage
from app.production.models import Asset
from app.quality.models import HumanReview, ReviewDecision
from app.quality.review import REVIEWABLE, ReviewNotAllowed, pending_reviews, submit_review
from app.storage import AssetStore

assert registry  # every ORM model must be registered before any session is used

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def get_session(request: Request) -> Iterator[Session]:
    state = request.app.state
    if state.engine is None:
        state.engine = make_engine(get_settings().database_url)
    with Session(state.engine, expire_on_commit=False) as session:
        yield session


def get_asset_store() -> AssetStore:
    return build_asset_store(get_settings())


def get_reviewer() -> str:
    return get_settings().reviewer or getpass.getuser()


SessionDep = Annotated[Session, Depends(get_session)]
StoreDep = Annotated[AssetStore, Depends(get_asset_store)]
ReviewerDep = Annotated[str, Depends(get_reviewer)]


def _lineage_or_404(session: Session, experiment_id: uuid.UUID) -> Lineage:
    try:
        return get_lineage(session, experiment_id)
    except ExperimentNotFound:
        raise HTTPException(status_code=404, detail="experiment not found") from None


def _review_page(
    request: Request, lineage: Lineage, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    reviewable = lineage.experiment.video_status in {status.value for status in REVIEWABLE}
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {"lineage": lineage, "reviewable": reviewable, "error": error},
        status_code=status_code,
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if app.state.engine is not None:
            app.state.engine.dispose()

    app = FastAPI(title="Hatch", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.engine = None

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/review", status_code=307)

    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request, session: SessionDep) -> HTMLResponse:
        recent = session.scalars(
            select(HumanReview)
            .options(selectinload(HumanReview.experiment))
            .order_by(HumanReview.created_at.desc())
            .limit(25)
        ).all()
        return templates.TemplateResponse(
            request, "review_queue.html", {"pending": pending_reviews(session), "recent": recent}
        )

    @app.get("/review/{experiment_id}", response_class=HTMLResponse)
    def review_detail(
        request: Request, experiment_id: uuid.UUID, session: SessionDep
    ) -> HTMLResponse:
        return _review_page(request, _lineage_or_404(session, experiment_id))

    @app.post("/review/{experiment_id}")
    def review_submit(
        request: Request,
        experiment_id: uuid.UUID,
        session: SessionDep,
        reviewer: ReviewerDep,
        decision: Annotated[ReviewDecision, Form()],
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        lineage = _lineage_or_404(session, experiment_id)
        try:
            submit_review(
                session, experiment_id, decision=decision, reason=reason, reviewer=reviewer
            )
        except ValueError:
            return _review_page(
                request,
                lineage,
                error="A reason is required to reject a video.",
                status_code=422,
            )
        except ReviewNotAllowed as exc:
            return _review_page(request, lineage, error=str(exc), status_code=409)
        session.commit()
        return RedirectResponse("/review", status_code=303)

    @app.get("/assets/{asset_id}/content")
    def asset_content(asset_id: uuid.UUID, session: SessionDep, store: StoreDep) -> FileResponse:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(store.local_path(asset.storage_uri), media_type=asset.mime_type)

    return app
