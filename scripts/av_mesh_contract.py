"""What an av-mesh checkout serves to needletail's tools, read from its
source (no build, no process): the fields of `GET /api/node` and
`GET /api/mesh/local` as their Rust structs declare them, and the paths that
av-mesh's tooling contract test (`fast_lane::the_fields_needletail_s_tools_
read_are_served`, src/tests/fast_lane.rs) asserts.

Paths use "/" between fields and "[]" for each element of a list; a map
(`BTreeMap`, `HashMap`) is a leaf, as is any type not declared as a struct
in the files read. Used by the tests that hold the tools' read lists
(mesh_ops.NODE_READS, stall_records.MESH_PATHS) to what the binary serves;
the root is NEEDLETAIL_AV_MESH_ROOT."""

from __future__ import annotations

import os
import pathlib
import re

ROOT_ENV = "NEEDLETAIL_AV_MESH_ROOT"
#: Files whose structs make the two documents; the first declaration of a
#: name wins.
SOURCES = ("src/node_status.rs", "src/main.rs", "src/fast_plane/relay.rs",
           "src/fast_plane/edge_clients.rs", "src/stall_journal.rs", "src/memory.rs",
           "src/node_warm.rs")
CONTRACT_FILE = "src/tests/fast_lane.rs"
CONTRACT_TEST = "the_fields_needletail_s_tools_read_are_served"

_STRUCT = re.compile(r"^(?:pub(?:\([a-z]+\))? )?struct (\w+)(?:<[^>]*>)? \{\n(.*?)^\}",
                     re.M | re.S)
_FIELD = re.compile(r"^\s*(?:pub(?:\([a-z]+\))? )?(?:r#)?(\w+): (.+?),\s*$")


def root() -> pathlib.Path | None:
    value = os.environ.get(ROOT_ENV)
    return pathlib.Path(value) if value else None


def structs(base: pathlib.Path) -> dict[str, dict[str, str]]:
    """{struct name: {field: Rust type}} of SOURCES (serialized fields only:
    `#[serde(skip)]` fields are left out)."""
    out: dict[str, dict[str, str]] = {}
    for name in SOURCES:
        path = base / name
        if not path.exists():
            continue
        for match in _STRUCT.finditer(path.read_text()):
            fields, skip = {}, False
            for line in match.group(2).splitlines():
                stripped = line.strip()
                if stripped.startswith("#[serde(") and re.search(r"\bskip\b(?!_)", stripped):
                    skip = True
                    continue
                if stripped.startswith(("#", "//")) or not stripped:
                    continue
                field = _FIELD.match(line)
                if field:
                    if not skip:
                        fields[field.group(1)] = field.group(2).strip()
                    skip = False
            out.setdefault(match.group(1), fields)
    return out


def _inner(rust: str) -> tuple[str, str]:
    """(kind, type): "list" for Vec or an array, "map", else "one"."""
    rust = rust.strip()
    for wrapper in ("Option<", "Box<", "Arc<"):
        if rust.startswith(wrapper) and rust.endswith(">"):
            return _inner(rust[len(wrapper):-1])
    if rust.startswith("Vec<") and rust.endswith(">"):
        return "list", _inner(rust[4:-1])[1]
    if rust.startswith("[") and ";" in rust:
        return "list", _inner(rust[1:rust.index(";")])[1]
    if re.match(r"^(?:std::collections::)?(?:BTreeMap|HashMap)<", rust):
        return "map", rust
    return "one", rust.split("::")[-1]


def paths(declared: dict[str, dict[str, str]], top: str, depth: int = 8) -> set[str]:
    """Every path of struct `top` as served (prefixes included; a list is
    there both as `name` and as `name[]`)."""
    out: set[str] = set()

    def walk(name: str, prefix: str, left: int) -> None:
        if left == 0:
            return
        for field, rust in declared.get(name, {}).items():
            kind, inner = _inner(rust)
            here = f"{prefix}{field}" + ("[]" if kind == "list" else "")
            out.add(here)
            out.add(f"{prefix}{field}")
            if kind != "map" and inner in declared:
                walk(inner, here + "/", left - 1)

    walk(top, "", depth)
    return out


def node_paths(base: pathlib.Path) -> set[str]:
    """`GET /api/node`: av-mesh's NodeStatus."""
    return paths(structs(base), "NodeStatus")


def mesh_local_paths(base: pathlib.Path) -> set[str]:
    """`GET /api/mesh/local`: av-mesh's MeshApiSnapshot."""
    return paths(structs(base), "MeshApiSnapshot")


def contract(base: pathlib.Path) -> dict[str, set[str]]:
    """The paths av-mesh's tooling contract test asserts: {"node": paths of
    /api/node, "mesh_local": paths of /api/mesh/local}."""
    text = (base / CONTRACT_FILE).read_text()
    start = text.index(f"fn {CONTRACT_TEST}(")
    end = text.find("\n}\n", start)
    body = text[start:end]
    local_at = body.index('"/api/mesh/local"')
    node_part, local_part = body[:local_at], body[local_at:]
    node = {item.strip("/") for item in re.findall(r'"(/[a-z_0-9/]+)"', node_part)}
    for chain in re.findall(r'node((?:\["\w+"\])+)', node_part):
        node.add("/".join(re.findall(r'"(\w+)"', chain)))
    local: set[str] = set()
    sections = re.split(r"let (\w+) = &local((?:\[\"\w+\"\])+);", local_part)
    # sections: [before, name, chain, text, name, chain, text, ...]
    for index in range(1, len(sections), 3):
        prefix = "/".join(re.findall(r'"(\w+)"', sections[index + 1]))
        text_of = sections[index + 2]
        for array in re.findall(r"for field in \[(.*?)\]", text_of, re.S):
            local.update(f"{prefix}/{field}" for field in re.findall(r'"(\w+)"', array))
        for field in re.findall(r'\w+\["(\w+)"\]', text_of):
            local.add(f"{prefix}/{field}")
    return {"node": node, "mesh_local": local}
