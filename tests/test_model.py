"""Claudes kompakta svarsformat och revideringar som ändringar."""

import pytest

from form_assistant.model import AiDraft, DraftPatch, FormDraft, compact

from conftest import draft_dict

transform = pytest.importorskip("anthropic.lib._parse._transform").transform_schema


def counts(schema) -> tuple[int, int]:
    """Valfria fält och unionstyper i ett schema, så som API:et räknar dem."""
    optional = unions = 0

    def walk(node):
        nonlocal optional, unions
        if isinstance(node, dict):
            props = node.get("properties", {})
            optional += sum(1 for k in props if k not in set(node.get("required", [])))
            unions += sum(1 for v in props.values() if "anyOf" in v or isinstance(v.get("type"), list))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return optional, unions


@pytest.mark.parametrize("model", [AiDraft, DraftPatch, FormDraft])
def test_schemas_stay_within_structured_output_limits(model):
    optional, unions = counts(transform(model))
    assert optional <= 24 and unions <= 16


def test_compact_round_trip(draft):
    short = compact(draft)
    assert "read_only" not in short["items"][0] and short["items"][-1] == {
        "link_id": "info", "text": "Tack för att du fyllt i formuläret.", "type": "display"}
    assert AiDraft.model_validate(short).to_draft() == draft
    assert AiDraft.model_validate(draft_dict()).to_draft() == draft           # explicita null går också


def patch(**kw):
    return DraftPatch.model_validate({"header": [], "remove": [], "changes": [], "notes": []} | kw)


def add(link_id, parent=None, after="", **item):
    return {"action": "lägg_till", "after": after,
            "item": {"link_id": link_id, "text": link_id.title(), "type": "string", "parent": parent} | item}


def test_patch_places_new_items(draft):
    result, log = patch(changes=[
        add("forst-i-halsa", "halsa", after="halsa"),
        add("sist-i-halsa", "halsa"),
        add("sist", None),
        add("ny-grupp-fraga", "ny-grupp"),
    ]).apply(draft)
    assert [i.link_id for i in result.children("halsa")][0] == "forst-i-halsa"
    assert [i.link_id for i in result.children("halsa")][-1] == "sist-i-halsa"
    assert result.children(None)[-2].link_id == "sist"
    assert result.item("ny-grupp-fraga").parent is None and "gruppen ny-grupp finns inte" in log[0]


def test_patch_removes_groups_and_dangling_conditions(draft):
    result, log = patch(remove=["patient", "allergi", "finns-inte"], notes=["Ny anteckning"],
                        header=[{"field": "description", "value": ""}, {"field": "title", "value": "Ny titel"}]
                        ).apply(draft)
    assert result.item("personnummer") is None and result.item("patient") is None   # gruppens frågor följer med
    assert result.item("allergi-vad").enable_when == [] and result.item("allergi-vad").enable_behavior is None
    assert result.title == "Ny titel" and result.description is None and result.notes == ["Ny anteckning"]
    assert any("finns-inte" in m for m in log) and any("villkor" in m for m in log)


def test_patch_change_keeps_position_and_move_by_remove_and_add(draft):
    moved = add("feber", "halsa", after="graviditet", type="boolean", required=True, prefix="2")
    result, log = patch(remove=["feber"], changes=[
        moved, {"action": "ändra", "after": "", "item": {"link_id": "mottagning", "text": "Mottagning",
                                                          "type": "string", "parent": "vardgivare"}}]).apply(draft)
    assert [i.link_id for i in result.children("halsa")] == ["allergi", "allergi-vad", "graviditet", "feber"]
    assert result.item("mottagning").text == "Mottagning" and result.item("mottagning").semantic == "ingen"
    assert result.children("vardgivare")[0].link_id == "mottagning" and not log
    unknown, log = patch(changes=[{"action": "ändra", "after": "", "item": {
        "link_id": "ny", "text": "Ny", "type": "string"}}]).apply(draft)
    assert unknown.item("ny") is not None and "fanns inte" in log[0]
