"""Workbench News Pipeline Agent.

Adapted from ai_news_scraper. Provides:
- Multi-interest RSS feed management with full CRUD + editing
- AI-powered theme identification and content generation
- German script support (script_de)
- Background scheduler with catch-up
- Email delivery via SMTP
- Per-user interests, schedules, feeds, and pipeline runs
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agents.base import AgentBase
from workbench.core.agents import get_user_agent_settings
from workbench.core.auth import (
    get_current_user,
    get_user_inference_api_key,
    get_user_inference_providers,
)
from workbench.core.db import get_session, get_session_factory
from workbench.core.models import AgentSession, User

_news_scheduler: Any = None


def get_scheduler():
    return _news_scheduler


def set_scheduler(scheduler: Any) -> None:
    global _news_scheduler
    _news_scheduler = scheduler


class NewsAgent(AgentBase):
    name = "news"
    display_name = "News Pipeline"
    description = "Multi-interest AI-powered RSS news scraper, theme analyzer, and content generator"
    version = "0.2.0"
    icon = "newspaper"

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        self._register_routes(router)
        return router

    def _register_routes(self, router: APIRouter) -> None:
        # Interest CRUD
        router.add_api_route("/interests", self.list_interests, methods=["GET"])
        router.add_api_route("/interests", self.create_interest, methods=["POST"])
        router.add_api_route("/interests/{interest_id}", self.update_interest, methods=["PATCH"])
        router.add_api_route("/interests/{interest_id}", self.delete_interest, methods=["DELETE"])
        # Feed CRUD
        router.add_api_route("/interests/{interest_id}/feeds", self.list_feeds, methods=["GET"])
        router.add_api_route("/interests/{interest_id}/feeds", self.add_feed, methods=["POST"])
        router.add_api_route("/interests/{interest_id}/feeds/{feed_id}", self.update_feed, methods=["PATCH"])
        router.add_api_route("/interests/{interest_id}/feeds/{feed_id}", self.delete_feed, methods=["DELETE"])
        # Pipeline
        router.add_api_route("/interests/{interest_id}/run", self.trigger_run, methods=["POST"])
        router.add_api_route("/interests/{interest_id}/runs", self.list_runs, methods=["GET"])
        router.add_api_route("/runs/{run_id}/themes", self.get_themes, methods=["GET"])
        router.add_api_route("/runs/{run_id}/brief", self.get_brief, methods=["GET"])
        router.add_api_route("/runs/{run_id}/deliverables", self.get_deliverables, methods=["GET"])
        # Scheduler
        router.add_api_route("/interests/{interest_id}/next-run", self.get_next_run, methods=["GET"])
        router.add_api_route("/scheduler/status", self.scheduler_status, methods=["GET"])
        # Email verification (public endpoint — no auth required)
        router.add_api_route("/verify-email-recipient", self.verify_email_recipient_public, methods=["GET"])
        router.add_api_route("/interests/{interest_id}/send-verification-email", self.send_verification_email, methods=["POST"])

    # ---- Interests ----
    async def list_interests(self, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        interests = await store.list_interests(str(user.id))
        return {"interests": interests}

    async def create_interest(self, body: InterestCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        record = await store.create_interest(str(user.id), body.model_dump())
        return {"interest": record}

    async def update_interest(self, interest_id: int, body: InterestUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        existing = await store.get_interest(str(user.id), interest_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Interest not found")

        update_data = body.model_dump(exclude_none=True)

        # If email_recipient changed, reset verification status
        if "email_recipient" in update_data:
            new_recipient = update_data["email_recipient"].strip()
            old_recipient = (existing.get("email_recipient") or "").strip()
            if new_recipient and new_recipient != old_recipient:
                # Reset verified flag — the new email needs verification
                await store._s.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE news_interests SET email_recipient_verified = FALSE WHERE id = :iid"
                    ),
                    {"iid": interest_id},
                )
                await store._s.commit()

        updated = await store.update_interest(str(user.id), interest_id, update_data)
        return {"interest": updated}

    async def delete_interest(self, interest_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        existing = await store.get_interest(str(user.id), interest_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Interest not found")
        await store.delete_interest(str(user.id), interest_id)
        return {"status": "ok"}

    # ---- Feeds ----
    async def list_feeds(self, interest_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        feeds = await store.list_feeds(str(user.id), interest_id)
        return {"feeds": feeds}

    async def add_feed(self, interest_id: int, body: FeedCreate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        record = await store.add_feed(str(user.id), interest_id, body.url, body.name, body.category)
        return {"feed": record}

    async def update_feed(self, interest_id: int, feed_id: int, body: FeedUpdate, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        updated = await store.update_feed(str(user.id), interest_id, feed_id, body.model_dump(exclude_none=True))
        if not updated:
            raise HTTPException(status_code=404, detail="Feed not found")
        return {"feed": updated}

    async def delete_feed(self, interest_id: int, feed_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        await store.delete_feed(str(user.id), interest_id, feed_id)
        return {"status": "ok"}

    # ---- Pipeline ----
    async def trigger_run(self, interest_id: int, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        interest = await store.get_interest(str(user.id), interest_id)
        if not interest:
            raise HTTPException(status_code=404, detail="Interest not found")

        # Resolve per-agent provider override for base_url
        base_url = None
        all_agent_settings = await get_user_agent_settings(str(user.id), session)
        agent_cfg = all_agent_settings.get(self.name, {})
        agent_settings = agent_cfg.get("settings", {})
        agent_provider_id = agent_settings.get("provider_id") or None
        if agent_provider_id:
            providers = await get_user_inference_providers(user, session, request.app.state.config)
            agent_provider = next((p for p in providers if p["id"] == agent_provider_id), None)
            if agent_provider:
                base_url = agent_provider.get("provider_url")

        api_key = await get_user_inference_api_key(user, session)
        if not api_key:
            raise HTTPException(status_code=400, detail="OpenRouter API key not configured — set it in Settings")

        from workbench.services.news_pipeline import NewsPipeline
        pipeline = NewsPipeline(store, session)
        run_id = await pipeline.run(str(user.id), interest, api_key, base_url=base_url)

        # Save AgentSession
        try:
            import uuid as _uuid_mod
            from datetime import date as _date
            from datetime import datetime as _datetime

            def _sanitize(obj: Any) -> Any:
                """Recursively convert non-JSON-serializable types to str."""
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_sanitize(v) for v in obj]
                if isinstance(obj, (_uuid_mod.UUID, _date, _datetime)):
                    return str(obj)
                return obj

            title = interest.get("name", f"News Run {run_id}")
            agent_session = AgentSession(
                user_id=user.id,
                agent_name="news",
                session_id=str(run_id),
                title=title,
                state_json={
                    "interest": _sanitize(interest),
                    "run_id": run_id,
                    "pipeline_stage": "completed",
                },
                content=None,
                content_format="markdown",
                metadata_json=_sanitize({
                    "interest_id": interest.get("id"),
                    "interest_name": interest.get("name"),
                    "enable_summary": interest.get("enable_summary"),
                    "enable_script": interest.get("enable_script"),
                    "enable_brief": interest.get("enable_brief"),
                    "interval_hours": interest.get("interval_hours"),
                }),
            )
            session.add(agent_session)
            await session.commit()
        except Exception:
            logger = __import__("logging").getLogger(__name__)
            logger.exception("Failed to save AgentSession for news run %d", run_id)

        # Send email if configured — uses local Postfix on the VPS host.
        # No authentication needed (Docker containers are in mynetworks).
        if interest.get("enable_email"):
            try:
                from workbench.core.auth import get_server_config_value
                from workbench.services.news_emailer import send_pipeline_results

                # Determine recipient: use per-interest email only if verified,
                # otherwise fall back to user's own email
                custom_recipient = interest.get("email_recipient", "").strip()
                if custom_recipient and interest.get("email_recipient_verified"):
                    recipient = custom_recipient
                else:
                    recipient = user.email or "playa77@gmail.com"

                smtp_config = {
                    "host": await get_server_config_value(session, "smtp_host", "172.18.0.1"),
                    "port": int(await get_server_config_value(session, "smtp_port", "25")),
                    "user": await get_server_config_value(session, "smtp_user", ""),
                    "password": await get_server_config_value(session, "smtp_password", ""),
                    "sender": await get_server_config_value(session, "smtp_from", "emailfrom@workbench.gronowski.cc"),
                    "recipient": recipient,
                }

                logger = __import__("logging").getLogger(__name__)
                logger.info("Dispatching email for run %d to %s via %s:%s",
                             run_id, smtp_config["recipient"],
                             smtp_config["host"], smtp_config["port"])

                await send_pipeline_results(
                    run_id=run_id,
                    store=store,
                    interest=interest,
                    smtp_config=smtp_config,
                )
            except Exception as exc:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("Email dispatch failed: %s", exc)

        return {"run_id": run_id, "status": "started"}

    async def list_runs(self, interest_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        runs = await store.list_runs(str(user.id), interest_id)
        return {"runs": runs}

    async def get_themes(self, run_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        run = await store.get_run_for_user(str(user.id), run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        themes = await store.get_themes_for_run(run_id)
        return {"themes": themes}

    async def get_brief(self, run_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        run = await store.get_run_for_user(str(user.id), run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        brief = await store.get_daily_brief_for_run(run_id)
        return {"brief": brief}

    async def get_deliverables(self, run_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        run = await store.get_run_for_user(str(user.id), run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        themes = await store.get_themes_for_run(run_id)
        result = []
        for theme in themes:
            dels = await store.get_deliverables_for_theme(theme.get("id", 0))
            result.append({
                "theme_id": theme.get("id"),
                "theme_title": theme.get("title"),
                "deliverables": dels,
            })
        return {"deliverables": result}

    # ---- Scheduler ----
    async def get_next_run(self, interest_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from workbench.services.news_scheduler import _compute_next_run_seconds
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        interest = await store.get_interest(str(user.id), interest_id)
        if not interest:
            raise HTTPException(status_code=404, detail="Interest not found")

        now = datetime.now(ZoneInfo("Europe/Berlin"))
        seconds = _compute_next_run_seconds(
            interest.get("start_time", "04:00"),
            interest.get("interval_hours", 24),
            now,
        )
        next_at = now.timestamp() + seconds
        return {
            "next_run_seconds": seconds,
            "next_run_iso": datetime.fromtimestamp(next_at, tz=ZoneInfo("Europe/Berlin")).isoformat(),
        }

    async def scheduler_status(self, user: User = Depends(get_current_user)):
        sched = _news_scheduler
        return {
            "running": bool(sched) and sched._started,
        }

    # ---- Email Recipient Verification ----

    async def send_verification_email(
        self, interest_id: int, user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ):
        """Send a verification email to the interest's email_recipient address."""
        from workbench.services.news_store import NewsStore
        store = NewsStore(session)
        interest = await store.get_interest(str(user.id), interest_id)
        if not interest:
            raise HTTPException(status_code=404, detail="Interest not found")

        recipient = (interest.get("email_recipient") or "").strip()
        if not recipient:
            raise HTTPException(status_code=400, detail="No email recipient configured for this interest")

        if interest.get("email_recipient_verified"):
            return {"status": "already_verified", "message": "Email recipient is already verified"}

        # Generate and store verification token (24-hour expiry)
        raw_token = await store.set_email_recipient_verification_token(interest_id)

        # Build the verification link
        import os
        base_url = os.environ.get("WORKBENCH_BASE_URL", "https://workbench.gronowski.cc")
        verify_url = f"{base_url}/api/v1/agents/news/verify-email-recipient?interest_id={interest_id}&token={raw_token}"

        # Read SMTP overrides from the server_config DB table (admin-configured in Settings)
        from workbench.core.email import get_smtp_overrides_from_db
        smtp_overrides = await get_smtp_overrides_from_db(session)

        # Send the verification email
        try:
            from workbench.core.config import load_config
            from workbench.core.email import _send_email as send_smtp_email
            config = load_config()

            sent_ok = await send_smtp_email(
                config=config,
                to_address=recipient,
                subject="Verify your email for Workbench News Pipeline",
                html_body=(
                    f'<h2 style="font-weight:600;font-size:18px;margin-bottom:16px">Verify your email</h2>'
                    f'<p>You are receiving this email because this address was set as the recipient '
                    f'for news pipeline deliveries for interest <strong>{interest.get("name", "Unknown")}</strong>.</p>'
                    f'<p>Click the button below to verify your email address. This link expires in 24 hours.</p>'
                    f'<p><a href="{verify_url}" style="display:inline-block;background:#60a5fa;color:#0f1117;'
                    f'padding:10px 20px;border-radius:4px;text-decoration:none;font-weight:600;margin:16px 0">'
                    f'Verify email</a></p>'
                    f'<p style="font-size:12px;color:#6b6e7d">If you did not request this, you can ignore this email.</p>'
                ),
                plain_body=(
                    f"Verify your email address for Workbench News Pipeline:\n\n"
                    f"Interest: {interest.get('name', 'Unknown')}\n"
                    f"Verify here: {verify_url}\n\n"
                    f"This link expires in 24 hours.\n"
                    f"If you did not request this, you can ignore this email."
                ),
                smtp_overrides=smtp_overrides,
            )
            if not sent_ok:
                raise HTTPException(
                    status_code=500,
                    detail="SMTP not configured or failed to send. Configure SMTP in Settings or check server logs.",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to send verification email: {exc}")

        return {"status": "sent", "message": f"Verification email sent to {recipient}"}

    async def verify_email_recipient_public(
        self, interest_id: int = Query(...), token: str = Query(...),
    ):
        """Public endpoint (no auth) — verifies an email recipient token from a link clicked in email."""
        session_factory = get_session_factory()
        async with session_factory() as session:
            from workbench.services.news_store import NewsStore
            store = NewsStore(session)

            interest = await store.get_interest_by_id_global(interest_id)
            if not interest:
                return HTMLResponse(
                    content="<h1>Not Found</h1><p>This interest does not exist.</p>",
                    status_code=404,
                )

            success = await store.verify_email_recipient(interest_id, token)
            if success:
                return HTMLResponse(
                    content=(
                        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Email Verified</title>"
                        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
                        "display:flex;justify-content:center;align-items:center;min-height:100vh;"
                        "background:#0f1117;color:#e4e6eb;text-align:center}"
                        "h1{color:#60a5fa;margin-bottom:8px}p{color:#6b6e7d}</style></head>"
                        "<body><div><h1>Email Verified ✓</h1>"
                        f"<p>The email recipient for <strong>{interest.get('name', 'News Interest')}</strong> has been verified.</p>"
                        "<p>You can close this page.</p></div></body></html>"
                    ),
                )
            else:
                return HTMLResponse(
                    content="<h1>Verification Failed</h1><p>This link is invalid or has expired (links are valid for 24 hours). Please request a new verification email.</p>",
                    status_code=400,
                )

    def get_frontend_tab(self) -> dict:
        return {
            "id": self.name,
            "displayName": self.display_name,
            "icon": self.icon,
            "component": f"agent-{self.name}",
            "js": f"/static/js/components/{self.name}-tab.js",
        }


class InterestCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    start_time: str = "04:00"
    interval_hours: int = Field(default=24, ge=1, le=168)
    target_summary_words: int = Field(default=750, ge=50)
    target_script_words: int = Field(default=1250, ge=50)
    target_script_de_words: int = Field(default=1250, ge=50)
    target_brief_words: int = Field(default=600, ge=50)
    enable_summary: bool = True
    enable_script: bool = True
    enable_script_de: bool = False
    enable_brief: bool = True
    enable_email: bool = False
    input_data_length_mode: str = "full_article"
    input_word_count: int = Field(default=256, ge=1)
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_sender: str = ""
    email_recipient: str = ""


class InterestUpdate(BaseModel):
    name: str | None = None
    start_time: str | None = None
    interval_hours: int | None = None
    target_summary_words: int | None = None
    target_script_words: int | None = None
    target_script_de_words: int | None = None
    target_brief_words: int | None = None
    enable_summary: bool | None = None
    enable_script: bool | None = None
    enable_script_de: bool | None = None
    enable_brief: bool | None = None
    enable_email: bool | None = None
    input_data_length_mode: str | None = None
    input_word_count: int | None = None
    email_smtp_host: str | None = None
    email_smtp_port: int | None = None
    email_smtp_user: str | None = None
    email_sender: str | None = None
    email_recipient: str | None = None


class FeedCreate(BaseModel):
    name: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    category: str = "news"


class FeedUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    category: str | None = None
