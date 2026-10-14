import os
from getpass import getpass

from anthropic import Anthropic, APIError
import asyncio
import webbrowser
from pathlib import Path
from tempfile import TemporaryDirectory

from copilot import CopilotClient
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from openai_codex.types import TurnStatus

from repo_doctor.backend_selection import (
    ModelOption,
    Transport,
    choose_number,
)
from repo_doctor.model_proposer import ProposalPrompt
from repo_doctor.proposals import MAX_RESPONSE_CHARACTERS
from repo_doctor.backend_selection import ModelOption, Transport, app_data_directory
from keyring.backends.Windows import WinVaultKeyring


def load_saved_key(name: str) -> str:
    return WinVaultKeyring().get_password("RepoDoctor", name) or ""


def save_key(name: str, value: str) -> None:
    try:
        WinVaultKeyring().set_password("RepoDoctor", name, value)
    except Exception as error:
        print(
            "Authenticated, but could not save credentials: "
            f"{type(error).__name__}"
        )

class ModelBackendError(RuntimeError):
    """Backend failure with a message suitable for display."""


def _validate_response(text: str | None) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ModelBackendError("Backend returned no response text")

    if len(text) > MAX_RESPONSE_CHARACTERS:
        raise ModelBackendError("Backend response exceeds the size limit")

    return text


def _require_model(
    models: tuple[ModelOption, ...],
    model_id: str,
) -> None:
    if model_id not in {model.id for model in models}:
        raise ModelBackendError("Choose a model from the backend's model list")


class ClaudeBackendError(RuntimeError):
    """Raised when Claude authentication or generation fails."""


class ClaudeAdapter:
    def __init__(self):
        self._client: Anthropic | None = None
        self._models: tuple[ModelOption, ...] = ()

    def ensure_authenticated(self) -> None:
        if self._client is not None:
            return

        key = (
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or load_saved_key("claude")
        )
        if not key:
            key = getpass("Anthropic API key: ").strip()

        if not key:
            raise ClaudeBackendError("An Anthropic API key is required")

        client = Anthropic(
            api_key=key,
            timeout=60.0,
            max_retries=0,
        )

        try:
            # Iteration follows pagination through the SDK.
            models = tuple(
                ModelOption(model.id, model.display_name)
                for model in client.models.list()
                if getattr(model, "lifecycle", "active") == "active"
            )
        except APIError as error:
            client.close()
            raise ClaudeBackendError(
                f"Could not load Claude models: {type(error).__name__}"
            ) from None
        except Exception:
            client.close()
            raise

        if not models:
            client.close()
            raise ClaudeBackendError("No active Claude models were returned")

        self._client = client
        self._models = models
        save_key("claude", key)

    def list_models(self) -> tuple[ModelOption, ...]:
        if self._client is None:
            raise ClaudeBackendError("Authenticate before listing models")
        return self._models

    def create_transport(self, model_id: str) -> Transport:
        if self._client is None:
            raise ClaudeBackendError("Authenticate before creating transport")

        if model_id not in {model.id for model in self._models}:
            raise ClaudeBackendError("Choose a model from the model list")

        # Capture this model and client for this transport.
        client = self._client

        def transport(prompt: ProposalPrompt) -> str:
            try:
                response = client.messages.create(
                    model=model_id,
                    max_tokens=4_096,
                    system=prompt.instructions,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt.evidence,
                        }
                    ],
                )
            except APIError as error:
                raise ClaudeBackendError(
                    f"Claude request failed: {type(error).__name__}"
                ) from None

            if response.stop_reason != "end_turn":
                raise ClaudeBackendError(
                    "Claude did not complete a normal response: "
                    f"{response.stop_reason}"
                )

            if any(block.type != "text" for block in response.content):
                raise ClaudeBackendError(
                    "Claude returned unexpected non-text content"
                )

            text = "".join(block.text for block in response.content)

            if not text.strip():
                raise ClaudeBackendError("Claude returned an empty response")

            if len(text) > MAX_RESPONSE_CHARACTERS:
                raise ClaudeBackendError("Claude response exceeds size limit")

            return text

        return transport

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._models = ()
            
