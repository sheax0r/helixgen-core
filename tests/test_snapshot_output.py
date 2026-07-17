"""First-class per-snapshot OUTPUT level/pan in the recipe + `view` surfaces
(backlog #76).

Pins the whole loop: the snapshot-level `output` recipe field (spec parse +
validation), its realization onto the b13 gain/pan `snapshots` arrays
(generate), the `view` lift back into the recipe shape, and round-trip
stability — including a phase-2-normalized `.hsp` built with the real
normalize/mutate primitives (`device normalize`'s actuator).
"""
from __future__ import annotations

import copy

import pytest

from helixgen import mutate, view
from helixgen.device import normalize
from helixgen.generate import GenerateError
from helixgen.hsp import read_hsp
from helixgen.recipe import apply_recipe
from helixgen.spec import SpecError, parse_spec
from tests.golden import harness


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("snapshot-output-test-library")
    return harness.build_corpus_library(root)


@pytest.fixture
def snapshots_body():
    return read_hsp(harness.CORPUS_DIR / "snapshots.hsp")


def _recipe(snapshots, paths=None):
    return {
        "name": "snap-out",
        "paths": paths or [{"blocks": [{"block": "Brit 2204 Custom"}]}],
        "snapshots": snapshots,
    }


def _gain(body, path=0):
    return body["preset"]["flow"][path]["b13"]["slot"][0]["params"]["gain"]


def _pan(body, path=0):
    return body["preset"]["flow"][path]["b13"]["slot"][0]["params"]["pan"]


# --- spec parsing ------------------------------------------------------------

class TestSpecParse:
    def test_object_form_parses(self):
        spec = parse_spec(_recipe([
            {"name": "Rhythm"},
            {"name": "Lead", "output": {"level": -4.5, "pan": 0.25}},
        ]))
        ov = spec.snapshots[1].output
        assert len(ov) == 1
        assert (ov[0].path, ov[0].level, ov[0].pan) == (0, -4.5, 0.25)
        assert spec.snapshots[0].output == []

    def test_object_form_level_only(self):
        spec = parse_spec(_recipe([{"name": "A", "output": {"level": 3}}]))
        ov = spec.snapshots[0].output[0]
        assert ov.level == 3.0 and ov.pan is None

    def test_list_form_with_paths(self):
        spec = parse_spec(_recipe(
            [{"name": "A", "output": [{"level": -2.0},
                                      {"path": 1, "level": -6.0, "pan": 0.1}]}],
            paths=[{"blocks": [{"block": "Brit 2204 Custom"}]},
                   {"blocks": []}],
        ))
        ovs = spec.snapshots[0].output
        assert [(o.path, o.level, o.pan) for o in ovs] == [
            (0, -2.0, None), (1, -6.0, 0.1)]

    def test_unknown_key_rejected(self):
        with pytest.raises(SpecError, match="unknown output key"):
            parse_spec(_recipe([{"name": "A", "output": {"gain": -3.0}}]))

    def test_needs_level_or_pan(self):
        with pytest.raises(SpecError, match='needs "level" and/or "pan"'):
            parse_spec(_recipe([{"name": "A", "output": {"path": 0}}]))

    def test_level_out_of_range_rejected(self):
        with pytest.raises(SpecError, match="level"):
            parse_spec(_recipe([{"name": "A", "output": {"level": 99.0}}]))

    def test_pan_out_of_range_rejected(self):
        with pytest.raises(SpecError, match="pan"):
            parse_spec(_recipe([{"name": "A", "output": {"pan": 1.5}}]))

    def test_duplicate_path_rejected(self):
        with pytest.raises(SpecError, match="duplicate output override"):
            parse_spec(_recipe(
                [{"name": "A",
                  "output": [{"level": -2.0}, {"path": 0, "level": -3.0}]}]))

    def test_path_out_of_range_rejected(self):
        with pytest.raises(SpecError, match="targets path 1"):
            parse_spec(_recipe([{"name": "A", "output": {"path": 1, "level": -3.0}}]))

    def test_non_object_rejected(self):
        with pytest.raises(SpecError, match='"output" must be'):
            parse_spec(_recipe([{"name": "A", "output": -3.0}]))

    def test_empty_list_rejected(self):
        with pytest.raises(SpecError, match="non-empty"):
            parse_spec(_recipe([{"name": "A", "output": []}]))


# --- generate ----------------------------------------------------------------

