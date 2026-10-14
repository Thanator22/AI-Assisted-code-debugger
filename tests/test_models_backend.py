from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repo_doctor.models_backend import ClaudeAdapter, ClaudeBackendError
from repo_doctor.model_proposer import ProposalPrompt


@pytest.fixture
def configured_adapter(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    read_key = Mock(return_value="test-key")
    monkeypatch.setattr("repo_doctor.models_backend.getpass", read_key)

    client = Mock()
    client.models.list.return_value = [
        SimpleNamespace(
            id="model-a",
            display_name="Model A",
            lifecycle="active",
        ),
        SimpleNamespace(
            id="model-b",
            display_name="Model B",
            lifecycle="active",
        ),
    ]
    client.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"example": true}')],
    )

    factory = Mock(return_value=client)
    monkeypatch.setattr("repo_doctor.models_backend.Anthropic", factory)

    adapter = ClaudeAdapter()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    monkeypatch.setattr(
        "repo_doctor.models_backend.load_saved_key",
        lambda backend: None,
    )

    # Prevent tests from overwriting your actual saved credentials.
    monkeypatch.setattr(
        "repo_doctor.models_backend.save_key",
        Mock(),
    )
    adapter.ensure_authenticated()

    yield adapter, client, read_key, factory

    adapter.close()


def test_authentication_reuses_client(configured_adapter):
    adapter, client, read_key, factory = configured_adapter
    adapter.ensure_authenticated()

    read_key.assert_called_once()
    factory.assert_called_once_with(
        api_key="test-key",
        timeout=60.0,
        max_retries=0,
    )
    assert [model.id for model in adapter.list_models()] == [
        "model-a",
        "model-b",
    ]


def test_transport_keeps_its_selected_model(configured_adapter):
    adapter, client, _, _ = configured_adapter
    first = adapter.create_transport("model-a")
    second = adapter.create_transport("model-b")
    prompt = ProposalPrompt("Instructions", "Evidence")

    first(prompt)
    second(prompt)
    first(prompt)

    calls = client.messages.create.call_args_list
    assert [call.kwargs["model"] for call in calls] == [
        "model-a",
        "model-b",
        "model-a",
    ]
    assert calls[0].kwargs["system"] == "Instructions"
    assert calls[0].kwargs["messages"] == [
        {"role": "user", "content": "Evidence"}
    ]
    assert "tools" not in calls[0].kwargs


def test_rejects_token_limited_response(configured_adapter):
    adapter, client, _, _ = configured_adapter
    client.messages.create.return_value.stop_reason = "max_tokens"

    with pytest.raises(ClaudeBackendError, match="normal response"):
        adapter.create_transport("model-a")(
            ProposalPrompt("Instructions", "Evidence")
        )


def test_rejects_unknown_model(configured_adapter):
    adapter, client, _, _ = configured_adapter

    with pytest.raises(ClaudeBackendError, match="model list"):
        adapter.create_transport("unknown-model")

    client.messages.create.assert_not_called()


def test_empty_key_does_not_create_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    monkeypatch.setattr(
        "repo_doctor.models_backend.load_saved_key",
        lambda backend: None,
    )
    monkeypatch.setattr(
        "repo_doctor.models_backend.getpass",
        lambda prompt: "",
    )

    factory = Mock()
    monkeypatch.setattr(
        "repo_doctor.models_backend.Anthropic",
        factory,
    )

    with pytest.raises(ClaudeBackendError, match="key is required"):
        ClaudeAdapter().ensure_authenticated()

    factory.assert_not_called()