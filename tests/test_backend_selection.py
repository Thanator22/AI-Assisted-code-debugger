from unittest.mock import Mock

import pytest

from repo_doctor.backend_selection import (
    Backend,
    BackendSelector,
    ModelOption,
    ModelSelection,
)


def fake_adapter():
    adapter = Mock()
    adapter.list_models.return_value = (
        ModelOption("model-a", "Model A"),
        ModelOption("model-b", "Model B"),
    )
    adapter.create_transport.side_effect = lambda model_id: Mock()
    return adapter


def configure_with_answers(selector, *answers, **kwargs):
    responses = iter(answers)
    return selector.configure(
        read=lambda prompt: next(responses),
        write=lambda message: None,
        **kwargs,
    )


def test_authenticates_only_selected_backend():
    copilot = fake_adapter()
    claude = fake_adapter()
    codex = fake_adapter()

    selector = BackendSelector({
        Backend.COPILOT: copilot,
        Backend.CLAUDE: claude,
        Backend.CODEX: codex,
    })

    selection = configure_with_answers(selector, "2", "1")

    assert selection == ModelSelection(Backend.CLAUDE, "model-a")
    claude.ensure_authenticated.assert_called_once()
    claude.create_transport.assert_called_once_with("model-a")
    copilot.ensure_authenticated.assert_not_called()
    codex.ensure_authenticated.assert_not_called()


def test_switches_model_within_selected_backend():
    adapter = fake_adapter()
    selector = BackendSelector({Backend.CODEX: adapter})

    configure_with_answers(selector, "1", "1")
    previous_transport = selector.transport

    selection = configure_with_answers(
        selector,
        "2",
        switch_model_only=True,
    )

    assert selection == ModelSelection(Backend.CODEX, "model-b")
    assert selector.transport is not previous_transport


def test_failed_switch_preserves_previous_selection():
    adapter = fake_adapter()
    selector = BackendSelector({Backend.CODEX: adapter})

    configure_with_answers(selector, "1", "1")
    previous_selection = selector.selection
    previous_transport = selector.transport

    adapter.create_transport.side_effect = RuntimeError(
        "Model setup failed"
    )

    with pytest.raises(RuntimeError, match="Model setup failed"):
        configure_with_answers(
            selector,
            "2",
            switch_model_only=True,
        )

    assert selector.selection == previous_selection
    assert selector.transport is previous_transport


def test_invalid_menu_input_is_retried():
    adapter = fake_adapter()
    selector = BackendSelector({Backend.COPILOT: adapter})

    selection = configure_with_answers(
        selector,
        "invalid", "0", "1",  # Backend selection
        "99", "2",            # Model selection
    )

    assert selection == ModelSelection(Backend.COPILOT, "model-b")