"""Tool-manifest generation for quackpack (backlog #6 / issue #26).

quackpack is, at heart, a curated library of *named, parameterized* queries —
which is exactly the shape a modern agent / MCP layer wants when it enumerates
"callable tools". This module turns saved :class:`~quackpack.store.Query`
records into a stable, machine-readable manifest so a thin shim (or any agent)
can discover the pack's queries and invoke them via the non-interactive ``run``
contract — *without* quackpack becoming an always-on MCP server. We stay a
local CLI; this is just the metadata surface.

Two output shapes are produced:

* ``json`` — a compact custom shape: each tool has ``name``, ``description``,
  and a ``params`` list of ``{name, type, required, default?}`` entries.
* ``jsonschema`` — the same information as JSON-Schema ``inputSchema`` objects
  (``type: object`` with ``properties`` + a ``required`` array), which is what
  most MCP tool definitions expect.

Everything here is pure (no I/O, no DB) so it is trivial to test in isolation.

Parameter typing
----------------
A stored query records only the *names* of its ``:param`` placeholders — the
run engine binds values natively and quackpack's CLI type-coerces
``--param`` strings at call time (``int`` → ``float`` → ``str``; see
:func:`quackpack.params.coerce_value`). To give an agent a useful type hint we
infer each param's type from the best evidence available:

#. the type of a preset-provided default value (if any preset binds it), else
#. ``str`` — the safe default, since that is exactly what the coercion engine
   falls back to for non-numeric input.

A param is **required** when no preset supplies a default for it; if *any*
preset binds a value we treat that value as the tool's default and mark the
param optional. This mirrors how ``run --preset`` seeds a base layer that the
caller may override.
"""

from __future__ import annotations

from typing import Any, Optional

from .params import PARAM_TYPES, coerce_value, extract_params
from .store import Catalog, Query

__all__ = [
    "MANIFEST_FORMATS",
    "PARAM_TYPE_NAMES",
    "JSONSCHEMA_TYPES",
    "param_python_type",
    "type_name",
    "query_params",
    "tool_entry",
    "build_manifest",
]

# The formats the ``tools`` / ``describe`` commands accept.
MANIFEST_FORMATS = ("json", "jsonschema")

# The type names we expose in the manifest; these line up 1:1 with the
# coercions the run engine applies, so an agent's typed call succeeds.
PARAM_TYPE_NAMES = ("int", "float", "str")

# Map our type names onto JSON-Schema primitive types.
JSONSCHEMA_TYPES = {"int": "integer", "float": "number", "str": "string"}

# Reverse of ``PARAM_TYPES`` (python type -> our name) for default inference.
_PYTYPE_TO_NAME = {int: "int", float: "float", str: "str", bool: "int"}


def type_name(value: Any) -> str:
    """Return the manifest type name best describing *value*.

    Booleans collapse to ``int`` (SQL has no bool param affinity here) and any
    unrecognised type falls back to ``str`` so the manifest is always valid.
    """
    # ``bool`` is a subclass of ``int``; check it explicitly for clarity.
    if isinstance(value, bool):
        return "int"
    return _PYTYPE_TO_NAME.get(type(value), "str")


def param_python_type(name: str) -> type:
    """Return the python type for a manifest type *name* (``str`` if unknown)."""
    return PARAM_TYPES.get(name, str)


def _default_for(
    param: str, presets: dict[str, dict[str, Any]]
) -> tuple[bool, Optional[Any], Optional[str]]:
    """Find a preset-provided default for *param*.

    Returns ``(has_default, value, type_name)``. The first preset (in stored
    order) that binds *param* wins; its value both marks the param optional and
    seeds the inferred type.
    """
    for binding in presets.values():
        if param in binding:
            value = binding[param]
            return True, value, type_name(value)
    return False, None, None


def query_params(query: Query) -> list[dict[str, Any]]:
    """Return the typed param schema entries for *query*.

    Each entry is ``{"name", "type", "required"}`` plus an optional
    ``"default"`` when a preset supplies one. Types are inferred per this
    module's docstring: preset-default type, else ``str``.
    """
    # Derive param *names* from the SQL so this stays correct even if a stored
    # ``params`` list drifted from the SQL body.
    names = query.params or extract_params(query.sql)

    entries: list[dict[str, Any]] = []
    for key in names:
        has_default, default, default_type = _default_for(key, query.presets)

        if default_type is not None:
            ptype = default_type
        else:
            ptype = "str"

        entry: dict[str, Any] = {
            "name": key,
            "type": ptype,
            "required": not has_default,
        }
        if has_default:
            # Coerce the stored default through the same rules a CLI value would
            # follow so the manifest's default is the value an agent would get.
            try:
                entry["default"] = coerce_value(default, ptype)
            except (ValueError, TypeError):  # pragma: no cover - defensive
                entry["default"] = default
        entries.append(entry)
    return entries


def _input_schema(params: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a JSON-Schema ``inputSchema`` object from *params* entries."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        prop: dict[str, Any] = {"type": JSONSCHEMA_TYPES.get(p["type"], "string")}
        if "default" in p:
            prop["default"] = p["default"]
        properties[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        # Agents should pass exactly the declared params; extra keys aren't
        # bound by the engine, so signal that clearly.
        "additionalProperties": False,
    }
    return schema


def tool_entry(query: Query, *, fmt: str = "json") -> dict[str, Any]:
    """Return one query rendered as a callable-tool entry in *fmt*.

    ``json`` (default) emits ``{name, description, params}``; ``jsonschema``
    emits ``{name, description, inputSchema}`` with a JSON-Schema input object.
    """
    params = query_params(query)
    if fmt == "jsonschema":
        return {
            "name": query.name,
            "description": query.desc,
            "inputSchema": _input_schema(params),
        }
    return {
        "name": query.name,
        "description": query.desc,
        "params": params,
    }


def build_manifest(
    catalog: Catalog, *, tag: Optional[str] = None, fmt: str = "json"
) -> list[dict[str, Any]]:
    """Return the full tool manifest for *catalog*.

    Queries are listed in the catalog's stable (name-sorted) order via
    :meth:`Catalog.list`, optionally filtered to those carrying *tag*. Each
    entry is a :func:`tool_entry` in the requested *fmt*.
    """
    return [tool_entry(q, fmt=fmt) for q in catalog.list(tag=tag)]
