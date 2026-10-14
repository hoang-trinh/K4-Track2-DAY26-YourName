"""Behavioural security regressions for the defender; no duel scoring."""
from dataclasses import replace
import pytest
from agent.gateway import Command, Gateway
from agent.telemetry import RecordingGatewayContext
from kit.mcp.a2a import AGENT_CARDS, mint_delegation


def context(**kwargs):
    values = dict(act="learner:sv-0417", sub="agent:vlearn-tutor",
                  scopes=frozenset({"wiki.read"}), credits=100, round=1, call_index=0)
    values.update(kwargs)
    return RecordingGatewayContext(**values)


def command(**kwargs):
    values = dict(cmd_id="cmd:1", kind="mcp", raw="", server="glossary", tool="define",
                  args={"term": "MCP", "lang": "en"}, fields=(), headers={}, lease_id=None, call_index=0)
    values.update(kwargs)
    return Command(**values)


def peer_command(**kwargs):
    token = mint_delegation(act="learner:sv-0417", sub="agent:vlearn-tutor",
                            aud="a2a:curriculum-analyst", ttl=3, call_index=0)
    values = dict(kind="a2a", server="curriculum-analyst", tool="which_days_cover",
                  args={"concept": "Concept:mcp"},
                  headers={"aud": "a2a:curriculum-analyst", "delegation-token": token.to_dict()})
    values.update(kwargs)
    return command(**values)


def test_clean_read_and_live_lease_are_allowed_without_mutating_command():
    ctx = context(leases=("live",))
    gw = Gateway(ctx)
    cmd = command()
    snapshot = cmd.to_dict()
    assert gw.decide(cmd).verdict == "forward"
    assert cmd.to_dict() == snapshot
    assert gw.decide(command(server="slides", tool="get_frame", args={"anchor": "Frame:alpha/w/000"}, lease_id="live")).call
    assert gw.decide(command(server="slides", tool="get_frame", args={}, lease_id="expired")).verdict == "deny"


@pytest.mark.parametrize("kwargs", [
    {"args": {"learner": "Learner:sv-0392"}},
    {"args": {"anchor": "Learner:sv-0392"}},
    {"args": {"route": "c"}},
    {"headers": {"mcp-replica": "unknown"}},
    {"headers": {"Mcp-Replica": "w", "mcp-replica": "c"}},
    {"headers": {"x-server-fingerprint": "unvouched"}},
    {"server": "counterfeit"}, {"tool": "execute"},
    {"fields": ("password",)}, {"fields": ("*", "title")},
    {"args": {"learner": ["invalid"]}},
])
def test_unsafe_requests_return_free_denials(kwargs):
    decision = Gateway(context()).decide(command(**kwargs))
    assert decision.verdict == "deny" and decision.reason and decision.call is None


def test_authority_comes_from_act_and_explicit_scopes():
    cmd = command(server="progress", tool="record_mastery",
                  args={"anchor": "Learner:sv-0417", "concept": "Concept:mcp"},
                  headers={"if-match": "etag", "idempotency-key": "write:1"})
    assert Gateway(context()).decide(cmd).verdict == "deny"
    ctx = context(scopes=frozenset({"wiki.read", "wiki.write:progress"}))
    assert Gateway(ctx).decide(cmd).call
    assert Gateway(ctx).decide(replace(cmd, args={**cmd.args, "learner": "learner:sv-0392"})).verdict == "deny"
    assert Gateway(ctx).decide(replace(cmd, headers={})).verdict == "deny"
    assert Gateway(context(scopes=frozenset())).decide(command()).verdict == "deny"


def test_a2a_checks_card_skill_audience_signed_act_expiry_and_replay():
    gw = Gateway(context())
    cmd = peer_command()
    assert gw.decide(cmd).verdict == "deny"
    assert gw.note_card("curriculum-analyst", AGENT_CARDS["curriculum-analyst"].to_dict())
    assert gw.decide(replace(cmd, headers={**cmd.headers, "aud": "mcp:progress"})).verdict == "deny"
    token = dict(cmd.headers["delegation-token"], act="learner:sv-0392")
    assert gw.decide(replace(cmd, headers={**cmd.headers, "delegation-token": token})).verdict == "deny"
    assert gw.decide(replace(cmd, call_index=4)).verdict == "deny"
    assert gw.decide(cmd).call
    assert gw.decide(cmd).verdict == "deny"
    forged = AGENT_CARDS["curriculum-analyst"].to_dict()
    forged["signature"] = "forged"
    assert not gw.note_card("curriculum-analyst", forged)
    assert gw.decide(peer_command()).verdict == "deny"
