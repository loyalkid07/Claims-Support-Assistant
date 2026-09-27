"""LiveKit voice entrypoint for the synthetic Observe Insurance claims demo."""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace

import httpx
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.plugins import ai_coustics

from airtable_writer import AirtableWriter
from claim_delivery import claim_detail, claim_headline
from errors import PolicyError
from handoff import HANDOFF_WAIT_SECONDS, SupabaseHandoffRepository
from outbox import InteractionDelivery, SupabaseOutbox
from postcall import CompletionOnce, CompletionRunner, TrustedEvent
from prompts import PERSONA_NAME
from prompts.v2 import INSTRUCTIONS, PROMPT_VERSION
from session_state import Auth, Handoff, SessionState
from supabase_claims import SupabaseClaimsRepository
from verification import IdentityVerifier
from voice_workflow import VoiceWorkflow

logger = logging.getLogger("claims-support-assistant")

load_dotenv(".env.local")

AGENT_NAME = "claims-support-assistant"
STT_MODEL_ID = "deepgram/nova-3"
LLM_MODEL_ID = "openai/gpt-4.1-mini"
TTS_MODEL_ID = "cartesia/sonic-3"
TTS_VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
WORKFLOW_VERSION = "workflow-v2"
MODEL_STACK_VERSION = f"{STT_MODEL_ID}|{LLM_MODEL_ID}|{TTS_MODEL_ID}|{TTS_VOICE_ID}"

OPENING = (
    f"Thanks for calling Observe Insurance. I'm {PERSONA_NAME}, your automated "
    "claims assistant. I can check on a claim, talk through next steps, or "
    "help you reach a representative. How can I help today?"
)

HANDOFF_INTRO = (
    "A representative has joined. I'll step out now so you can continue with them."
)


async def finish_handoff_segment(
    session: AgentSession, completion: CompletionRunner
) -> None:
    """End and persist only the AI segment, leaving the human room intact."""

    speech = session.say(HANDOFF_INTRO)
    await speech.wait_for_playout()
    session.shutdown(drain=True)
    await completion.run()


def _safe_tool_error(workflow: VoiceWorkflow, exc: PolicyError) -> dict[str, str]:
    workflow.tool_error_count += 1
    status = {
        "CONSENT_REQUIRED": "entry_not_ready",
        "CONFIRMATION_REQUIRED": "later_caller_confirmation_required",
        "CANDIDATE_REQUIRED": "phone_confirmation_required",
        "UNAUTHENTICATED": "verification_required",
        "AUTH_LOCKED": "verification_locked",
        "INVALID_INPUT": "clarification_needed",
        "HANDOFF_IN_PROGRESS": "handoff_in_progress",
    }.get(exc.code, "temporarily_unavailable")
    return {"status": status}