class CopilotAdapter:
    def __init__(self):
        self._token: str | None = None
        self._models: tuple[ModelOption, ...] = ()

    def _client(self, working_directory: str) -> CopilotClient:
        return CopilotClient(
            mode="empty",
            working_directory=working_directory,
            github_token=self._token,
            use_logged_in_user=self._token is None,
        )

    async def _discover_models(self):
        with TemporaryDirectory(prefix="repo-doctor-copilot-") as directory:
            async with self._client(directory) as client:
                status = await client.get_auth_status()

                if not status.isAuthenticated:
                    return None

                models = await client.list_models()
                return tuple(
                    ModelOption(model.id, model.name)
                    for model in models
                )

    def ensure_authenticated(self) -> None:
        if self._models:
            return
        self._token = load_saved_key("copilot") or None

        try:
            models = asyncio.run(self._discover_models())

            if models is None:
                print(
                    "No Copilot sign-in was found. Enter a supported "
                    "GitHub token, or cancel and sign in using Copilot CLI."
                )
                self._token = getpass("GitHub token: ").strip()

                if not self._token:
                    raise ModelBackendError("A GitHub sign-in is required")

                models = asyncio.run(self._discover_models())

            if not models:
                raise ModelBackendError(
                    "Copilot authentication returned no usable models"
                )

            self._models = models
            if self._token:
                save_key("copilot", self._token)
        except ModelBackendError:
            self._token = None
            raise
        except Exception as error:
            self._token = None
            raise ModelBackendError(
                f"Copilot setup failed: {type(error).__name__}"
            ) from None

    def list_models(self) -> tuple[ModelOption, ...]:
        if not self._models:
            raise ModelBackendError("Authenticate with Copilot first")
        return self._models

    async def _generate(
        self,
        model_id: str,
        prompt: ProposalPrompt,
    ) -> str:
        with TemporaryDirectory(prefix="repo-doctor-copilot-") as directory:
            async with self._client(directory) as client:
                async with await client.create_session(
                    model=model_id,
                    available_tools=[],
                    system_message={
                        "mode": "replace",
                        "content": prompt.instructions,
                    },
                ) as session:
                    response = await session.send_and_wait(
                        prompt.evidence,
                        timeout=120.0,
                    )

                    if response is None:
                        raise ModelBackendError(
                            "Copilot returned no assistant message"
                        )

                    return _validate_response(response.data.content)

    def create_transport(self, model_id: str) -> Transport:
        _require_model(self._models, model_id)

        def transport(prompt: ProposalPrompt) -> str:
            _require_model(self._models, model_id)
            try:
                return asyncio.run(self._generate(model_id, prompt))
            except ModelBackendError:
                raise
            except Exception as error:
                raise ModelBackendError(
                    f"Copilot request failed: {type(error).__name__}"
                ) from None

        return transport

    def close(self) -> None:
        # Each operation already closes its SDK client and session.
        self._token = None
        self._models = ()
        
class CodexAdapter:
    def __init__(self):
        self._client: Codex | None = None
        self._runtime: TemporaryDirectory | None = None
        self._working_directory: str | None = None
        self._models: tuple[ModelOption, ...] = ()

    def ensure_authenticated(self) -> None:
        if self._client is not None:
            return

        runtime = TemporaryDirectory(prefix="repo-doctor-codex-")
        root = Path(runtime.name)
        codex_home = app_data_directory() / "codex"
        working_directory = root / "work"
        codex_home.mkdir(parents=True, exist_ok=True)
        working_directory.mkdir()

        # This environment applies only to the SDK's child runtime.
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(codex_home)

        client = None

        try:
            client = Codex(
                CodexConfig(
                    cwd=str(working_directory),
                    env=environment,
                    config_overrides=(
                        'web_search="disabled"',
                        "features.shell_tool=false",
                        "features.unified_exec=false",
                        "features.apps=false",
                        "project_doc_max_bytes=0",
                        'cli_auth_credentials_store="keyring"',
                    ),
                )
            )

            if client.account().account is None:
                method = choose_number(
                    "Choose Codex authentication: ",
                    ["Sign in with ChatGPT", "Use an OpenAI API key"],
                )

                if method == 0:
                    login = client.login_chatgpt()
                    print("Complete sign-in in your browser:")
                    print(login.auth_url)
                    webbrowser.open(login.auth_url)

                    try:
                        completed = login.wait()
                    except BaseException:
                        login.cancel()
                        raise

                    if not completed.success:
                        raise ModelBackendError("ChatGPT sign-in failed")
                else:
                    key = os.environ.get("OPENAI_API_KEY", "").strip()
                    if not key:
                        key = getpass("OpenAI API key: ").strip()

                    if not key:
                        raise ModelBackendError("An OpenAI API key is required")

                    client.login_api_key(key)

            catalog = client.models()
            models = tuple(
                ModelOption(model.model, model.display_name)
                for model in catalog.data
                if not model.hidden
            )

            if not models:
                raise ModelBackendError("Codex returned no selectable models")

            self._client = client
            self._runtime = runtime
            self._working_directory = str(working_directory)
            self._models = models

        except BaseException:
            try:
                if client is not None:
                    client.close()
            finally:
                runtime.cleanup()
            raise

    def list_models(self) -> tuple[ModelOption, ...]:
        if self._client is None:
            raise ModelBackendError("Authenticate with Codex first")
        return self._models

    def create_transport(self, model_id: str) -> Transport:
        _require_model(self._models, model_id)
        client = self._client

        if client is None:
            raise ModelBackendError("Authenticate with Codex first")

        def transport(prompt: ProposalPrompt) -> str:
            if self._client is not client:
                raise ModelBackendError("This Codex transport has been closed")

            try:
                thread = client.thread_start(
                    model=model_id,
                    cwd=self._working_directory,
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=True,
                    base_instructions=prompt.instructions,
                )
                result = thread.run(prompt.evidence)

                if result.status != TurnStatus.completed:
                    raise ModelBackendError(
                        "Codex did not complete the proposal"
                    )

                return _validate_response(result.final_response)
            except ModelBackendError:
                raise
            except Exception as error:
                raise ModelBackendError(
                    f"Codex request failed: {type(error).__name__}"
                ) from None

        return transport

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        finally:
            self._client = None
            self._models = ()
            self._working_directory = None

            if self._runtime is not None:
                self._runtime.cleanup()
                self._runtime = None
