"""Defender gateway: route, admit, authorise and budget without I/O.

Command/Decision/GatewayContext retain the lab's public interface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


from kit.mcp.specs import A2A_PEERS, TOOL_SPECS
from kit.mcp.types import canonicalise_fields
from kit.mcp.a2a import verify_card, verify_delegation, parse_traceparent


def _learner_identity(value: str) -> str:
    """Only normalise the namespace, preserving the learner identifier."""
    namespace, separator, slug = value.partition(":")
    return "learner:" + slug if separator and namespace.lower() == "learner" else value


class Gateway:
    """Pure, synchronous control plane; one instance per duel.

    Cards and results enter through trusted arena callbacks, never through
    tool arguments. An A2A call carries a signed delegation-token header.
    No identity, scope, token or provenance is invented by the defender.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        self._denied_cmd_ids: set[str] = set()
        self._cards: dict[str, tuple[str, ...]] = {}
        self._seen_tokens: set[str] = set()

    def note_card(self, server: str, card: Mapping[str, Any]) -> bool:
        """Trusted registry callback. Verify raw signed cards cryptographically.

        The spar harness supplies a registry-verified summary instead of a
        signed card. Such summaries are accepted ONLY through this callback,
        not from command arguments or headers.
        """
        self._cards.pop(server, None)
        if server not in A2A_PEERS:
            return False
        if card.get("verified") is True and "signature" not in card:
            skills = card.get("skills", ())
            if not isinstance(skills, (list, tuple)) or not all(isinstance(s, str) for s in skills):
                return False
            self._cards[server] = tuple(skills)
            return True
        result = verify_card(card)
        if not result.admitted or result.peer != server:
            return False
        self._cards[server] = result.declared_skills
        return True

    def decide(self, cmd: Command) -> Decision:
        self._telemetry.decision_seen(cmd)
        # Canonicalised commands are trusted structurally; their contents are not.
        try:
            decision = self._decide(cmd)
        except (ValueError, TypeError, KeyError, AttributeError):
            decision = Decision(verdict="deny", reason="malformed tool arguments or headers")
        if decision.verdict == "deny":
            self._denied_cmd_ids.add(cmd.cmd_id)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _decide(self, cmd: Command) -> Decision:
        spec = TOOL_SPECS.get((cmd.server, cmd.tool))
        if spec is None:
            return self.deny(cmd, "unknown server or tool")
        if (cmd.kind == "a2a") != (cmd.server in A2A_PEERS):
            return self.deny(cmd, "command kind does not match the server identity")
        headers: dict[str, Any] = {}
        for key, value in cmd.headers.items():
            name = key.lower()
            if name in headers and headers[name] != value:
                return self.deny(cmd, "conflicting header names")
            headers[name] = value
        # Routing comes from the header. Body routing is ambiguous even if
        # the body claims the same replica as the header.
        if any(key in cmd.args for key in ("route", "replica", "mcp-replica")):
            return self.deny(cmd, "replica routing must be in the header")
        if "mcp-replica" in headers and headers["mcp-replica"] not in ("w", "c"):
            return self.deny(cmd, "invalid replica")
        if headers.get("x-server-fingerprint") == "unvouched":
            return self.deny(cmd, "server identity is not vouched for")
        if headers.get("x-card-signature") == "invalid" or cmd.args.get("peer_unverified"):
            return self.deny(cmd, "peer identity or answer verification failed")
        if "wiki.read" not in self.ctx.scopes:
            return self.deny(cmd, "missing wiki.read scope")
        act = _learner_identity(self.ctx.act)
        for key in ("act", "learner", "target_learner"):
            if key in cmd.args and _learner_identity(cmd.args[key]) != act:
                return self.deny(cmd, "target does not belong to ctx.act")
        anchor = cmd.args.get("anchor")
        if isinstance(anchor, str) and anchor.lower().startswith("learner:"):
            if _learner_identity(anchor) != act:
                return self.deny(cmd, "cross-learner access")
        if cmd.server == "progress" or cmd.server == "roster":
            target = cmd.args.get("anchor") if spec.is_write else cmd.args.get("learner")
            target = target or cmd.args.get("learner")
            if not isinstance(target, str) or _learner_identity(target) != act:
                return self.deny(cmd, "learner target is required and must belong to ctx.act")
        if spec.is_write:
            if "wiki.write:" + cmd.server not in self.ctx.scopes:
                return self.deny(cmd, "missing write scope for this server")
            if not all(isinstance(headers.get(h), str) and headers[h].strip() for h in spec.required_headers):
                return self.deny(cmd, "write requires If-Match and Idempotency-Key")
        if spec.needs_lease and (not cmd.lease_id or cmd.lease_id not in self.ctx.leases):
            return self.deny(cmd, "get_frame requires a live lease")
        token_id = None
        if cmd.kind == "a2a":
            skills = self._cards.get(cmd.server, ())
            declared = cmd.tool in skills or (cmd.server == "roster" and any(s in skills for s in ("role_of", "who_enrolled")))
            if not declared:
                return self.deny(cmd, "peer card or skill not admitted by the registry")
            if headers.get("aud") not in (cmd.server, "a2a:" + cmd.server):
                return self.deny(cmd, "delegation audience does not match this peer")
            token = headers.get("delegation-token")
            if not isinstance(token, Mapping):
                return self.deny(cmd, "A2A requires a registry-signed delegation token")
            result = verify_delegation(token, aud="a2a:" + cmd.server,
                                       call_index=cmd.call_index, expected_act=self.ctx.act,
                                       seen_token_ids=self._seen_tokens)
            if not result.admitted or token.get("minted_at_call_index", 0) > cmd.call_index:
                return self.deny(cmd, "invalid, expired, replayed or escalated delegation")
            if "traceparent" in headers:
                parse_traceparent(headers["traceparent"])
            token_id = token["token_id"]
        fields = canonicalise_fields(cmd.fields)
        if "*" in fields and fields != ("*",):
            return self.deny(cmd, "wildcard must be the entire field mask")
        if fields != ("*",) and any(f not in spec.all_fields for f in fields):
            return self.deny(cmd, "unknown response field")
        call = ToolCall(server=cmd.server, tool=cmd.tool, args=dict(cmd.args),
                        fields=fields, headers=headers, lease_id=cmd.lease_id,
                        call_index=cmd.call_index)
        if token_id:
            self._seen_tokens.add(token_id)
        return Decision(verdict="rewrite" if headers != cmd.headers or fields != cmd.fields else "forward", call=call)

    def deny(self, cmd: Command, reason: str) -> Decision:
        return Decision(verdict="deny", reason=reason)
