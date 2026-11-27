import chainlit as cl

from gpt_rag_ui.config.dependencies import get_config

config = get_config()

ENABLE_FEEDBACK = config.get("ENABLE_USER_FEEDBACK", False, bool)
FEEDBACK_RATING = config.get("USER_FEEDBACK_RATING", False, bool)

def create_feedback_actions(question_id: str, conversation_id: str, ask: str) -> list:
    """Create feedback actions for a message.

    Behavior:
    - If feedback is disabled: return no actions.
    - If rating is enabled: open feedback form (existing behavior).
    - If rating is disabled: send quick feedback directly (no popup).
    """

    if ENABLE_FEEDBACK is not True:
        return []

    if FEEDBACK_RATING:
        # Existing detailed form flow (rating + text)
        return [
            cl.Action(
                name="show_feedback_form",
                payload={
                    "questionId": question_id,
                    "conversationId": conversation_id,
                    "ask": ask,
                    "is_positive": 1,
                },
                label="👍",
                description="Give detailed feedback",
            ),
            cl.Action(
                name="show_feedback_form",
                payload={
                    "questionId": question_id,
                    "conversationId": conversation_id,
                    "ask": ask,
                    "is_positive": 0,
                },
                label="👎",
                description="Give detailed feedback",
            ),
        ]

    # Quick feedback (no rating/text) -> submit immediately
    return [
        cl.Action(
            name="submit_feedback",
            payload={
                "questionId": question_id,
                "conversationId": conversation_id,
                "ask": ask,
                "isPositive": True,
                # Explicit placeholders for compatibility; backend will ignore when rating is disabled
                "rating": None,
                "text": "",
            },
            label="👍",
            description="Send quick feedback",
        ),
        cl.Action(
            name="submit_feedback",
            payload={
                "questionId": question_id,
                "conversationId": conversation_id,
                "ask": ask,
                "isPositive": False,
                "rating": None,
                "text": "",
            },
            label="👎",
            description="Send quick feedback",
        ),
    ]


def feedback_requires_ownership(
    auth_payload: dict,
    *,
    allow_standalone_anonymous: bool,
) -> bool:
    copilot_auth_mode = str(
        auth_payload.get("copilot_auth_mode") or ""
    ).strip().lower()
    return bool(copilot_auth_mode) or not allow_standalone_anonymous
