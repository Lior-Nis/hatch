"""Internal operator web UI. Functional, local-only, server-rendered.

It has no authentication: bind it to localhost (the default for ``hatch
serve``) and do not expose it.
"""

import getpass
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.admin import accounts, display, views
from app.bootstrap import build_asset_store
from app.budgets.governor import BudgetLimits
from app.config import get_settings
from app.db import make_engine, registry
from app.experiments.lineage import ExperimentNotFound, Lineage, get_lineage
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.observability.trace import experiment_timeline
from app.production.models import Asset
from app.quality.models import HumanReview, QAResult, ReviewDecision
from app.quality.ports import QAOutcome
from app.quality.review import (
    REVIEWABLE,
    ReviewNotAllowed,
    audit_rejection,
    pending_reviews,
    submit_review,
)
from app.storage import AssetStore

assert registry  # every ORM model must be registered before any session is used

# Stage A of progressive autonomy: a human approves each of the first ~100 videos.
STAGE_A_VIDEOS = 100

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["status"] = display.status_tag
templates.env.filters["status_label"] = display.status_label
templates.env.filters["relation"] = display.relation_label
templates.env.filters["when"] = display.when
templates.env.globals["reject_reasons"] = display.REJECT_REASONS


def get_session(request: Request) -> Iterator[Session]:
    state = request.app.state
    if state.engine is None:
        state.engine = make_engine(get_settings().database_url)
    with Session(state.engine, expire_on_commit=False) as session:
        yield session


def get_asset_store() -> AssetStore:
    return build_asset_store(get_settings())


