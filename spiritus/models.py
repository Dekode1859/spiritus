"""Provider/model identifiers used by the public Spiritus API."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Model:
    """A fully-qualified OpenCode model identifier.

    Model IDs may themselves contain slashes (for example an OpenRouter model),
    so parsing always splits on the first slash only.
    """

    provider_id: str
    model_id: str

    def __post_init__(self) -> None:
        provider = self.provider_id.strip()
        model = self.model_id.strip()
        if not provider or not model:
            raise ValueError("model must include non-empty provider and model IDs")
        if any(character.isspace() for character in provider + model):
            raise ValueError("provider and model IDs cannot contain whitespace")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "model_id", model)

    @classmethod
    def parse(cls, value: Model | str) -> Model:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or "/" not in value:
            raise ValueError("model must use the form 'provider/model-id'")
        return cls(*value.split("/", 1))

    def __str__(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def as_request(self) -> dict[str, str]:
        return {"providerID": self.provider_id, "modelID": self.model_id}