class ClaimsAssistant(Agent):
    """Thin LiveKit tools over server-owned state and authorized adapters."""

    def __init__(
        self,
        workflow: VoiceWorkflow,
        on_handoff_waiting: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            llm=inference.LLM(model=LLM_MODEL_ID), instructions=INSTRUCTIONS
        )
        self.workflow = workflow
        self._on_handoff_waiting = on_handoff_waiting
        self._claim_projection: dict | None = None
        self._claim_projection_ref: str | None = None

    @function_tool()
    async def prepare_phone(self, context: RunContext, spoken: str) -> dict:
        """Prepare a synthetic account phone; return only its last four for readback."""

        try:
            return self.workflow.prepare_phone(spoken)
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def confirm_phone(self, context: RunContext) -> dict:
        """Process the caller's latest answer to the phone last-four readback."""

        try:
            return await self.workflow.confirm_phone()
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def prepare_bundle(
        self, context: RunContext, claim_id_spoken: str, postal_spoken: str
    ) -> dict:
        """Prepare a synthetic claim ID and postal code for caller readback."""

        try:
            result = self.workflow.prepare_bundle(claim_id_spoken, postal_spoken)
            if self.workflow.state.auth != Auth.CANDIDATE_READY:
                return {"status": "bundle_saved_wait_for_phone_confirmation"}
            return result
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def confirm_bundle(self, context: RunContext) -> dict:
        """Process the caller's answer to the claim and ZIP readback."""

        try:
            return await self.workflow.confirm_bundle()
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def get_claim_status(self, context: RunContext) -> dict:
        """Return verified status only; fulfill an earlier detail request separately."""

        try:
            self._claim_projection = await self.workflow.get_claim_status()
            self._claim_projection_ref = self.workflow.state.require_claim_ref()
            return claim_headline(self._claim_projection)
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def get_claim_detail(self, context: RunContext, topic: str) -> dict:
        """Get one verified detail topic requested by the caller.

        Use representative for an assigned person's name or contact.
        Use claim_context for claim ID, type, loss date, or last update.
        Other topics: next_step, documents, document_receipt.
        """

        try:
            claim_ref = self.workflow.state.require_claim_ref()
            if (
                self._claim_projection is None
                or self._claim_projection_ref != claim_ref
            ):
                return {"status": "claim_status_required"}
            return claim_detail(self._claim_projection, topic)
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)

    @function_tool()
    async def get_faq(self, context: RunContext, topic_id: str) -> dict:
        """Get approved public guidance for one FAQ topic ID, or an unknown result."""

        return self.workflow.get_faq(topic_id)

    @function_tool()
    async def request_representative(
        self, context: RunContext, reason_category: str | None = None
    ) -> dict:
        """Request a human; optional stated-reason category only.

        Valid categories: claim_status, documents, new_claim, complaint,
        general_question. Omit the value if the caller gave no clear reason.
        """

        try:
            result = await self.workflow.request_representative(reason_category)
            if result["status"] == "waiting" and self._on_handoff_waiting:
                self._on_handoff_waiting(result["handoff_id"])
            return result
        except PolicyError as exc:
            return _safe_tool_error(self.workflow, exc)


server = AgentServer(shutdown_process_timeout=25.0)


