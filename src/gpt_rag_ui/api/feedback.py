"""Chainlit feedback callback registration."""

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

import chainlit as cl

from gpt_rag_ui.clients.orchestrator_client import call_orchestrator_for_feedback
from gpt_rag_ui.services.conversation_security import get_owned_conversation
from gpt_rag_ui.services.feedback import FEEDBACK_RATING, feedback_requires_ownership


def register_feedback_handlers(
    auth_info: Callable[[], dict | Awaitable[dict]] | None = None,
    *,
    allow_standalone_anonymous: bool = False,
) -> None:
    """Register feedback-related Chainlit event handlers"""

    feedback_msg: cl.Message | None = None
    last_feedback_context: dict | None = None

    @cl.action_callback("show_feedback_form")
    async def on_show_feedback_form(action):
        """Show feedback form action"""
        nonlocal feedback_msg
        nonlocal last_feedback_context

        # If rating flow is disabled, ignore the popup action gracefully
        if not FEEDBACK_RATING:
            logging.info("[feedback] Rating flow disabled; ignoring show_feedback_form action")
            return await cl.context.emitter.send_toast("Feedback form is disabled.", "info")

        question_id = action.payload.get("questionId", "dummy-question-id")
        conversation_id = action.payload.get("conversationId", "dummy-conversation-id")
        logging.info("[feedback] Opening feedback form with questionId=%s conversationId=%s", question_id, conversation_id)
        # Persist the context to recover in case the frontend omits fields when submitting
        last_feedback_context = {
            "question_id": question_id,
            "conversation_id": conversation_id,
            "ask": action.payload.get("ask"),
            "is_positive": action.payload.get("is_positive"),
        }
        feedback_element = cl.CustomElement(
            name="FeedbackForm",
            props={
                "conversationId": conversation_id,
                "questionId": question_id,
                "ask": action.payload.get("ask"),
                "isPositive": action.payload.get("is_positive"),
                "feedbackType": "general",
                "show": True,
            },
            display="inline",
        )

        feedback_msg = cl.Message(content="", elements=[feedback_element])
        await feedback_msg.send()
        return None

    @cl.action_callback("submit_feedback")
    async def handle_feedback(action):
        """Handle feedback submission"""
        nonlocal feedback_msg
        nonlocal last_feedback_context
        notification_type: Literal["success", "error"]
        try:
            logging.info("[feedback] Received submit_feedback action")
            question_id = action.payload.get("questionId")
            ask = action.payload.get("ask")
            conversation_id = action.payload.get("conversationId")
            text = action.payload.get("text", "")
            rating = action.payload.get("rating")
            is_positive = action.payload.get("isPositive")

            # Fallback from stored context if fields are missing
            if not question_id and last_feedback_context:
                question_id = last_feedback_context.get("question_id")
            if not ask and last_feedback_context:
                ask = last_feedback_context.get("ask")
            if not conversation_id and last_feedback_context:
                conversation_id = last_feedback_context.get("conversation_id")
            if is_positive is None and last_feedback_context:
                is_positive = last_feedback_context.get("is_positive")

            # Validate required fields
            if not conversation_id:
                raise ValueError("[app] Conversation ID is required.")
            if FEEDBACK_RATING and rating is None:
                raise ValueError("[app] Rating is required when USER_FEEDBACK_RATING is enabled.")
            if not question_id:
                raise ValueError("[app] Ask ID is missing; cannot submit feedback.")

            # Call orchestrator
            auth_payload = auth_info() if auth_info else {}
            if inspect.isawaitable(auth_payload):
                auth_payload = await auth_payload
            requires_ownership = feedback_requires_ownership(
                auth_payload,
                allow_standalone_anonymous=allow_standalone_anonymous,
            )
            if (
                requires_ownership
                and not await get_owned_conversation(
                    conversation_id,
                    auth_payload,
                )
            ):
                logging.warning(
                    "[feedback] Conversation ownership denied: conversation=%s",
                    conversation_id,
                )
                return await cl.context.emitter.send_toast(
                    "You are not authorized to submit feedback for this conversation.",
                    "error",
                )
            orc_feedback_response = await call_orchestrator_for_feedback(
                conversation_id=conversation_id,
                question_id=question_id,
                ask=ask,
                is_positive=is_positive,
                star_rating=rating,
                feedback_text=text,
                auth_info=auth_payload,
            )
        except Exception:
            # Record the primary outcome before attempting optional presentation cleanup.
            logging.error("[feedback] Error while handling feedback submission")
            notification = "An unexpected error occurred while submitting feedback."
            notification_type = "error"
        else:
            if orc_feedback_response:
                notification = "Thank you for your feedback!"
                notification_type = "success"
            else:
                notification = "Error: Failed to submit feedback"
                notification_type = "error"

        # A presentation failure must not reclassify or repeat a completed write.
        if feedback_msg is not None:
            try:
                await feedback_msg.remove()
            except Exception:
                logging.error("[feedback] Failed to remove feedback form; submission outcome unchanged")
            else:
                feedback_msg = None
        return await cl.context.emitter.send_toast(notification, notification_type)

    @cl.action_callback("close_feedback_popup")
    async def close_feedback_handler(action):
        nonlocal feedback_msg
        if feedback_msg is not None:
            await feedback_msg.remove()
            feedback_msg = None
