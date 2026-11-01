"""Single registration owner for Chainlit callbacks and data-layer wiring."""

import logging

import chainlit as cl

from gpt_rag_ui.services import chat

logger = logging.getLogger(__name__)
_registered = False


def register_callbacks() -> None:
    global _registered
    if _registered:
        return
    from gpt_rag_ui.auth.recovery import register_recovery_action
    register_recovery_action()
    if chat.OAUTH_CONFIGURED:
        from gpt_rag_ui.api.oauth import register_oauth_callback

        register_oauth_callback()
    if getattr(chat, "NATIVE_SETTINGS", None) is not None:
        from gpt_rag_ui.api.native_chat import register
        register(chat.get_auth_info)
        _registered = True
        return
    if chat.OAUTH_CONFIGURED or chat.COPILOT_ENABLED:
        if chat.CHAT_BACKEND == "orchestrator":
            from gpt_rag_ui.api.history import register_data_layer

            register_data_layer()
        else:
            logger.info(
                "Hosted chat does not register the classic orchestrator history "
                "data layer; conversation context remains session-scoped"
            )
    if chat.ENABLE_FEEDBACK:
        from gpt_rag_ui.api.feedback import register_feedback_handlers

        register_feedback_handlers(
            chat.get_auth_info,
            allow_standalone_anonymous=chat.ALLOW_ANONYMOUS and not chat.OAUTH_CONFIGURED,
        )
    cl.on_chat_start(chat.on_chat_start)
    cl.on_chat_resume(chat.on_chat_resume)
    cl.on_message(chat.handle_message)
    _registered = True