@server.rtc_session()
async def claims_support_agent(ctx: JobContext) -> None:
    """Run one caller session and persist one sanitized completion on shutdown."""

    ctx.log_context_fields = {"room": ctx.room.name}
    state = SessionState(room_name=ctx.room.name)
    client = httpx.AsyncClient()
    try:
        claims = SupabaseClaimsRepository(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"], client
        )
        faq = await claims.load_faq_snapshot()
        verifier = IdentityVerifier(
            state,
            claims,
            os.environ["IDENTIFIER_HMAC_SECRET"].encode(),
        )
        handoffs = SupabaseHandoffRepository(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"], client
        )
        workflow = VoiceWorkflow(state, verifier, claims, faq, handoffs)
        delivery = InteractionDelivery(
            SupabaseOutbox(
                os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"], client
            ),
            AirtableWriter(
                os.environ["AIRTABLE_PAT"],
                os.environ["AIRTABLE_BASE_ID"],
                os.environ["AIRTABLE_TABLE_ID"],
                client,
            ),
        )
    except Exception:
        await client.aclose()
        raise

    completion = CompletionOnce()
    handoff_timer: asyncio.Task | None = None
    handoff_join_tasks: set[asyncio.Task] = set()
    handoff_lock = asyncio.Lock()

    async def persist_completion() -> None:
        try:
            if handoff_timer and not handoff_timer.done():
                handoff_timer.cancel()
            for task in tuple(handoff_join_tasks):
                task.cancel()
            if state.handoff == Handoff.WAITING and state.handoff_id:
                try:
                    await handoffs.mark_cancelled(state.handoff_id)
                except PolicyError as exc:
                    logger.warning("handoff cancellation failed: code=%s", exc.code)
                state.fail_handoff(state.handoff_id)
                workflow.events.append(TrustedEvent.HANDOFF_UNAVAILABLE)
            state.end()
            verified_name = None
            if state.verified_customer_ref:
                try:
                    verified_name = await claims.get_verified_caller_name(state)
                except PolicyError as exc:
                    workflow.tool_error_count += 1
                    logger.warning(
                        "verified caller name unavailable: code=%s call_id=%s",
                        exc.code,
                        state.call_id,
                    )
            if not workflow.events:
                workflow.events.append(TrustedEvent.CALLER_DISCONNECTED)
            interaction = completion.build(
                state,
                tuple(workflow.events),
                tuple(workflow.caller_cues),
                verified_name=verified_name,
            )
            interaction = replace(
                interaction,
                tool_error_count=workflow.tool_error_count,
                agent_version="0.1.0",
                prompt_version=PROMPT_VERSION,
                workflow_version=WORKFLOW_VERSION,
                model_stack_version=MODEL_STACK_VERSION,
            )
            delivery_task = asyncio.create_task(delivery.queue_and_attempt(interaction))
            try:
                result = await asyncio.shield(delivery_task)
            except asyncio.CancelledError:
                # Finish the durable write before releasing the HTTP client.
                result = await asyncio.shield(delivery_task)
            logger.info("post-call delivery state=%s call_id=%s", result, state.call_id)
        except Exception as exc:
            logger.error("post-call persistence failed: %s", type(exc).__name__)
        finally:
            await client.aclose()

    completion_runner = CompletionRunner(persist_completion)
    ctx.add_shutdown_callback(completion_runner.run)

    session = AgentSession(
        stt=inference.STT(model=STT_MODEL_ID, language="en"),
        tts=inference.TTS(
            model=TTS_MODEL_ID,
            voice=TTS_VOICE_ID,
            language="en",
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            endpointing={"mode": "fixed", "min_delay": 0.3, "max_delay": 2.5},
            interruption={"mode": "adaptive"},
            preemptive_generation={"enabled": False},
        ),
    )

    @session.on("conversation_item_added")
    def on_conversation_item_added(event) -> None:
        item = event.item
        if getattr(item, "role", None) == "user":
            workflow.caller_turn_committed(item.text_content or "")

    async def on_supervisor_join(participant) -> None:
        async with handoff_lock:
            try:
                connected = await workflow.supervisor_joined(
                    ctx.room.name, participant.identity, dict(participant.attributes)
                )
            except PolicyError as exc:
                logger.warning("supervisor event rejected: code=%s", exc.code)
                return
            if not connected:
                return
            if handoff_timer and not handoff_timer.done():
                handoff_timer.cancel()
            logger.info("validated supervisor joined: handoff_id=%s", state.handoff_id)
            # Completion cancels stale join handlers; this one owns the handoff.
            handoff_join_tasks.discard(asyncio.current_task())
            await finish_handoff_segment(session, completion_runner)

    @ctx.room.on("participant_connected")
    def on_participant_connected(participant) -> None:
        task = asyncio.create_task(on_supervisor_join(participant))
        handoff_join_tasks.add(task)
        task.add_done_callback(handoff_join_tasks.discard)

    def on_handoff_waiting(handoff_id: str) -> None:
        nonlocal handoff_timer

        async def wait_for_supervisor() -> None:
            await asyncio.sleep(HANDOFF_WAIT_SECONDS)
            async with handoff_lock:
                try:
                    expired = await workflow.expire_handoff(handoff_id)
                except PolicyError as exc:
                    logger.warning("handoff expiry failed: code=%s", exc.code)
                    expired = False
                if expired:
                    session.say(
                        "I'm sorry, but I couldn't connect a representative right now. "
                        "You can call again during our service hours."
                    )

        handoff_timer = asyncio.create_task(wait_for_supervisor())

    logger.info(
        "starting claims session with stt=%s llm=%s tts=%s",
        STT_MODEL_ID,
        LLM_MODEL_ID,
        TTS_MODEL_ID,
    )

    await session.start(
        agent=ClaimsAssistant(workflow, on_handoff_waiting),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )
    await ctx.connect()
    session.say(OPENING)
    # The invited entry path owns the pre-call notice. This legacy state flag
    # enables tools without a spoken yes; it is not identity verification.
    state.accept_consent()


if __name__ == "__main__":
    cli.run_app(server)