def get_reviewer(request: Request) -> str:
    """Who is acting. In production nginx authenticates each person and passes
    the username in X-Remote-User (the app listens only on localhost, behind
    it). Run locally without a proxy, it falls back to the configured name."""
    return (
        request.headers.get("x-remote-user", "").strip()
        or get_settings().reviewer
        or getpass.getuser()
    )


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
    final_id = lineage.final_asset.id if lineage.final_asset else None
    current_qa = [r for r in lineage.qa_results if r.asset_id == final_id]
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "lineage": lineage,
            "reviewable": reviewable,
            "error": error,
            "qa_results": current_qa,
            "escalations": [r for r in current_qa if r.outcome == "escalate"],
            "auditable": lineage.experiment.video_status == VideoStatus.QA_REJECTED.value
            and not any(r.decision.startswith("audit_") for r in lineage.human_reviews),
        },
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

    def limits() -> BudgetLimits:
        return BudgetLimits.from_settings(get_settings())

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, session: SessionDep) -> HTMLResponse:
        context = views.portfolio(session, limits())
        return templates.TemplateResponse(
            request, "videos.html", {**context, "videos": views.videos(session)}
        )

    @app.get("/ips", response_class=HTMLResponse)
    def ips_page(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "dashboard.html", views.portfolio(session, limits())
        )

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "accounts.html", {"rows": accounts.checklist(session)}
        )

    @app.get("/ips/{slug}", response_class=HTMLResponse)
    def ip_page(request: Request, slug: str, session: SessionDep) -> HTMLResponse:
        detail = views.ip_detail(session, slug)
        if detail is None:
            raise HTTPException(status_code=404, detail="IP not found")
        return templates.TemplateResponse(request, "ip.html", detail)

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(
        request: Request, experiment_id: uuid.UUID, session: SessionDep
    ) -> HTMLResponse:
        lineage = _lineage_or_404(session, experiment_id)
        return templates.TemplateResponse(
            request,
            "experiment.html",
            {
                "lineage": lineage,
                "tree": views.family_tree(session, experiment_id),
                "timeline": experiment_timeline(session, experiment_id),
                **views.experiment_relations(session, experiment_id),
            },
        )

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(request, "queue.html", views.queue(session))

    @app.get("/costs", response_class=HTMLResponse)
    def costs_page(request: Request, session: SessionDep) -> HTMLResponse:
        return templates.TemplateResponse(request, "costs.html", views.costs(session, limits()))

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions_page(
        request: Request, session: SessionDep, ip: str | None = None
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "decisions.html",
            {"decisions": views.decisions(session, ip_slug=ip), "ip": ip},
        )

    @app.get("/review", response_class=HTMLResponse)
    def review_queue(request: Request, session: SessionDep) -> HTMLResponse:
        recent = session.scalars(
            select(HumanReview)
            .options(selectinload(HumanReview.experiment))
            .order_by(HumanReview.created_at.desc())
            .limit(25)
        ).all()
        pending = pending_reviews(session)
        flagged = set(
            session.scalars(
                select(HumanReview.experiment_id).where(
                    HumanReview.decision == ReviewDecision.FLAG,
                    HumanReview.experiment_id.in_([e.id for e in pending]),
                )
            )
        )
        escalated = set(
            session.scalars(
                select(QAResult.experiment_id).where(
                    QAResult.outcome == QAOutcome.ESCALATE,
                    QAResult.experiment_id.in_([e.id for e in pending]),
                )
            )
        )
        decided = session.scalar(
            select(func.count(func.distinct(HumanReview.experiment_id))).where(
                HumanReview.decision.in_((ReviewDecision.APPROVE, ReviewDecision.REJECT))
            )
        )
        audited = select(HumanReview.experiment_id).where(
            HumanReview.decision.in_((ReviewDecision.AUDIT_AGREE, ReviewDecision.AUDIT_DISAGREE))
        )
        rejected = session.scalars(
            select(Experiment)
            .where(
                Experiment.video_status == VideoStatus.QA_REJECTED,
                Experiment.id.not_in(audited),
            )
            .order_by(Experiment.created_at.desc())
            .limit(20)
        ).all()
        return templates.TemplateResponse(
            request,
            "review_queue.html",
            {
                "rejected": rejected,
                "pending": pending,
                "recent": recent,
                "flagged": flagged,
                "escalated": escalated,
                "decided": decided or 0,
                "stage_a_target": STAGE_A_VIDEOS,
            },
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
        reason_code: Annotated[str, Form()] = "",
    ) -> Response:
        lineage = _lineage_or_404(session, experiment_id)
        if reason_code and reason_code not in display.REJECT_REASONS:
            return _review_page(
                request, lineage, error="Pick one of the listed reasons.", status_code=422
            )
        if reason_code:
            label = display.REJECT_REASONS[reason_code]
            reason = f"{label}: {reason.strip()}" if reason.strip() else label
        try:
            submit_review(
                session, experiment_id, decision=decision, reason=reason, reviewer=reviewer
            )
        except ValueError as exc:
            message = str(exc)
            return _review_page(
                request, lineage, error=message[0].upper() + message[1:] + ".", status_code=422
            )
        except ReviewNotAllowed as exc:
            return _review_page(request, lineage, error=str(exc), status_code=409)
        session.commit()
        flash = {
            ReviewDecision.APPROVE: "approved",
            ReviewDecision.REJECT: "rejected",
            ReviewDecision.FLAG: "flagged",
        }.get(decision, "")
        if decision is ReviewDecision.FLAG:
            target = "/review"
        else:
            # Straight on to the next video waiting, so a queue is worked in one pass.
            waiting = [e for e in pending_reviews(session) if e.id != experiment_id]
            target = f"/review/{waiting[0].id}" if waiting else "/review"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie("flash", flash, max_age=30, path="/", samesite="lax")
        return response

    @app.post("/review/{experiment_id}/audit")
    def review_audit(
        request: Request,
        experiment_id: uuid.UUID,
        session: SessionDep,
        reviewer: ReviewerDep,
        verdict: Annotated[Literal["agree", "disagree"], Form()],
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        """Audit an automated rejection. The video stays rejected either way."""
        lineage = _lineage_or_404(session, experiment_id)
        try:
            audit_rejection(
                session, experiment_id, agrees=verdict == "agree", reason=reason, reviewer=reviewer
            )
        except ValueError:
            return _review_page(
                request,
                lineage,
                error="A reason is required to audit a rejection.",
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
