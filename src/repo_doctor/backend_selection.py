from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from repo_doctor.model_proposer import ProposalPrompt


class Backend(str, Enum):
    COPILOT = "copilot"
    CLAUDE = "claude"
    CODEX = "codex"


BACKEND_LABELS = {
    Backend.COPILOT: "GitHub Copilot",
    Backend.CLAUDE: "Claude",
    Backend.CODEX: "Codex",
}


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str

    def __post_init__(self):
        if not self.id.strip() or not self.label.strip():
            raise ValueError("Model ID and label cannot be empty")


@dataclass(frozen=True)
class ModelSelection:
    backend: Backend
    model_id: str


Transport = Callable[[ProposalPrompt], str]


class BackendAdapter(Protocol):
    def ensure_authenticated(self) -> None:
        """Reuse valid credentials or initiate supported authentication."""
        ...

    def list_models(self) -> Sequence[ModelOption]:
        """Return selectable models supported by this adapter."""
        ...

    def create_transport(self, model_id: str) -> Transport:
        """Create a proposal-only transport using the selected model."""
        ...


def choose_number(
    prompt: str,
    labels: Sequence[str],
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    if not labels:
        raise ValueError("No choices are available")

    for number, label in enumerate(labels, start=1):
        write(f"{number}. {label}")

    while True:
        answer = read(prompt).strip()

        try:
            index = int(answer) - 1
        except ValueError:
            write("Enter a listed number.")
            continue

        if 0 <= index < len(labels):
            return index

        write("Enter a listed number.")


class BackendSelector:
    def __init__(self, adapters: Mapping[Backend, BackendAdapter]):
        self.adapters = dict(adapters)
        self.selection: ModelSelection | None = None
        self.transport: Transport | None = None

    def configure(
        self,
        *,
        switch_model_only: bool = False,
        read: Callable[[str], str] = input,
        write: Callable[[str], None] = print,
    ) -> ModelSelection:
        if switch_model_only:
            if self.selection is None:
                raise ValueError("Choose a backend first")
            backend = self.selection.backend
        else:
            backends = [
                backend
                for backend in Backend
                if backend in self.adapters
            ]

            index = choose_number(
                "Choose backend: ",
                [BACKEND_LABELS[backend] for backend in backends],
                read=read,
                write=write,
            )
            backend = backends[index]

        adapter = self.adapters[backend]

        # The adapter decides whether existing authentication is sufficient.
        adapter.ensure_authenticated()

        models = tuple(adapter.list_models())
        if len({model.id for model in models}) != len(models):
            raise ValueError("Backend returned duplicate model IDs")

        index = choose_number(
            "Choose model: ",
            [f"{model.label} ({model.id})" for model in models],
            read=read,
            write=write,
        )
        model = models[index]

        # Commit the new selection only after setup succeeds.
        transport = adapter.create_transport(model.id)
        selection = ModelSelection(
            backend=backend,
            model_id=model.id,
        )

        self.selection = selection
        self.transport = transport
        return selection