"""A2A server exposing the external agent as a networked peer.

Runs as its own process. The manager reaches it over HTTP — there is no
in-process import path from the orchestrator to `ExternalAgent`, which is the
point: this is the seam where another team's agent could be swapped in.

Run:
    uvicorn app.a2a.server:app --port 8500

Serves:
    GET  /.well-known/agent-card.json   Agent Card (A2A standard path)
    GET  /.well-known/agent.json        Agent Card (legacy alias)
    POST /message:send                  Task submission (a2a-sdk REST binding)
    GET  /tasks/{id}                    Task status
"""

import asyncio
import json
import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Task,
    TaskState,
    TaskStatus,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.agents.external import ExternalAgent
from app.config.settings import get_settings

logger = logging.getLogger(__name__)

SKILL_ID = "external_research"


def build_agent_card() -> AgentCard:
    """Describe this agent for discovery by A2A clients."""
    settings = get_settings()
    return AgentCard(
        name="external-research-agent",
        description=(
            "Searches public web and market-research sources and returns "
            "normalized, cited evidence for a single sub-question."
        ),
        version="0.2.0",
        supported_interfaces=[
            AgentInterface(
                url=settings.external_agent_url,
                protocol_binding="REST",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=SKILL_ID,
                name="External Research",
                description=(
                    "Given a sub-question, search allowlisted public sources and "
                    "return evidence items with url, source, date and authority."
                ),
                tags=["research", "web", "market-intelligence"],
                examples=[
                    "What straight-through processing rates do AP automation vendors claim?",
                    "What are published benchmarks for invoice processing error rates?",
                ],
                input_modes=["text/plain"],
                output_modes=["application/json"],
            )
        ],
    )


class ExternalResearchExecutor(AgentExecutor):
    """Bridges A2A task lifecycle to `ExternalAgent.research`.

    Lifecycle emitted: submitted -> working -> completed (or failed).
    """

    def __init__(self) -> None:
        self.agent = ExternalAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The request handler requires the Task itself on the queue before any
        # status update, so create one when this is a fresh (non-follow-up) call.
        task = context.current_task
        if task is None:
            task = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        await updater.submit()
        await updater.start_work()

        question = (context.get_user_input() or "").strip()
        logger.info("A2A task %s: research %r", context.task_id, question)

        if not question:
            await updater.failed(
                message=updater.new_agent_message([{"text": "empty question"}])
            )
            return

        try:
            # Off the event loop, in a worker thread: `ExternalAgent.research`
            # makes a blocking Tavily HTTP call, and this server only runs one
            # asyncio worker. Calling it inline serializes every concurrent
            # A2A task through that one call -- fine at low concurrency, but a
            # comparison question fans out many subquestions at once, and each
            # one queued behind the last blows straight through the client's
            # per-agent timeout even though no single search was slow.
            evidence = await asyncio.to_thread(self.agent.research, question)
            payload = {
                "question": question,
                "evidence": [e.to_dict() for e in evidence],
                "count": len(evidence),
            }
            await updater.add_artifact(
                [{"text": json.dumps(payload)}],
                name="external_evidence",
            )
            await updater.complete()
            logger.info("A2A task %s completed with %d evidence", context.task_id, len(evidence))
        except Exception as exc:  # noqa: BLE001 - report failure through the protocol
            logger.exception("A2A task %s failed", context.task_id)
            await updater.failed(
                message=updater.new_agent_message(
                    [{"text": f"external research failed: {exc}"}]
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancellation is not supported; tasks are short-lived."""
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.failed(
            message=updater.new_agent_message([{"text": "cancel not supported"}])
        )


def create_app() -> FastAPI:
    """Build the A2A FastAPI application."""
    agent_card = build_agent_card()
    handler = DefaultRequestHandler(
        agent_executor=ExternalResearchExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    application = FastAPI(title="External Research Agent (A2A)", version="0.2.0")

    for route in create_agent_card_routes(agent_card):
        application.router.routes.append(route)
    for route in create_rest_routes(handler):
        application.router.routes.append(route)

    @application.get("/.well-known/agent.json")
    def legacy_agent_card() -> JSONResponse:
        """Alias for clients expecting the pre-standard card path."""
        from google.protobuf.json_format import MessageToDict

        return JSONResponse(MessageToDict(agent_card))

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "agent": agent_card.name}

    return application


app = create_app()
