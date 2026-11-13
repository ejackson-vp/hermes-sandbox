"""Canonical JSON must match the TypeScript implementation byte for byte.

The server hashes and signs these strings. A different key order or a stray
space produces a different hash, which shows up as ``client_proof_invalid``
rather than a helpful error, so this is pinned with explicit examples.
"""

from __future__ import annotations

from hermes_sandbox.canonical import canonical_json


def test_sorts_keys():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_no_whitespace():
    assert canonical_json({"a": [1, 2], "b": {"c": True}}) == '{"a":[1,2],"b":{"c":true}}'


def test_drops_none_like_undefined():
    # JSON.stringify drops undefined object values; the port drops None.
    assert canonical_json({"a": 1, "b": None}) == '{"a":1}'


def test_keeps_null_inside_arrays():
    assert canonical_json([1, None]) == "[1,null]"


def test_nested_sorted():
    assert canonical_json({"z": {"y": 1, "x": 2}}) == '{"z":{"x":2,"y":1}}'


def test_string_escaping():
    assert canonical_json({"a": 'he said "hi"'}) == '{"a":"he said \\"hi\\""}'


def test_empty_structures():
    assert canonical_json({}) == "{}"
    assert canonical_json([]) == "[]"
