"""Library-first IR folders sidecar (hash -> folder label)."""
from __future__ import annotations

import json

import pytest

from helixgen import ir_folders


def test_missing_sidecar_is_flat(tmp_path):
    assert ir_folders.load(library_dir=tmp_path) == {}


def test_set_and_load_roundtrip(tmp_path):
    ir_folders.set_folder("aaa", "Marshall cabs", library_dir=tmp_path)
    ir_folders.set_folder("bbb", "Fender cabs", library_dir=tmp_path)
    assert ir_folders.load(library_dir=tmp_path) == {
        "aaa": "Marshall cabs", "bbb": "Fender cabs"}


def test_set_blank_folder_unfiles(tmp_path):
    ir_folders.set_folder("aaa", "X", library_dir=tmp_path)
    ir_folders.set_folder("aaa", "", library_dir=tmp_path)
    assert ir_folders.load(library_dir=tmp_path) == {}
    ir_folders.set_folder("bbb", "Y", library_dir=tmp_path)
    ir_folders.set_folder("bbb", None, library_dir=tmp_path)
    assert ir_folders.load(library_dir=tmp_path) == {}


def test_label_is_trimmed(tmp_path):
    ir_folders.set_folder("aaa", "  Cabs  ", library_dir=tmp_path)
    assert ir_folders.load(library_dir=tmp_path) == {"aaa": "Cabs"}


def test_blank_hash_rejected(tmp_path):
    with pytest.raises(ValueError):
        ir_folders.set_folder("  ", "X", library_dir=tmp_path)


def test_corrupt_sidecar_degrades_to_flat(tmp_path):
    (tmp_path / ir_folders.SIDECAR_NAME).write_text("not json{", encoding="utf-8")
    assert ir_folders.load(library_dir=tmp_path) == {}


def test_non_object_folders_degrades(tmp_path):
    (tmp_path / ir_folders.SIDECAR_NAME).write_text(
        json.dumps({"folders": ["a", "b"]}), encoding="utf-8")
    assert ir_folders.load(library_dir=tmp_path) == {}


def test_blank_and_non_string_labels_dropped_on_load(tmp_path):
    (tmp_path / ir_folders.SIDECAR_NAME).write_text(
        json.dumps({"folders": {"a": "Cabs", "b": "  ", "c": 3}}),
        encoding="utf-8")
    assert ir_folders.load(library_dir=tmp_path) == {"a": "Cabs"}


def test_written_sidecar_is_versioned_json(tmp_path):
    ir_folders.set_folder("aaa", "Cabs", library_dir=tmp_path)
    doc = json.loads((tmp_path / ir_folders.SIDECAR_NAME).read_text())
    assert doc == {"version": 1, "folders": {"aaa": "Cabs"}}
