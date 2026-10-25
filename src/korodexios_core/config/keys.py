"""Known configuration keys: what the chassis reads, and what a typo meant."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from .defaults import CORE_DEFAULTS

MODULE_SCOPE: Final[str] = "modules"
"""Namespace prefix a module owns: modules.<name>.<key>."""

PROJECT_SCOPE: Final[str] = "project"
"""Reserved prefix for keys a project owns explicitly."""

MODULE_SETTINGS: Final[frozenset[str]] = frozenset({"enabled", "priority"})
"""Per-module keys the chassis reads even before that module is discovered."""

_SUGGESTION_COUNT: Final[int] = 6
_SUGGESTION_CUTOFF: Final[float] = 0.62
_CHASSIS_NAMESPACES: Final[tuple[str, ...]] = ("core", MODULE_SCOPE)
_NEAR_NAMESPACE_CUTOFF: Final[float] = 0.7
_NEAR_MODULE_CUTOFF: Final[float] = 0.72
"""Closeness a mistyped module name must reach before another module is a suggestion."""


def _mapping_prefixes() -> tuple[str, ...]:
    """Return the chassis keys whose built-in default is a mapping, sorted."""
    declared = ((name, value) for name, value in CORE_DEFAULTS.items())
    return tuple(sorted(name for name, value in declared if isinstance(value, Mapping)))


MAPPING_PREFIXES: Final[tuple[str, ...]] = _mapping_prefixes()
"""Chassis keys that carry project-declared subkeys, such as ``core.env.aliases``."""


def mapping_subkey(key: str) -> str:
    """Return the mapping-valued chassis key ``key`` is a subkey of, or an empty string.

    A mapping default (``core.env.aliases = {}``) names a namespace, not a leaf: every
    project key under it - ``core.env.aliases.LEGACY_RUN_MODE`` - is a path the chassis
    reads. A subkey with an empty segment (``core.env.aliases.``) is malformed and stays
    unknown, so a real typo still surfaces.
    """
    if not key or key in CORE_DEFAULTS:
        return ""
    for prefix in MAPPING_PREFIXES:
        head = f"{prefix}."
        if not key.startswith(head):
            continue
        segments = key[len(head) :].split(".")
        if all(segments) and len(segments) == 1:
            return prefix
    return ""


def core_keys() -> tuple[str, ...]:
    """Return every chassis key, sorted."""
    return tuple(sorted(CORE_DEFAULTS))


def module_scope(name: str) -> str:
    """Return the namespace a module named *name* owns."""
    return f"{MODULE_SCOPE}.{name}"


def is_core_key(key: str) -> bool:
    """Return True when ``key`` is a chassis key with a built-in default.

    A subkey of a mapping-valued default counts as a chassis key too: the default names
    the namespace, the project fills in the leaves.
    """
    return key in CORE_DEFAULTS or bool(mapping_subkey(key))


def module_of(key: str) -> str:
    """Return the module name a module-scoped *key* belongs to, or an empty string."""
    head, _, rest = key.partition(".")
    if head != MODULE_SCOPE or not rest:
        return ""
    module, _, _ = rest.partition(".")
    return module


def _near_namespace(head: str) -> str:
    """Return the chassis namespace *head* almost spells, or an empty string."""
    found = difflib.get_close_matches(
        head, _CHASSIS_NAMESPACES, n=1, cutoff=_NEAR_NAMESPACE_CUTOFF
    )
    return str(found[0]) if found else ""


def is_project_key(key: str) -> bool:
    """Return True when *key* belongs to the project, never to the chassis.

    Anything outside the ``core`` and ``modules`` namespaces is the project's own
    business, dotted or not: ``greeting``, ``app.name``, ``project.owner``. A head
    that only almost spells a chassis namespace stays a suspect instead.
    """
    head, _, _ = key.partition(".")
    if not head:
        return False
    if head == PROJECT_SCOPE:
        return True
    return head not in (MODULE_SCOPE, "core") and not _near_namespace(head)


def is_known_key(key: str, module_names: Iterable[str] = ()) -> bool:
    """Return True when *key* is read by the chassis, a known module, or the project."""
    if is_core_key(key) or is_project_key(key):
        return True
    module = module_of(key)
    if not module:
        return False
    if module in set(module_names):
        return True
    # A forward reference to a module that is not installed yet is still read.
    tail = key.partition(MODULE_SCOPE + ".")[2].partition(".")[2]
    return tail in MODULE_SETTINGS


def _near_module(module: str, names: Sequence[str]) -> str:
    """Return the loaded module ``module`` almost names, or an empty string.

    A module-scoped typo must be answered inside that module's own namespace: naming an
    unrelated loaded module is noise. Containment (``backup`` inside ``backup_engine``)
    is a prefix fact, so it wins when exactly one module matches; otherwise the module
    name has to be close.
    """
    if not module or not names:
        return ""
    contained = [
        name
        for name in names
        if name != module and (name.startswith(module) or module.startswith(name))
    ]
    if len(contained) == 1:
        return contained[0]
    found = difflib.get_close_matches(module, list(names), n=1, cutoff=_NEAR_MODULE_CUTOFF)
    return str(found[0]) if found else ""


def suggest(
    key: str,
    module_names: Iterable[str] = (),
    known_keys: Iterable[str] = (),
) -> str:
    """Return the closest known key or module namespace for ``key``, or an empty string.

    ``known_keys`` are the keys the same configuration actually carries, which let a
    module-scoped typo point at its own sibling (``modules.heartbeat.intervl`` means
    ``modules.heartbeat.interval``) instead of collapsing to the bare namespace.
    """
    names = sorted(set(module_names))
    module = module_of(key)
    present = [name for name in dict.fromkeys(known_keys) if name and name != key]
    if module and module in set(names):
        prefix = f"{module_scope(module)}."
        candidates = [name for name in present if name.startswith(prefix)] or [module_scope(module)]
        matches = difflib.get_close_matches(
            key, candidates, n=_SUGGESTION_COUNT, cutoff=_SUGGESTION_CUTOFF
        )
        return str(matches[0]) if matches else module_scope(module)
    candidates = list(core_keys()) + [module_scope(name) for name in names]
    matches = difflib.get_close_matches(
        key, candidates, n=_SUGGESTION_COUNT, cutoff=_SUGGESTION_CUTOFF
    )
    if matches:
        return str(matches[0])
    if module:
        near = _near_module(module, names)
        if near:
            return module_scope(near)
    return ""


def unknown_keys(
    keys: Iterable[str],
    module_names: Iterable[str] = (),
    known_keys: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """Return (key, suggestion) pairs for keys nothing in the chassis reads.

    Only the two high-confidence classes are reported: a ``core.*`` key with no
    built-in default and no mapping default above it, and a ``modules.<name>`` key
    whose module was never discovered. Anything else is the project's own namespace and
    stays quiet, so a project can keep its keys next to the chassis keys. Suggestions are
    scoped with ``known_keys``, which defaults to the keys being checked.
    """
    present = sorted(set(keys))
    context = tuple(present) if known_keys is None else tuple(dict.fromkeys(known_keys))
    return [
        (key, suggest(key, module_names, context))
        for key in present
        if not is_known_key(key, module_names)
    ]


def project_key_names(keys: Iterable[str]) -> tuple[str, ...]:
    """Return the project-owned keys among *keys*, sorted."""
    return tuple(sorted({key for key in keys if is_project_key(key)}))