class TestGenerate:
    def _compose(self, library, recipe):
        return apply_recipe(recipe, library, chassis=library.load_chassis(),
                            source="test")

    def test_level_override_writes_dense_array(self, library):
        body = self._compose(library, _recipe([
            {"name": "Rhythm"},
            {"name": "Lead", "output": {"level": -4.5}},
        ]))
        g = _gain(body)
        assert g["snapshots"] == [0.0, -4.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        assert g["value"] == 0.0  # active snapshot 0 keeps the base
        assert "snapshots" not in _pan(body)  # pan untouched (no override)

    def test_fill_uses_base_output_level(self, library):
        recipe = _recipe([
            {"name": "Rhythm"},
            {"name": "Lead", "output": {"level": -5.0}},
        ])
        recipe["paths"][0]["output"] = {"level": -2.0}
        body = self._compose(library, recipe)
        g = _gain(body)
        assert g["snapshots"] == [-2.0, -5.0, -2.0, -2.0, -2.0, -2.0, -2.0, -2.0]
        assert g["value"] == -2.0

    def test_snapshot_zero_override_resyncs_value(self, library):
        body = self._compose(library, _recipe([
            {"name": "Rhythm", "output": {"level": -3.0}},
            {"name": "Lead"},
        ]))
        g = _gain(body)
        assert g["snapshots"][0] == -3.0
        assert g["value"] == -3.0  # value mirrors activesnapshot (always 0)

    def test_pan_override_writes_array(self, library):
        body = self._compose(library, _recipe([
            {"name": "A", "output": {"pan": 0.25}},
            {"name": "B"},
        ]))
        p = _pan(body)
        assert p["snapshots"] == [0.25, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
        assert p["value"] == 0.25
        assert "snapshots" not in _gain(body)

    def test_path1_override_lands_on_flow1(self, library):
        body = self._compose(library, _recipe(
            [{"name": "A", "output": [{"path": 1, "level": -6.0}]},
             {"name": "B"}],
            paths=[{"blocks": [{"block": "Brit 2204 Custom"}]},
                   {"blocks": []}],
        ))
        assert _gain(body, path=1)["snapshots"][0] == -6.0
        assert "snapshots" not in _gain(body, path=0)

    def test_declared_snapshots_drop_stale_structural_array(self, library):
        # Author a preset WITH a per-snapshot trim, project it, delete the
        # snapshot `output` field from the projection, regenerate: the
        # recipe is authoritative — the stale array (still present in the
        # projection's verbatim structural b13) must be dropped.
        body = self._compose(library, _recipe([
            {"name": "Rhythm"},
            {"name": "Lead", "output": {"level": -4.5}},
        ]))
        projection = view.view(body, library)
        assert projection["snapshots"][1].pop("output") == {"level": -4.5}
        body2 = self._compose(library, projection)
        assert "snapshots" not in _gain(body2)

    def test_without_snapshots_structural_array_carries_verbatim(self, library):
        # No spec-level snapshots: the pre-#76 verbatim carry behavior is
        # unchanged (a placeholder-named preset keeps its arrays).
        body = self._compose(library, {
            "name": "no-snaps",
            "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
        })
        g = _gain(body)
        g["snapshots"] = [0.0, -4.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        projection = view.view(body, library)
        assert "snapshots" not in projection  # nothing named -> no lift
        body2 = self._compose(library, projection)
        assert _gain(body2)["snapshots"] == [0.0, -4.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    def test_override_without_output_endpoint_errors(self, library):
        # Force a path with no b13 by stripping the chassis endpoint.
        chassis = library.load_chassis()
        broken = copy.deepcopy(chassis)
        del broken["preset"]["flow"][0]["b13"]
        with pytest.raises(GenerateError, match="no lane-0 output endpoint"):
            apply_recipe(_recipe([{"name": "A", "output": {"level": -3.0}}]),
                         library, chassis=broken, source="test")


# --- view lift ---------------------------------------------------------------

class TestViewLift:
    def test_mutate_written_trim_lifts(self, snapshots_body, library):
        # The `device normalize` actuator: a per-snapshot output trim written
        # through mutate.set_flow_param must surface in the projection.
        mutate.set_flow_param(snapshots_body, "output", "level", -4.5,
                              path=0, snapshot="Lead")
        projection = view.view(snapshots_body, library)
        assert projection["snapshots"][1]["output"] == {"level": -4.5}
        assert "output" not in projection["snapshots"][0]
        assert "output" not in projection["snapshots"][2]

    def test_densified_base_fill_not_lifted(self, snapshots_body, library):
        # A uniform (all-base) array is densify fill, not an override.
        gain = _gain(snapshots_body)
        gain["value"] = -2.0
        gain["snapshots"] = [-2.0] * 8
        projection = view.view(snapshots_body, library)
        for snap in projection["snapshots"]:
            assert "output" not in snap

    def test_multi_path_lift_uses_list_form(self, library):
        body = apply_recipe(_recipe(
            [{"name": "A"},
             {"name": "B", "output": [{"level": -2.0},
                                      {"path": 1, "level": -6.0}]}],
            paths=[{"blocks": [{"block": "Brit 2204 Custom"}]},
                   {"blocks": []}],
        ), library, chassis=library.load_chassis(), source="test")
        projection = view.view(body, library)
        assert projection["snapshots"][1]["output"] == [
            {"path": 0, "level": -2.0},
            {"path": 1, "level": -6.0},
        ]

    def test_beyond_named_range_not_lifted(self, snapshots_body, library):
        # snapshots.hsp names 3 snapshots; an override parked on slot 5
        # (placeholder range) has no named snapshot to attach to.
        mutate.set_flow_param(snapshots_body, "output", "level", -9.0,
                              path=0, snapshot=5)
        projection = view.view(snapshots_body, library)
        assert all("output" not in s for s in projection["snapshots"])


# --- round trips ---------------------------------------------------------------

def _strip_provenance(body):
    b = copy.deepcopy(body)
    b.get("meta", {}).get("helixgen", {}).pop("generated_at", None)
    return b


class TestRoundTrip:
    def test_author_generate_view_regenerate(self, library):
        """The backlog #76 acceptance loop: author via recipe -> generate ->
        view -> regenerate; the trims survive and the loop is stable."""
        recipe = _recipe([
            {"name": "Rhythm"},
            {"name": "Lead", "output": {"level": -4.5, "pan": 0.3}},
            {"name": "Clean", "output": {"level": 2.0}},
        ])
        recipe["paths"][0]["output"] = {"level": -1.5}
        chassis = library.load_chassis()
        body = apply_recipe(recipe, library, chassis=chassis, source="test")
        projection = view.view(body, library)
        assert projection["snapshots"][1]["output"] == {"level": -4.5, "pan": 0.3}
        assert projection["snapshots"][2]["output"] == {"level": 2.0}
        body2 = apply_recipe(projection, library, chassis=chassis, source="test")
        assert _strip_provenance(body2) == _strip_provenance(body)
        assert view.view(body2, library) == projection
        # wire-level facts
        g = _gain(body2)
        assert g["snapshots"] == [-1.5, -4.5, 2.0, -1.5, -1.5, -1.5, -1.5, -1.5]
        assert _pan(body2)["snapshots"] == [0.5, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]

    def test_phase2_normalized_hsp_views_and_round_trips(
            self, snapshots_body, library):
        """A phase-2-normalized `.hsp` — built with the actual normalize
        primitives (`apply_snapshot_trim` + `apply_base_trim`, the `device
        normalize --yes` writers) — projects its trims into the recipe form
        and survives a view -> generate round trip."""
        assert normalize.apply_snapshot_trim(snapshots_body, 1, -3.5) == []
        assert normalize.apply_snapshot_trim(snapshots_body, 2, 1.5) == []
        assert normalize.apply_base_trim(snapshots_body, -1.0) == []
        expected = _gain(snapshots_body)["snapshots"]
        # base -1.0; Lead (1) -4.5; Clean (2) +0.5 -- on every output path
        assert expected == [-1.0, -4.5, 0.5, -1.0, -1.0, -1.0, -1.0, -1.0]

        projection = view.view(snapshots_body, library)
        # Trims land on BOTH output paths (normalize applies to every path),
        # so the lift uses the list form.
        assert projection["snapshots"][1]["output"] == [
            {"path": 0, "level": -4.5}, {"path": 1, "level": -4.5}]
        assert projection["snapshots"][2]["output"] == [
            {"path": 0, "level": 0.5}, {"path": 1, "level": 0.5}]
        assert "output" not in projection["snapshots"][0]
        assert projection["paths"][0]["output"] == {"level": -1.0}

        body2 = apply_recipe(projection, library,
                             chassis=library.load_chassis(), source="test")
        for pi in (0, 1):
            assert _gain(body2, path=pi)["snapshots"] == expected
            assert _gain(body2, path=pi)["value"] == -1.0
        # and the re-projection is stable
        assert view.view(body2, library) == projection
