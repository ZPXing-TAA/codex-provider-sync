import unittest

from codex_provider_sync.history import (
    HistoryNormalizationError,
    PersistedResponseHistory,
    ProviderProvenance,
    normalize_persisted_history,
    normalize_response_history,
)


class HistoryNormalizerTest(unittest.TestCase):
    def test_same_provider_preserves_valid_reasoning_id(self):
        history = [{"type": "reasoning", "id": "rs_123", "encrypted_content": "opaque"}]

        result = normalize_response_history(
            history,
            source_provider="openai",
            target_provider="openai",
            continuation_compatible=True,
        )

        self.assertEqual(result.items, history)
        self.assertEqual(result.diagnostics, [])

    def test_cross_provider_drops_opaque_reasoning_and_keeps_summary(self):
        history = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "item_abc", "encrypted_content": "secret", "summary": [{"text": "Plan"}]},
            {"type": "message", "role": "assistant", "id": "item_visible", "content": [{"type": "output_text", "text": "hello"}]},
        ]

        result = normalize_response_history(
            history, source_provider="provider_x", target_provider="openai"
        )

        self.assertEqual([item["type"] for item in result.items], ["message", "message", "message"])
        self.assertNotIn("id", result.items[0])
        self.assertNotIn("id", result.items[2])
        self.assertEqual(result.items[1]["content"][0]["text"], "Plan")
        self.assertTrue(any(d.action == "converted" and d.item_type == "reasoning" for d in result.diagnostics))

    def test_cross_provider_drops_foreign_message_id_without_prefix_translation(self):
        history = [{"type": "message", "role": "assistant", "id": "item_foreign", "content": []}]

        result = normalize_response_history(
            history, source_provider="provider_x", target_provider="openai"
        )

        self.assertEqual(result.items, [{"type": "message", "role": "assistant", "content": []}])
        self.assertFalse(any(value.startswith("rs_") for value in result.items[0].values() if isinstance(value, str)))

    def test_cross_provider_drops_tool_pairs_together(self):
        history = [
            {"type": "message", "role": "user", "content": []},
            {"type": "function_call", "id": "item_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
            {"type": "message", "role": "assistant", "content": []},
        ]

        result = normalize_response_history(
            history, source_provider="provider_x", target_provider="openai"
        )

        self.assertEqual([item["type"] for item in result.items], ["message", "message"])

    def test_cross_provider_mixed_conversation_keeps_portable_context(self):
        history = [
            {
                "type": "message",
                "role": "user",
                "id": "item_user",
                "content": [{"type": "input_text", "text": "question"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "id": "item_before",
                "content": [{"type": "output_text", "text": "thinking"}],
            },
            {"type": "reasoning", "id": "item_reasoning", "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "id": "item_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
            {
                "type": "message",
                "role": "assistant",
                "id": "item_after",
                "content": [{"type": "output_text", "text": "answer"}],
            },
        ]

        result = normalize_response_history(
            history, source_provider="provider_x", target_provider="openai"
        )

        self.assertEqual(
            [item["content"][0]["text"] for item in result.items],
            ["question", "thinking", "answer"],
        )
        self.assertFalse(
            any(
                item["type"] in {"reasoning", "function_call", "function_call_output"}
                for item in result.items
            )
        )

    def test_cross_provider_drops_unknown_item_without_identifier(self):
        result = normalize_response_history(
            [{"type": "provider_native_state", "state": "opaque"}],
            source_provider="provider_x",
            target_provider="openai",
        )

        self.assertEqual(result.items, [])

    def test_cross_provider_converts_textual_summary_state(self):
        result = normalize_response_history(
            [{"type": "summary", "id": "item_summary", "text": "keep this context"}],
            source_provider="provider_x",
            target_provider="openai",
        )

        self.assertEqual(
            result.items,
            [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "keep this context"}],
            }],
        )

    def test_same_provider_rejects_dangling_tool_result(self):
        with self.assertRaises(HistoryNormalizationError):
            normalize_response_history(
                [{"type": "function_call_output", "call_id": "call_missing", "output": "x"}],
                source_provider="openai",
                target_provider="openai",
                continuation_compatible=True,
            )

    def test_same_provider_rejects_wrong_openai_reasoning_namespace(self):
        with self.assertRaises(HistoryNormalizationError):
            normalize_response_history(
                [{"type": "reasoning", "id": "item_foreign"}],
                source_provider="openai",
                target_provider="openai",
                continuation_compatible=True,
            )

    def test_same_provider_rejects_wrong_openai_message_namespace(self):
        with self.assertRaises(HistoryNormalizationError):
            normalize_response_history(
                [{"type": "message", "role": "assistant", "id": "item_foreign", "content": []}],
                source_provider="openai",
                target_provider="openai",
                continuation_compatible=True,
            )

    def test_same_provider_name_without_continuation_proof_is_sanitized(self):
        result = normalize_response_history(
            [{"type": "reasoning", "id": "rs_123", "encrypted_content": "opaque"}],
            source_provider="openai",
            target_provider="openai",
        )

        self.assertEqual(result.items, [])

    def test_same_provider_name_with_different_route_is_sanitized(self):
        result = normalize_response_history(
            [{"type": "message", "role": "assistant", "id": "msg_123", "content": []}],
            source_provider="openai",
            target_provider="openai",
            source_endpoint="https://chatgpt.com/backend-api/codex",
            target_endpoint="https://api.openai.com/v1",
            source_transport="websocket",
            target_transport="http",
            continuation_compatible=True,
        )

        self.assertEqual(result.items, [{"type": "message", "role": "assistant", "content": []}])

    def test_cross_provider_strips_standard_looking_ids(self):
        result = normalize_response_history(
            [{"type": "message", "role": "assistant", "id": "msg_looks_valid", "content": []}],
            source_provider="custom",
            target_provider="openai",
        )

        self.assertEqual(result.items, [{"type": "message", "role": "assistant", "content": []}])

    def test_missing_provenance_is_conservative(self):
        persisted = PersistedResponseHistory(
            items=({"type": "reasoning", "id": "item_legacy", "encrypted_content": "opaque"},
                   {"type": "message", "role": "user", "id": "item_user", "content": []}),
            provenance=None,
        )

        result = normalize_persisted_history(persisted, target_provider="openai")

        self.assertEqual(result.items, [{"type": "message", "role": "user", "content": []}])

    def test_provenance_is_not_added_to_request_items(self):
        persisted = PersistedResponseHistory(
            items=({"type": "message", "role": "user", "content": []},),
            provenance=ProviderProvenance(
                source_provider="provider_x",
                source_endpoint="https://source.invalid/v1",
                source_model="source-model",
            ),
        )

        result = normalize_persisted_history(persisted, target_provider="openai")

        self.assertEqual(result.items, [{"type": "message", "role": "user", "content": []}])
        self.assertNotIn("source_provider", result.items[0])
        self.assertNotIn("source_endpoint", result.items[0])
        self.assertNotIn("source_model", result.items[0])

    def test_repeated_switches_do_not_accumulate_stale_ids(self):
        history = [{"type": "message", "role": "assistant", "id": "item_a", "content": []}]
        first = normalize_response_history(history, source_provider="openai", target_provider="provider_a")
        second = normalize_response_history(first.items, source_provider="provider_a", target_provider="provider_b")
        third = normalize_response_history(second.items, source_provider="provider_b", target_provider="openai")

        self.assertEqual(third.items, [{"type": "message", "role": "assistant", "content": []}])

    def test_diagnostic_logger_redacts_ids(self):
        logs = []
        normalize_response_history(
            [{"type": "reasoning", "id": "item_secret_identifier", "encrypted_content": "secret"}],
            source_provider="provider_x",
            target_provider="openai",
            logger=logs.append,
        )

        self.assertEqual(len(logs), 1)
        self.assertIn("item_secret_…", logs[0])
        self.assertNotIn("item_secret_identifier", logs[0])
        self.assertNotIn("encrypted_content", logs[0])


if __name__ == "__main__":
    unittest.main()
