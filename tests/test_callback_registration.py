"""Backend-specific callback registration without network or Chainlit sessions."""

import importlib.util
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


class TestCallbackRegistration(unittest.TestCase):
    def register(self, backend, *, oauth=True, copilot=False, feedback=False):
        spec = importlib.util.find_spec("gpt_rag_ui.api.callbacks")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        chainlit = ModuleType("chainlit")
        for name in ("on_chat_start", "on_chat_resume", "on_message"):
            setattr(chainlit, name, Mock())
        chat = SimpleNamespace(
            CHAT_BACKEND=backend,
            OAUTH_CONFIGURED=oauth,
            COPILOT_ENABLED=copilot,
            ENABLE_FEEDBACK=feedback,
            ALLOW_ANONYMOUS=not oauth,
            get_auth_info=Mock(),
            on_chat_start=Mock(),
            on_chat_resume=Mock(),
            handle_message=Mock(),
        )
        services = ModuleType("gpt_rag_ui.services")
        services.chat = chat
        history = ModuleType("gpt_rag_ui.api.history")
        history.register_data_layer = Mock()
        auth = ModuleType("gpt_rag_ui.api.oauth")
        auth.register_oauth_callback = Mock()
        feedback_module = ModuleType("gpt_rag_ui.api.feedback")
        feedback_module.register_feedback_handlers = Mock()
        modules = {
            "chainlit": chainlit,
            "gpt_rag_ui.services": services,
            "gpt_rag_ui.api.history": history,
            "gpt_rag_ui.api.oauth": auth,
            "gpt_rag_ui.api.feedback": feedback_module,
        }
        callbacks = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(callbacks)
            callbacks.register_callbacks()
            callbacks.register_callbacks()
        for name in ("on_chat_start", "on_chat_resume", "on_message"):
            getattr(chainlit, name).assert_called_once_with(getattr(chat, {
                "on_message": "handle_message",
            }.get(name, name)))
        return history, auth, feedback_module, chat

    def test_hosted_retains_oauth_without_classic_history(self):
        history, auth, _, _ = self.register("hosted_agent")
        history.register_data_layer.assert_not_called()
        auth.register_oauth_callback.assert_called_once()

    def test_classic_retains_authenticated_history(self):
        history, auth, _, _ = self.register("orchestrator")
        history.register_data_layer.assert_called_once()
        auth.register_oauth_callback.assert_called_once()

    def test_hosted_copilot_does_not_register_classic_history(self):
        history, auth, _, _ = self.register("hosted_agent", oauth=False, copilot=True)
        history.register_data_layer.assert_not_called()
        auth.register_oauth_callback.assert_not_called()

    def test_classic_copilot_preserves_history(self):
        history, auth, _, _ = self.register("orchestrator", oauth=False, copilot=True)
        history.register_data_layer.assert_called_once()
        auth.register_oauth_callback.assert_not_called()

    def test_anonymous_classic_does_not_enable_history(self):
        history, auth, _, _ = self.register("orchestrator", oauth=False)
        history.register_data_layer.assert_not_called()
        auth.register_oauth_callback.assert_not_called()

    def test_hosted_preserves_authenticated_feedback(self):
        _, _, feedback, chat = self.register("hosted_agent", feedback=True)
        feedback.register_feedback_handlers.assert_called_once_with(
            chat.get_auth_info, allow_standalone_anonymous=False
        )

    def test_hosted_preserves_explicit_anonymous_feedback(self):
        _, _, feedback, chat = self.register("hosted_agent", oauth=False, feedback=True)
        feedback.register_feedback_handlers.assert_called_once_with(
            chat.get_auth_info, allow_standalone_anonymous=True
        )
