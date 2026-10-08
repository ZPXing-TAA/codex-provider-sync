"""Provider-aware normalization for persisted Responses API history.

This module deliberately keeps provider provenance next to, but separate from,
the request payload. Its output is safe to pass to a request serializer; the
provenance object itself must never be sent as a Responses API item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import re
from typing import Any, Callable, Iterable, Mapping


JsonObject = dict[str, Any]
DiagnosticLogger = Callable[[str], None]

REASONING_TYPES = {"reasoning"}
OPAQUE_STATE_TYPES = {
    "compaction",
    "context_compaction",
    "response.compaction",
    "response.summary",
    "summary",
}
MESSAGE_TYPES = {"message", "system", "developer", "user", "assistant"}
TOOL_CALL_TYPES = {"function_call", "tool_call"}
TOOL_RESULT_TYPES = {"function_call_output", "tool_result", "tool_result_output"}
KNOWN_PORTABLE_TYPES = MESSAGE_TYPES | {
    "input_text",
    "output_text",
    "input_image",
    "output_image",
    "input_file",
    "file",
}
OPAQUE_KEYS = {
    "encrypted_content",
    "previous_response_id",
    "response_id",
    "compaction_id",
    "reasoning_id",
}
OPENAI_ITEM_ID_PREFIXES = {
    "reasoning": "rs_",
    "message": "msg_",
    "function_call": "fc_",
    "tool_call": "fc_",
}


@dataclass(frozen=True)
class ProviderProvenance:
    """Metadata about the provider that produced provider-native state."""

    source_provider: str | None
    source_endpoint: str | None = None
    source_model: str | None = None
    source_auth_mode: str | None = None
    source_transport: str | None = None
    source_continuation_scope: str | None = None

    def is_compatible_with(
        self,
        *,
        target_provider: str,
        target_endpoint: str | None = None,
        target_model: str | None = None,
        target_auth_mode: str | None = None,
        target_transport: str | None = None,
        target_continuation_scope: str | None = None,
        continuation_compatible: bool = False,
    ) -> bool:
        """Return whether provider-native continuation state may be replayed.

        Provider names and item-ID prefixes are not proof of compatibility.
        The request host must explicitly assert that the old continuation state
        is valid in the current authenticated continuation domain.
        """

        if not continuation_compatible or self.source_provider != target_provider:
            return False
        pairs = (
            (self.source_endpoint, target_endpoint),
            (self.source_model, target_model),
            (self.source_auth_mode, target_auth_mode),
            (self.source_transport, target_transport),
            (self.source_continuation_scope, target_continuation_scope),
        )
        return all(
            source == target
            for source, target in pairs
            if source is not None or target is not None
        )


@dataclass(frozen=True)
class HistoryDiagnostic:
    action: str
    item_type: str
    reason: str
    item_id: str | None = None

    def render(self, target_provider: str, source_provider: str | None) -> str:
        source = source_provider or "unknown"
        item_id = _redact_id(self.item_id)
        suffix = f" id={item_id}" if item_id else ""
        return (
            f"[provider-switch] target={target_provider} source={source} "
            f"{self.action} {self.item_type}{suffix} reason={self.reason}"
        )


@dataclass
class NormalizationResult:
    items: list[JsonObject]
    diagnostics: list[HistoryDiagnostic] = field(default_factory=list)


@dataclass(frozen=True)
class PersistedResponseHistory:
    """Canonical persisted history plus provider provenance.

    ``provenance`` is intentionally not part of ``items`` and must not be
    serialized as a Responses API item.
    """

    items: tuple[JsonObject, ...]
    provenance: ProviderProvenance | None = None


class HistoryNormalizationError(ValueError):
    """Raised when same-provider tool state is structurally inconsistent."""


def normalize_response_history(
    history: Iterable[Mapping[str, Any]],
    *,
    source_provider: str | None,
    target_provider: str,
    source_endpoint: str | None = None,
    source_model: str | None = None,
    source_auth_mode: str | None = None,
    source_transport: str | None = None,
    source_continuation_scope: str | None = None,
    target_endpoint: str | None = None,
    target_model: str | None = None,
    target_auth_mode: str | None = None,
    target_transport: str | None = None,
    target_continuation_scope: str | None = None,
    continuation_compatible: bool = False,
    logger: DiagnosticLogger | None = None,
) -> NormalizationResult:
    """Return request-safe history for ``target_provider``.

    Incompatible-continuation normalization preserves semantic message content
    but drops provider-native continuation state. Native state is preserved
    only when the caller explicitly asserts continuation compatibility; it is
    then validated instead of silently repaired.
    """

    provenance = ProviderProvenance(
        source_provider,
        source_endpoint,
        source_model,
        source_auth_mode,
        source_transport,
        source_continuation_scope,
    )
    native_state_compatible = provenance.is_compatible_with(
        target_provider=target_provider,
        target_endpoint=target_endpoint,
        target_model=target_model,
        target_auth_mode=target_auth_mode,
        target_transport=target_transport,
        target_continuation_scope=target_continuation_scope,
        continuation_compatible=continuation_compatible,
    )
    items = [copy.deepcopy(dict(item)) for item in history]
    diagnostics: list[HistoryDiagnostic] = []

    if not native_state_compatible:
        result = _normalize_cross_provider(items, diagnostics)
    else:
        _validate_same_provider(items, target_provider)
        result = NormalizationResult(items, diagnostics)

    if logger:
        for diagnostic in result.diagnostics:
            logger(diagnostic.render(target_provider, source_provider))
    return result


def normalize_persisted_history(
    persisted: PersistedResponseHistory,
    *,
    target_provider: str,
    target_endpoint: str | None = None,
    target_model: str | None = None,
    target_auth_mode: str | None = None,
    target_transport: str | None = None,
    target_continuation_scope: str | None = None,
    continuation_compatible: bool = False,
    logger: DiagnosticLogger | None = None,
) -> NormalizationResult:
    """Normalize a persisted history while keeping provenance out of payload."""

    provenance = persisted.provenance or ProviderProvenance(None)
    return normalize_response_history(
        persisted.items,
        source_provider=provenance.source_provider,
        target_provider=target_provider,
        source_endpoint=provenance.source_endpoint,
        source_model=provenance.source_model,
        source_auth_mode=provenance.source_auth_mode,
        source_transport=provenance.source_transport,
        source_continuation_scope=provenance.source_continuation_scope,
        target_endpoint=target_endpoint,
        target_model=target_model,
        target_auth_mode=target_auth_mode,
        target_transport=target_transport,
        target_continuation_scope=target_continuation_scope,
        continuation_compatible=continuation_compatible,
        logger=logger,
    )


def _normalize_cross_provider(
    items: list[JsonObject], diagnostics: list[HistoryDiagnostic]
) -> NormalizationResult:
    output: list[JsonObject] = []
    tool_types = TOOL_CALL_TYPES | TOOL_RESULT_TYPES

    for item in items:
        item_type = _item_type(item)
        item_id = _item_id(item)

        if item_type in TOOL_CALL_TYPES or item_type in TOOL_RESULT_TYPES:
            diagnostics.append(
                HistoryDiagnostic(
                    "removed",
                    item_type,
                    "cross-provider tool state is not replayed",
                    item_id or _tool_reference(item),
                )
            )
            continue

        if item_type in REASONING_TYPES or item_type in OPAQUE_STATE_TYPES:
            summary_text = _reasoning_summary_text(item)
            if summary_text:
                output.append(_portable_summary_message(summary_text))
                diagnostics.append(
                    HistoryDiagnostic(
                        "converted",
                        item_type,
                        "portable reasoning summary retained; opaque state removed",
                        item_id,
                    )
                )
            else:
                diagnostics.append(
                    HistoryDiagnostic(
                        "removed",
                        item_type,
                        "cross-provider opaque continuation state",
                        item_id,
                    )
                )
            continue

        if _contains_opaque_key(item):
            diagnostics.append(
                HistoryDiagnostic(
                    "removed",
                    item_type,
                    "opaque provider continuation state",
                    item_id,
                )
            )
            continue

        if item_type not in KNOWN_PORTABLE_TYPES:
            diagnostics.append(
                HistoryDiagnostic(
                    "removed",
                    item_type,
                    "unsupported or unknown item type across providers",
                    item_id,
                )
            )
            continue

        if item_id:
            item.pop("id", None)
            diagnostics.append(
                HistoryDiagnostic(
                    "stripped-id",
                    item_type,
                    "provider-generated identifier is not portable",
                    item_id,
                )
            )
        output.append(item)

    # Keep this assertion close to the transformation so future additions do
    # not accidentally reintroduce dangling tool state.
    if any(_item_type(item) in tool_types for item in output):
        raise HistoryNormalizationError("cross-provider output contains tool state")
    return NormalizationResult(output, diagnostics)


def _validate_same_provider(items: list[JsonObject], target_provider: str) -> None:
    calls: set[str] = set()
    for item in items:
        item_type = _item_type(item)
        if item_type in TOOL_CALL_TYPES:
            call_id = _tool_id(item)
            if not call_id:
                raise HistoryNormalizationError(
                    f"same-provider {item_type} is missing call_id/id"
                )
            if call_id in calls:
                raise HistoryNormalizationError(
                    f"same-provider {item_type} repeats call_id/id: {_redact_id(call_id)}"
                )
            calls.add(call_id)
        elif item_type in TOOL_RESULT_TYPES:
            reference = _tool_reference(item)
            if not reference:
                raise HistoryNormalizationError(
                    f"same-provider {item_type} is missing call_id"
                )
            if reference not in calls:
                raise HistoryNormalizationError(
                    "same-provider tool result has no preceding matching tool call: "
                    + _redact_id(reference)
                )

        item_id = _item_id(item)
        if target_provider == "openai" and item_id:
            expected_prefix = OPENAI_ITEM_ID_PREFIXES.get(item_type)
            if expected_prefix and (
                not item_id.startswith(expected_prefix)
                or len(item_id) <= len(expected_prefix)
                or re.search(r"\s", item_id)
            ):
                raise HistoryNormalizationError(
                    f"same-provider {item_type} id has an invalid OpenAI namespace: "
                    f"{_redact_id(item_id)}"
                )


def _item_type(item: Mapping[str, Any]) -> str:
    value = item.get("type")
    return value if isinstance(value, str) and value else "unknown"


def _item_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("id")
    return value if isinstance(value, str) and value else None


def _tool_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("call_id") or item.get("id")
    return value if isinstance(value, str) and value else None


def _tool_reference(item: Mapping[str, Any]) -> str | None:
    value = item.get("call_id") or item.get("tool_call_id")
    return value if isinstance(value, str) and value else None


def _contains_opaque_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(key in OPAQUE_KEYS or _contains_opaque_key(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_opaque_key(child) for child in value)
    return False


def _reasoning_summary_text(item: Mapping[str, Any]) -> str:
    for key in ("summary", "text", "content"):
        summary = item.get(key)
        text = _text_from_value(summary)
        if text:
            return text
    return ""


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        return value["text"].strip()
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            parts.append(entry)
        elif isinstance(entry, Mapping) and isinstance(entry.get("text"), str):
            parts.append(entry["text"])
    return "\n".join(part.strip() for part in parts if part.strip())


def _portable_summary_message(text: str) -> JsonObject:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _redact_id(value: str | None) -> str:
    if not value:
        return ""
    return value[:12] + ("…" if len(value) > 12 else "")
