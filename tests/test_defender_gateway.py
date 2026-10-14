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
    assert gw.decide(cmd).call.args == cmd.args
    assert cmd.to_dict() == snapshot
    assert gw.decide(command(cmd_id="frame:1", server="slides", tool="get_frame", args={"anchor": "Frame:alpha/w/000"}, lease_id="live")).call
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
    gw = Gateway(ctx)
    provenance = command(server="registry", tool="provenance", args={"anchor": "Learner:sv-0417"})
    assert gw.decide(provenance).call
    gw.record_result(provenance, {"ok": True, "etag": "etag", "anchors": ["Learner:sv-0417"]})
    assert gw.decide(replace(cmd, cmd_id="cmd:2", call_index=1)).call
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


def test_write_uses_trusted_fresh_provenance_and_never_blindly_retries():
    gw = Gateway(context(scopes=frozenset({"wiki.read", "wiki.write:progress"})))
    read = command(server="registry", tool="provenance", args={"anchor": "Learner:sv-0417"})
    write = command(cmd_id="write:1", call_index=1, server="progress", tool="record_mastery",
                    args={"learner": "Learner:sv-0417", "concept": "Concept:mcp"},
                    headers={"If-Match": "tag", "Idempotency-Key": "key:1"})
    assert gw.decide(write).verdict == "deny"
    assert not gw.record_result(read, {"ok": True, "etag": "tag", "anchors": ["Learner:sv-0417"]})
    assert gw.decide(read).call
    assert gw.record_result(read, {"ok": True, "etag": "tag", "anchors": ["Learner:sv-0417"]})
    assert gw.decide(replace(write, call_index=2)).verdict == "deny"
    accepted = gw.decide(write)
    assert accepted.call.args["anchor"] == "Learner:sv-0417"
    gw.record_result(write, {"ok": False, "error": {"code": "unavailable"}, "cost": 4})
    assert gw.decide(replace(write, cmd_id="write:2", headers={"if-match": "tag", "idempotency-key": "key:2"})).verdict == "deny"
    read2 = replace(read, cmd_id="read:2", call_index=2)
    assert gw.decide(read2).call
    gw.record_result(read2, {"ok": True, "etag": "new", "anchors": ["Learner:sv-0417"]})
    retry = replace(write, cmd_id="write:3", call_index=3, headers={"if-match": "new", "idempotency-key": "key:1"})
    assert gw.decide(retry).verdict == "deny"
    assert gw.decide(replace(retry, headers={"if-match": "new", "idempotency-key": "key:2"})).call


def test_mask_migration_catalog_and_query_cost_are_bounded():
    gw = Gateway(context())
    query = command(server="slides", tool="search", args={"q": "MCP", "limit": 1000}, fields=("anchor", "snippet"))
    call = gw.decide(query).call
    assert call.tool == "query" and call.fields == ("body",) and call.args["limit"] == 3
    catalog = command(cmd_id="catalog", server="registry", tool="list_servers", args={}, fields=("*",))
    assert gw.decide(catalog).call.fields == ("name",)
    terms = command(cmd_id="terms", server="glossary", tool="list_terms", args={})
    assert gw.decide(terms).call.fields == ("term",)
    assert Gateway(context(credits=3, round=10)).decide(command(server="slides", tool="get_frame", lease_id="live")).verdict == "deny"
    ctx = context(credits=1, round=10)
    affordable = Gateway(ctx)
    assert affordable.decide(command()).call
    assert affordable.decide(command(cmd_id="second")).verdict == "deny"


def test_partial_cursor_is_bound_to_query_and_round():
    ctx = context()
    gw = Gateway(ctx)
    query = command(server="slides", tool="query", args={"q": "MCP"})
    assert gw.decide(query).call
    gw.record_result(query, {"ok": True, "partial": True, "continuation": "3", "lease_id": "live"})
    continuation = replace(query, cmd_id="query:2", call_index=1, args={"q": "MCP", "cursor": "3"})
    assert gw.decide(replace(continuation, args={"q": "other", "cursor": "3"})).verdict == "deny"
    assert gw.decide(continuation).call
    ctx.leases = ("live",)
    frame = command(cmd_id="frame", server="slides", tool="get_frame", lease_id="live", call_index=3,
                    args={"anchor": "Frame:alpha/w/000"}, headers={"Mcp-Replica": "c"})
    assert gw.decide(frame).call.headers["mcp-replica"] == "w"
    assert gw.decide(replace(frame, call_index=4)).verdict == "deny"
    ctx.round = 2
    assert gw.decide(replace(frame, call_index=1)).verdict == "deny"
    assert gw.decide(replace(continuation, cmd_id="query:3")).verdict == "deny"


def test_rate_limit_is_per_tool_and_resets_after_three_rounds():
    ctx = context()
    gw = Gateway(ctx)
    gw.note_card("citation-checker", AGENT_CARDS["citation-checker"].to_dict())
    def citation(index):
        token = mint_delegation(act=ctx.act, sub=ctx.sub, aud="a2a:citation-checker", ttl=3, call_index=index)
        return command(cmd_id=f"cite:{index}", call_index=index, kind="a2a", server="citation-checker", tool="verify_source",
                       args={"anchor": "Source:one"}, headers={"aud": "citation-checker", "delegation-token": token.to_dict()})
    assert gw.decide(citation(0)).call
    ctx.round = 2
    assert gw.decide(citation(1)).call
    ctx.round = 3
    assert gw.decide(citation(2)).verdict == "deny"
    ctx.round = 4
    assert gw.decide(citation(3)).call
