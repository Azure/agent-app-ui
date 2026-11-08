"""Bounded native-result reader. Caller must complete existing owner admission."""

import json
import logging
from pathlib import Path
from typing import Any, NoReturn
from collections.abc import Iterator

from jsonschema import Draft202012Validator, FormatChecker

logger = logging.getLogger(__name__)
SCHEMA = Path(__file__).resolve().parents[1] / "contracts/turn-presentation-v1.schema.json"
LIMIT = 262144
KNOWN = {"cards": "Cards", "table": "Table", "chart": "Chart", "suggestions": "Suggestions"}


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def nonfinite(value: str) -> NoReturn:
    raise ValueError("nonfinite")


def known_properties(value: Any, definition: dict[str, Any], definitions: dict[str, Any]) -> Any:
    """Recursively retain only declared properties; never spread future props."""
    if "$ref" in definition:
        definition = definitions[definition["$ref"].split("/")[-1]]
    properties = dict(definition.get("properties", {}))
    for base in definition.get("allOf", []):
        if "$ref" in base:
            properties.update(definitions[base["$ref"].split("/")[-1]].get("properties", {}))
    if isinstance(value, dict) and properties:
        return {k: known_properties(v, properties[k], definitions) for k, v in value.items() if k in properties}
    if isinstance(value, dict) and isinstance(definition.get("additionalProperties"), dict):
        return {k: known_properties(v, definition["additionalProperties"], definitions) for k, v in value.items()}
    if isinstance(value, list) and isinstance(definition.get("items"), dict):
        return [known_properties(v, definition["items"], definitions) for v in value]
    return value


def read_presentation(text: Any, *, admitted: bool = False) -> dict[str, Any] | None:
    if not admitted or not isinstance(text, str) or len(text.encode("utf-8")) > LIMIT:
        return None
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get("kind") != "agent_lz_presentation" or not isinstance(value.get("markdown"), str):
        return None
    result = {"markdown": value["markdown"], "contributions": []}
    if value.get("schemaVersion") != 1:
        return result
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    header_schema = {"type": "object", "additionalProperties": False, "$defs": schema["$defs"],
                     "required": [k for k in schema["required"] if k != "contributions"],
                     "properties": {k: v for k, v in schema["properties"].items() if k != "contributions"}}
    header = {k: v for k, v in value.items() if k in header_schema["properties"]}
    if not Draft202012Validator(header_schema).is_valid(header):
        return result
    result["presentationId"] = value["presentationId"]
    contributions = value.get("contributions")
    if not isinstance(contributions, list):
        return result
    if sum(len(c.get("blocks", [])) for c in contributions if isinstance(c, dict) and isinstance(c.get("blocks"), list)) > 10:
        return result
    for contribution in contributions:
        if not isinstance(contribution, dict) or not isinstance(contribution.get("blocks"), list):
            continue
        blocks = []
        for block in contribution["blocks"]:
            if not isinstance(block, dict) or block.get("type") not in KNOWN:
                continue
            definition = schema["$defs"][KNOWN[block["type"]]]
            projected = known_properties(block, definition, schema["$defs"])
            check = Draft202012Validator({"$ref": f"#/$defs/{KNOWN[block['type']]}", "$defs": schema["$defs"]},
                                         format_checker=FormatChecker())
            if not check.is_valid(projected):
                logger.info("presentation_reader_rejected", extra={"reason": "known_shape"})
                continue
            if projected["type"] == "chart":
                chart = projected
                if (any(len(s["values"]) != len(chart["categories"]) for s in chart["series"]) or
                    len({c["sourceRowId"] for c in chart["categories"]}) != len(chart["categories"]) or
                    not any(v is not None for s in chart["series"] for v in s["values"]) or
                    sum(v is not None for s in chart["series"] for v in s["values"]) > 1000 or
                    len({s.get("unit") for s in chart["series"]}) > 1):
                    continue
            if projected["type"] == "table":
                columns = projected["columns"]
                ids = {c["id"] for c in columns}
                if len(ids) != len(columns) or any(set(r["values"]) != ids for r in projected["rows"]):
                    continue
                if len({r["id"] for r in projected["rows"]}) != len(projected["rows"]):
                    continue
                typed = Draft202012Validator({"$ref": "#/$defs/TypedValue", "$defs": schema["$defs"]},
                                             format_checker=FormatChecker())
                if any(not typed.is_valid({"type": c["type"], "value": r["values"][c["id"]]})
                       for c in columns for r in projected["rows"]):
                    continue
            blocks.append(projected)
        if blocks:
            candidate = known_properties(contribution, schema["$defs"]["Contribution"], schema["$defs"])
            candidate["blocks"] = blocks
            check = Draft202012Validator({"$ref": "#/$defs/Contribution", "$defs": schema["$defs"]})
            if not check.is_valid(candidate):
                continue
            source_ids = {s["sourceId"] for s in candidate["sourceRefs"]}
            if len(source_ids) != len(candidate["sourceRefs"]):
                continue
            # Saved visuals use native admission, not a second source ACL query.
            # Still reject structurally cross-contribution source references.
            def bindings(item: Any) -> Iterator[str]:
                if isinstance(item, dict):
                    if "sourceId" in item:
                        yield item["sourceId"]
                    for child in item.values():
                        yield from bindings(child)
                elif isinstance(item, list):
                    for child in item:
                        yield from bindings(child)
            blocks = [b for b in blocks if set(bindings(b)).issubset(source_ids)]
            if blocks:
                result["contributions"].append({
                    "agentId": candidate["agentId"], "workflowOrdinal": candidate["workflowOrdinal"],
                    "contributionId": candidate["contributionId"], "blocks": blocks,
                    "sourceRefs": [{k: s[k] for k in ("sourceId", "toolName", "resultId")}
                                   for s in candidate["sourceRefs"]],
                })
    accepted = result["contributions"]
    if (len({c["contributionId"] for c in accepted}) != len(accepted) or
        len({c["workflowOrdinal"] for c in accepted}) != len(accepted) or
        len({b["blockId"] for c in accepted for b in c["blocks"]}) != sum(len(c["blocks"]) for c in accepted)):
        result["contributions"] = []
    result["contributions"].sort(key=lambda c: c["workflowOrdinal"])
    return result


