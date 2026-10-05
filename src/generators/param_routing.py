"""Shared parameter-key resolution for namespace execute calls.

Callers may place operation parameters in three declared buckets
(``path_params``, ``query_params``, ``headers``) or, for legacy clients, as flat
top-level keys. Validation (``ToolGenerator``) and routing (``RuntimeToolDispatcher``)
both use these helpers so the accepted key set and the dispatch lookup never diverge.
"""

from __future__ import annotations

from typing import Any

from src.parsers import OperationInfo, ParameterInfo

# Legacy underscore aliases for OData query parameters.
ODATA_ALIAS_MAP = {
    "_page": "$page",
    "_limit": "$limit",
    "_filter": "$filter",
    "_orderby": "$orderby",
    "_select": "$select",
    "_expand": "$expand",
}

PARAM_BUCKETS = ("path_params", "query_params", "headers")
RESERVED_KEYS = frozenset({"operation", "request_body", *PARAM_BUCKETS})

# Lowest to highest precedence when the same key appears in several places.
_BUCKET_PRECEDENCE = ("headers", "query_params", "path_params")

_LOCATION_TO_BUCKET = {"path": "path_params", "query": "query_params", "header": "headers"}


def effective_parameters(operation: OperationInfo) -> list[ParameterInfo]:
    """Spec parameters plus an implicit ``If-Match`` header on non-GET operations.

    Some specs omit If-Match on write operations whose endpoint still checks it
    (e.g. the AHV ``$actions/associate-categories`` in vmm v4.3 while the ESXi
    variant declares it). Accepting and forwarding a caller-supplied If-Match keeps
    those operations usable instead of rejecting or dropping the header.
    """
    parameters = list(operation.parameters)
    declares_if_match = any(
        p.location == "header" and p.name.lower() == "if-match" for p in parameters
    )
    if operation.method.upper() != "GET" and not declares_if_match:
        parameters.append(ParameterInfo(name="If-Match", location="header", required=False))
    return parameters


def bucket_for_location(location: str) -> str | None:
    """Return the declared bucket name for an OpenAPI parameter location."""
    return _LOCATION_TO_BUCKET.get(location)


def parameter_key_candidates(param_name: str) -> list[str]:
    """Keys accepted for a spec parameter, most specific first.

    ``$filter`` -> ``["$filter", "_filter", "filter"]``.
    """
    candidates = [param_name]
    for alias, original in ODATA_ALIAS_MAP.items():
        if original == param_name:
            candidates.append(alias)
    if param_name.startswith("$") and len(param_name) > 1:
        candidates.append(param_name[1:])
    return candidates


def iter_supplied_keys(args: dict[str, Any]) -> list[tuple[str, str]]:
    """List ``(source, key)`` pairs for every caller-supplied parameter key.

    ``source`` is ``"top-level"`` for legacy flat keys or the bucket name.
    Non-dict buckets are skipped (they are rejected separately).
    """
    supplied: list[tuple[str, str]] = [
        ("top-level", key) for key in args if key not in RESERVED_KEYS
    ]
    for bucket in PARAM_BUCKETS:
        values = args.get(bucket)
        if isinstance(values, dict):
            supplied.extend((bucket, str(key)) for key in values)
    return supplied


def merge_supplied_parameters(args: dict[str, Any]) -> dict[str, Any]:
    """Merge flat keys and bucket contents into one lookup dict.

    Precedence (lowest to highest): flat top-level keys, headers, query_params,
    path_params. The bucket a value arrives in does not decide where it is sent —
    routing always follows the spec's parameter location.
    """
    merged: dict[str, Any] = {
        key: value for key, value in args.items() if key not in RESERVED_KEYS
    }
    for bucket in _BUCKET_PRECEDENCE:
        values = args.get(bucket)
        if isinstance(values, dict):
            merged.update({str(key): value for key, value in values.items()})
    return merged


def resolve_parameter_value(parameter: ParameterInfo, supplied: dict[str, Any]) -> Any:
    """Find the caller value for a spec parameter, or ``None`` when absent.

    Lookup order: exact name, OData underscore alias, bare OData name
    (``filter`` for ``$filter``). Header names also match case-insensitively,
    since HTTP header names are case-insensitive.
    """
    for key in parameter_key_candidates(parameter.name):
        if key in supplied:
            return supplied[key]
    if parameter.location == "header":
        lowered = parameter.name.lower()
        for key, value in supplied.items():
            if key.lower() == lowered:
                return value
    return None


def is_accepted_key(key: str, parameters: list[ParameterInfo]) -> bool:
    """Return whether a supplied key maps to any parameter of the operation."""
    for parameter in parameters:
        if key in parameter_key_candidates(parameter.name):
            return True
        if parameter.location == "header" and key.lower() == parameter.name.lower():
            return True
    return False
