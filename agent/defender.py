"""Exchange evidence and a guarded Environment adapter for kit.loop.Agent.

The trusted executor must enforce and meter ToolCall dictionaries and update
the live GatewayContext. This adapter never executes network or file I/O.
It guards the final answer before the loop publishes action['args'].
"""
from __future__ import annotations
import json
from collections.abc import Mapping
from typing import Any
from agent.gateway import Command, Gateway
from agent.guardrails import check_grounding, scan_for_injected_instructions, redact, verify_arithmetic
from kit.mcp.types import ToolResult
from kit.mcp.specs import TOOL_SPECS
from kit.world.anchor import Anchor, AnchorSyntaxError


class AnswerEvidence:
    def __init__(self, ask: Mapping[str, Any] | None = None):
        self.ask = dict(ask or {})
        self._sources: dict[str, list[dict]] = {}
        self._private: set[str] = set()
        self._partials: set[tuple[str, str, str]] = set()
        self._unverified: set[str] = set()

    def record(self, call, result: ToolResult | Mapping[str, Any]) -> None:
        result = result if isinstance(result, ToolResult) else ToolResult.from_dict(result)
        key = (call.server, call.tool, json.dumps({k: v for k, v in call.args.items() if k not in ("cursor", "limit")}, sort_keys=True))
        if not result.ok:
            return
        if result.partial:
            self._partials.add(key)
        else:
            self._partials.discard(key)
        for row_index, row in enumerate(result.rows):
            meta = row.get("meta", {})
            meta = meta if isinstance(meta, Mapping) else {}
            private = meta.get("private") is True or row.get("private") is True
            fields = meta.get("private_fields", row.get("private_fields", {}))
            if isinstance(fields, Mapping):
                self._private.update(str(value) for value in fields.values() if value is not None)
            if private:
                self._private.update(value for value in row.values() if isinstance(value, str))
            content = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)
            unsafe = private or scan_for_injected_instructions(content).suspicious
            if unsafe:
                continue
            row_anchor = row.get("anchor")
            if isinstance(row_anchor, str) and row_anchor in result.anchors:
                anchors = [row_anchor]
            elif len(result.rows) == len(result.anchors):
                anchors = [result.anchors[row_index]]
            elif TOOL_SPECS[(call.server, call.tool)].is_write and call.args.get("anchor") in result.anchors:
                anchors = [call.args["anchor"]]
            elif len(result.anchors) == 1:
                anchors = list(result.anchors)
            else:
                anchors = []  # ambiguous association cannot support a citation
            # A coarse peer may be signed yet return unverified facts. Do not
            # use those anchors until the same anchor is independently read.
            for anchor in anchors:
                if call.server == "curriculum-analyst":
                    if anchor not in self._sources:
                        self._unverified.add(anchor)
                    continue
                if call.server in ("registry", "citation-checker"):
                    continue  # provenance/URL checks do not supply factual bodies
                self._unverified.discard(anchor)
                self._sources.setdefault(anchor, []).append(dict(row))

    def prepare_answer(self, answer: Mapping[str, Any]) -> dict:
        answer = dict(answer)
        issues = []
        grounding = check_grounding(answer, self._sources)
        if not grounding.grounded:
            issues.append("citations are missing, malformed or not backed by safe exchange evidence")
        text = answer.get("text", "")
        if not isinstance(text, str) or not text.strip():
            issues.append("answer text is missing")
            text = ""
        if scan_for_injected_instructions(text).suspicious:
            issues.append("answer contains an embedded instruction")
        # Scan all outgoing fields as private information may be in JSON facts.
        # Citation anchors are public references. Other fields (including
        # nested values) still undergo private-value and PII checking.
        public_refs = {"cited_anchors", "anchor", "anchors", "w_anchor", "c_anchor", "a", "b", "talk"}
        serialized = json.dumps({k: v for k, v in answer.items() if k not in public_refs}, ensure_ascii=False, sort_keys=True, default=str)
        private_serialized = json.dumps(answer, ensure_ascii=False, sort_keys=True, default=str)
        if any(value and value in private_serialized for value in self._private):
            issues.append("answer contains private information")
        if scan_for_injected_instructions(serialized).suspicious:
            issues.append("answer contains an embedded instruction")
        if redact(serialized, private_values=self._private).hits:
            issues.append("answer contains private information")
        cited = [a for a in grounding.cited if isinstance(a, str) and a in self._sources]
        rows = [row for anchor in cited for row in self._sources[anchor]]
        # Use row values, excluding anchor/revision IDs, as numeric evidence.
        source_texts = [str(v) for row in rows for k, v in row.items() if k not in ("anchor", "etag", "rev", "meta", "links")]
        arithmetic = verify_arithmetic(text, source_texts=source_texts)
        if arithmetic.ok is False:
            issues.append(arithmetic.detail)
        required = self.ask.get("require", ())
        if not isinstance(required, (list, tuple)):
            required = ()
        for field in required:
            value = answer.get(field)
            if value is None or value == "":
                issues.append("missing required field: " + str(field))
            elif field in ("anchor", "w_anchor", "c_anchor", "a", "b", "talk") and value not in cited:
                issues.append("required anchor is not cited")
            elif field in ("w_anchor", "c_anchor") and (not isinstance(value, str) or Anchor.parse(value).rev != field[0]):
                issues.append("required edition anchor has the wrong replica")
            elif field == "anchors" and (not isinstance(value, list) or not all(a in cited for a in value)):
                issues.append("required anchors are not cited")
            elif field not in public_refs and not any(row.get(field) == value or
                    (field == "definition" and row.get("body") == value) or
                    (isinstance(row.get("meta"), Mapping) and row["meta"].get(field) == value) for row in rows):
                issues.append("required field is not supported by cited rows: " + str(field))
        if self._partials:
            issues.append("partial result has not been completed")
        # Detect conflicting editions of the same frame, or conflicting
        # structured values for the same required fact.
        logical_bodies: dict[tuple, set[str]] = {}
        for anchor, source_rows in self._sources.items():
            try:
                parsed = Anchor.parse(anchor)
            except AnchorSyntaxError:
                continue
            key = (parsed.ns, parsed.slug, parsed.idx)
            for row in source_rows:
                if isinstance(row.get("body"), str):
                    logical_bodies.setdefault(key, set()).add(row["body"])
        conflicts = any(len(bodies) > 1 for bodies in logical_bodies.values())
        for field in required:
            if field != "anchor":
                values = {json.dumps(row[field], sort_keys=True) for sources in self._sources.values() for row in sources if field in row}
                conflicts = conflicts or len(values) > 1
        if conflicts and not any(word in text.lower() for word in ("conflict", "disagree", "mâu thuẫn", "bất đồng")):
            issues.append("sources disagree without an explicit conflict statement")
        if issues:
            return {"text": "Không đủ bằng chứng an toàn để trả lời phần được hỏi.",
                    "cited_anchors": [], "abstained": True, "defender_issues": list(dict.fromkeys(issues))}
        return answer


