from __future__ import annotations

import json
import unittest

from agent_framework.registry.registry import FrameworkRegistry

# Standard pydantic Optional[T] emission: anyOf with a null member.
_OPT_OBJECT = {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}], "default": None}
_OPT_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}


class CoerceArgumentsTests(unittest.TestCase):
    def test_stringified_object_parsed_against_anyof_schema(self) -> None:
        schema = {"type": "object", "properties": {"filters": _OPT_OBJECT}, "required": ["entity_type"]}
        args = {"entity_type": "Product", "filters": json.dumps({"id": 25})}
        result = FrameworkRegistry._coerce_arguments_to_schema(args, schema)
        self.assertEqual(result["filters"], {"id": 25})
        self.assertEqual(result["entity_type"], "Product")

    def test_already_object_left_unchanged(self) -> None:
        schema = {"type": "object", "properties": {"filters": _OPT_OBJECT}}
        args = {"filters": {"id": 25}}
        self.assertEqual(FrameworkRegistry._coerce_arguments_to_schema(args, schema), args)

    def test_optional_string_scalar_not_coerced(self) -> None:
        # "25" parses as JSON but the (narrowed) schema says string -> stay a string.
        schema = {"type": "object", "properties": {"keyword": _OPT_STRING}}
        args = {"keyword": "25"}
        self.assertEqual(FrameworkRegistry._coerce_arguments_to_schema(args, schema), args)

    def test_stringified_optional_array_of_objects_recurses(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "related_requirements": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "object", "properties": {"filters": _OPT_OBJECT}},
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                }
            },
        }
        # Model stringified the whole array AND each inner filters object.
        array_text = json.dumps([{"filters": json.dumps({"id": 1})}])
        args = {"related_requirements": array_text}
        result = FrameworkRegistry._coerce_arguments_to_schema(args, schema)
        self.assertEqual(result["related_requirements"], [{"filters": {"id": 1}}])

    def test_already_list_of_objects_recurses_into_items(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "related_requirements": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "object", "properties": {"filters": _OPT_OBJECT}},
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                }
            },
        }
        args = {"related_requirements": [{"filters": json.dumps({"id": 1})}]}
        result = FrameworkRegistry._coerce_arguments_to_schema(args, schema)
        self.assertEqual(result["related_requirements"], [{"filters": {"id": 1}}])

    def test_unparseable_string_left_unchanged(self) -> None:
        schema = {"type": "object", "properties": {"filters": _OPT_OBJECT}}
        args = {"filters": "not json{"}
        self.assertEqual(FrameworkRegistry._coerce_arguments_to_schema(args, schema), args)

    def test_empty_object_string_parses_to_empty_dict(self) -> None:
        # Guard against a falsy bug: "{}" must become {}, not be discarded.
        schema = {"type": "object", "properties": {"filters": _OPT_OBJECT}}
        args = {"filters": "{}"}
        self.assertEqual(FrameworkRegistry._coerce_arguments_to_schema(args, schema), {"filters": {}})

    def test_no_properties_returns_arguments_unchanged(self) -> None:
        args = {"a": 1}
        self.assertEqual(FrameworkRegistry._coerce_arguments_to_schema(args, {"type": "object"}), args)

    def test_does_not_mutate_input(self) -> None:
        schema = {"type": "object", "properties": {"filters": _OPT_OBJECT}}
        original = {"filters": json.dumps({"id": 25})}
        args = {"filters": json.dumps({"id": 25})}
        FrameworkRegistry._coerce_arguments_to_schema(args, schema)
        self.assertEqual(args, original)


if __name__ == "__main__":
    unittest.main()