def _completed_business(text: Any) -> dict[str, Any] | None:
    """Decode one complete native result, never search flattened narration."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > LIMIT:
        return None
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError):
        return None
    if (not isinstance(value, dict) or value.get("status") not in ("approved", "rejected") or
        any(not isinstance(value.get(key), str) for key in
            ("topic", "draft", "configuration_revision", "review_comment"))):
        return None
    return value


def _presentations(value: Any) -> list[dict[str, Any]]:
    """Only assistant native messages and typed workflow result fields."""
    if isinstance(value, list):
        return [p for item in value for p in _presentations(item)]
    if not isinstance(value, dict):
        return []
    if value.get("type") in ("progress", "response.in_progress", "workflow_progress"):
        return []
    if "role" in value and value["role"] != "assistant":
        return []
    if "draft" in value and isinstance(value.get("presentation"), dict):
        if (not isinstance(value.get("topic"), str) or not isinstance(value.get("configuration_revision"), str) or
            value.get("status") not in (None, "approved", "rejected")):
            return []
        result = read_presentation(json.dumps(value["presentation"]), admitted=True)
        return [result] if result else []
    if value.get("role") == "assistant":
        if value.get("type") not in (None, "message"):
            return []
        content = value.get("content", value.get("contents", []))
        if not isinstance(content, list):
            return []
        found = []
        for item in content:
            if not isinstance(item, dict) or item.get("type", item.get("$type")) not in ("text", "output_text"):
                continue
            result = read_presentation(item.get("text"), admitted=True)
            if result is not None:
                found.append(result)
            elif (business := _completed_business(item.get("text"))) is not None:
                found.extend(_presentations(business))
        identity = value.get("id", value.get("message_id", value.get("messageId")))
        if isinstance(identity, str):
            for result in found:
                result["nativeMessageId"] = identity
        return found
    return [p for key in ("output", "messages", "response", "pending", "data", "items") for p in _presentations(value.get(key, []))]


def presentations(value: Any) -> list[dict[str, Any]]:
    """Deduplicate replayed carriers without timing-based contribution order."""
    result, seen = [], set()
    for presentation in _presentations(value):
        identity = (presentation.get("nativeMessageId"), presentation.get("presentationId"))
        if identity[1] is not None:
            if identity in seen:
                continue
            seen.add(identity)
        result.append(presentation)
    return result


def text_projection(value: Any, admitted: bool = False) -> str:
    """Shared readable REST/Chainlit projection; roles reset carrier admission."""
    if isinstance(value, str):
        presentation = read_presentation(value, admitted=admitted)
        if presentation is not None:
            return presentation["markdown"]
        if admitted and (business := _completed_business(value)) is not None:
            return text_projection(business)
        return value
    if isinstance(value, list):
        return "\n\n".join(filter(None, (text_projection(item, admitted) for item in value)))
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("draft"), str):
        text = f"{value.get('status', 'Revisão')}\n\n{value['draft']}"
        # Reuse the typed-result admission boundary, including future-version
        # markdown. A text-only reader must not lose saved source values.
        for presentation in _presentations(value):
            markdown = presentation["markdown"]
            if markdown and markdown not in text:
                text += "\n\n" + markdown
        return text
    if "role" in value:
        admitted = value["role"] == "assistant" and value.get("type") in (None, "message")
    if value.get("type", value.get("$type")) in ("text", "output_text"):
        return text_projection(value.get("text", ""), admitted)
    if isinstance(value.get("pending"), list) and value["pending"]:
        return "\n\n".join(text_projection(item.get("data")) for item in value["pending"] if isinstance(item, dict))
    for key in ("output", "messages", "response", "content", "contents", "items"):
        if key in value:
            return text_projection(value[key], admitted)
    return ""


def rest_projection(value: Any) -> Any:
    """Add optional readable projection without altering native wire ownership."""
    projected = presentations(value)
    if not projected or not isinstance(value, dict):
        return value
    return {**value, "presentation_text": text_projection(value),
            "presentations": projected}