class DefenderEnvironment:
    """Use as env= in kit.loop.Agent. Answers and calls pass the same boundary.

    executor.execute receives a canonical structured action. Only this trusted
    boundary may call record_result/note_card or update the live context.
    A new adapter/evidence object is constructed for each exchange; Gateway
    itself persists across the duel.
    """
    def __init__(self, gateway: Gateway, executor, *, ask: Mapping[str, Any] | None = None):
        self.gateway, self.executor = gateway, executor
        self.evidence = AnswerEvidence(ask)
        self._sequence = 0

    def execute(self, action: dict) -> dict:
        if action["kind"] == "answer":
            safe = self.evidence.prepare_answer(action["args"])
            # kit.loop.Agent reads this same args object after execute returns.
            action["args"] = safe
            action["raw"] = "ANSWER " + json.dumps(safe, ensure_ascii=False, sort_keys=True)
            return dict(self.executor.execute(action))
        cmd = Command.from_action_dict(action, cmd_id=f"defender:{self.gateway.ctx.round}:{self._sequence}")
        self._sequence += 1
        decision = self.gateway.decide(cmd)
        if decision.verdict == "deny":
            return {"ok": False, "error": {"code": "unauthorized", "reason": decision.reason}, "cost": 0}
        call = decision.call
        executed = {**call.to_dict(), "kind": action["kind"], "raw": action["raw"]}
        result = dict(self.executor.execute(executed))
        try:
            self.gateway.record_result(cmd, result)
            self.evidence.record(call, result)
        except (ValueError, TypeError, KeyError, AttributeError):
            return {"ok": False, "error": {"code": "unavailable"}, "cost": result.get("cost", 0)}
        return result
