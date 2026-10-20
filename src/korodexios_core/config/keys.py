"""Known configuration keys: what the chassis reads, and what a typo meant."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
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


def core_keys() -> tuple[str, ...]:
    """Return every chassis key, sorted."""
    return tuple(sorted(CORE_DEFAULTS))


def module_scope(name: str) -> str:
    """Return the namespace a module named *name* owns."""
    return f"{MODULE_SCOPE}.{name}"


def is_core_key(key: str) -> bool:
    """Return True when *key* is a chassis key with a built-in default."""
    return key in CORE_DEFAULTS


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


def suggest(key: str, module_names: Iterable[str] = ()) -> str:
    """Return the closest known key or module namespace for *key*, or an empty string."""
    names = sorted(set(module_names))
    candidates = list(core_keys()) + [module_scope(name) for name in names]
    matches = difflib.get_close_matches(
        key, candidates, n=_SUGGESTION_COUNT, cutoff=_SUGGESTION_CUTOFF
    )
    if matches:
        return str(matches[0])
    module = module_of(key)
    if module:
        found = difflib.get_close_matches(module, names, n=1, cutoff=0.5)
        if found:
            return module_scope(str(found[0]))
    return ""


def unknown_keys(keys: Iterable[str], module_names: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Return (key, suggestion) pairs for keys nothing in the chassis reads.

    Only the two high-confidence classes are reported: a ``core.*`` key with no
    built-in default, and a ``modules.<name>`` key whose module was never
    discovered. Anything else is the project's own namespace and stays quiet, so a
    project can keep its keys next to the chassis keys.
    """
    return [
        (key, suggest(key, module_names))
        for key in sorted(set(keys))
        if not is_known_key(key, module_names)
    ]


def project_key_names(keys: Iterable[str]) -> tuple[str, ...]:
    """Return the project-owned keys among *keys*, sorted."""
    return tuple(sorted({key for key in keys if is_project_key(key)}))

