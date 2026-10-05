"""Tool schema generation from parsed operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from src.generators.models import OperationDiscoveryItem, ToolAnnotations, ToolDefinition, ToolInputSchema
from src.generators.param_routing import (
    PARAM_BUCKETS,
    RESERVED_KEYS,
    bucket_for_location,
    effective_parameters,
    is_accepted_key,
    iter_supplied_keys,
)
from src.generators.schema_resolver import SchemaResolver
from src.parsers import OperationInfo, ParameterInfo
from src.parsers.yaml_parser import NamespaceMetadata, _camel_to_tokens

_VERSION_RE = re.compile(r"^v\d+", re.IGNORECASE)
_ACTION_SUFFIX_RE = re.compile(r"/\$actions/[^/]+$")

# Declared parameter buckets on every ``<namespace>_execute`` tool.
_BUCKET_PROPERTIES: dict[str, dict[str, Any]] = {
    "path_params": {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Path parameters (getOperationSchema path_parameters), keyed by exact name. "
            'Example: {"extId": "<uuid>"}'
        ),
    },
    "query_params": {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Query parameters (getOperationSchema query_parameters), keyed by exact name "
            "including the '$' prefix. Example: "
            '{"$filter": "startswith(name,\'zy-\')", "$select": "extId,name,categories", "$limit": 100}'
        ),
    },
    "headers": {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Header parameters (getOperationSchema header_parameters). "
            'Example: {"If-Match": "<_etag from GET>"}'
        ),
    },
}


def _score_operation(
    operation: OperationInfo,
    query_tokens: list[str],
) -> tuple[int, list[str]]:
    """Score an operation against query tokens using field-weighted matching.

    Returns (score, matched_fields). Score 0 means no match (all tokens must hit).
    Higher score = stronger relevance. Fields checked in order of signal strength:
      registered_name (raw)      -> 50 pts per token  (callable name, variant-aware)
      operation_id (raw)         -> 50 pts per token  (spec name; same tier as registered_name)
      operation_id (camel split) -> 40 pts per token
      summary                    -> 20 pts per token
      path tokens                -> 10 pts per token
      enriched search_text       ->  5 pts per token (tag names, descriptions)
    Bonus: +20 when ALL tokens matched in a single high-signal field (concentrated match).
    """
    op_id_lower = operation.operation_id.lower()
    # registered_name differs from operation_id only for variant operations (e.g. ahv_listVms).
    # Score it at the same tier so variant discriminators ("ahv", "esxi") rank correctly.
    reg_name_lower = operation.registered_name.lower()
    reg_name_differs = reg_name_lower != op_id_lower
    op_id_tokens = set(_camel_to_tokens(operation.operation_id).split())
    summary_lower = operation.summary.lower()
    # Path tokens: split on / and strip version segments and braces
    version_re = _VERSION_RE
    path_tokens_text = " ".join(
        seg.replace("-", " ").replace("_", " ").lower()
        for seg in operation.path.split("/")
        if seg and not seg.startswith("{") and not version_re.match(seg)
    )
    search_text = operation.search_text

    score = 0
    matched_fields: list[str] = []
    field_hits: dict[str, int] = {}

    for token in query_tokens:
        hit = False
        if token in op_id_lower or (reg_name_differs and token in reg_name_lower):
            score += 50
            field_hits["operation_id"] = field_hits.get("operation_id", 0) + 1
            hit = True
        elif token in op_id_tokens:
            score += 40
            field_hits["operation_id"] = field_hits.get("operation_id", 0) + 1
            hit = True
        if token in summary_lower:
            score += 20
            field_hits["summary"] = field_hits.get("summary", 0) + 1
            hit = True
        if token in path_tokens_text:
            score += 10
            field_hits["path"] = field_hits.get("path", 0) + 1
            hit = True
        if token in search_text and not hit:
            # Catch tag names, descriptions, CamelCase expansions not yet scored above.
            score += 5
            field_hits["search_text"] = field_hits.get("search_text", 0) + 1
            hit = True

        if not hit:
            # Token not found anywhere — AND semantics: entire operation is not a match.
            return 0, []

    # Concentrated match bonus: all tokens hit the same high-signal field.
    if field_hits.get("operation_id", 0) == len(query_tokens):
        score += 20
    elif field_hits.get("summary", 0) == len(query_tokens):
        score += 10

    matched_fields = sorted(field_hits.keys())
    return score, matched_fields


@dataclass(slots=True)
class ToolContractError(ValueError):
    """Validation error for namespace execute contract."""

    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


class ToolGenerator:
    """Generate namespace-level tool schemas from parsed operations."""

    # OData query params whose valid values are spec-defined per operation.
    _ODATA_FIELD_PARAMS = frozenset({"$filter", "$select", "$orderby", "$expand"})

    def __init__(
        self,
        operations: list[OperationInfo],
        schemas: dict[str, Any] | None = None,
        namespace_metadata: dict[str, NamespaceMetadata] | None = None,
        auto_etag: bool = False,
    ) -> None:
        self.operations = operations
        self.auto_etag = auto_etag
        self._schemas = schemas or {}
        self._namespace_metadata = namespace_metadata or {}
        self._resolver = SchemaResolver(self._schemas)
        # Lazy cache: populated on first getOperationSchema call per operation.
        self._schema_cache: dict[str, dict[str, Any]] = {}

    def group_by_namespace(self) -> dict[str, list[OperationInfo]]:
        """Group parsed operations by namespace."""
        grouped: dict[str, list[OperationInfo]] = {}
        for operation in self.operations:
            grouped.setdefault(operation.namespace, []).append(operation)
        return grouped

    def build_namespace_tools(self) -> list[dict[str, Any]]:
        """Build ``<namespace>_execute`` tool schemas with D2 descriptions.

        Declared properties: ``operation``, ``request_body`` and the three parameter
        buckets ``path_params``, ``query_params`` and ``headers``. The buckets must be
        declared so strict MCP clients (e.g. Open WebUI) forward them instead of
        dropping undeclared keys. ``additionalProperties: true`` is kept at the top
        level so legacy clients that send parameters as flat keys keep working.
        The exact parameter names and body schema for any operation are obtained
        from ``getOperationSchema``.
        """
        tools: list[dict[str, Any]] = []
        for namespace, ops in sorted(self.group_by_namespace().items()):
            registered_names = [op.registered_name for op in ops]
            meta = self._namespace_metadata.get(namespace)
            description = self._build_tool_description(namespace, meta, len(registered_names))
            categories = meta.categories if meta else []
            tool = ToolDefinition(
                name=f"{namespace}_execute",
                description=description,
                inputSchema=ToolInputSchema(
                    properties={
                        "operation": {"type": "string", "enum": registered_names},
                        "request_body": {
                            "type": "object",
                            "description": (
                                "JSON body for POST/PUT/PATCH operations. "
                                "Use exact field names from getOperationSchema request_body_schema. "
                                "Omit for GET/DELETE."
                            ),
                        },
                        **{name: dict(prop) for name, prop in _BUCKET_PROPERTIES.items()},
                    },
                    required=["operation"],
                    additional_properties=True,
                ),
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    openWorldHint=True,
                ),
                metadata={
                    "namespace": namespace,
                    "operation_count": len(registered_names),
                    "categories": categories,
                },
            )
            tools.append(tool.model_dump(by_alias=True, exclude_none=True))
        return tools

    def _build_tool_description(
        self,
        namespace: str,
        meta: NamespaceMetadata | None,
        op_count: int,
    ) -> str:
        """Compose a structured description for a namespace execute tool.

        Output format:
            <Title> — <overview sentence>
            Covers: <tag1> · <tag2> · ...
            N operations. Always call getOperationSchema before executing.

        Example:
            Nutanix VMM APIs — Manage the life-cycle of virtual machines hosted on Nutanix
            Covers: Templates · OVAs · Images · VMs · VM Recovery Points.
            192 operations. Always call getOperationSchema before executing.
        """
        if meta is None:
            return (
                f"Execute operations from the {namespace} namespace. "
                "Use the operation field to select the exact API operation."
            )
        title = meta.title or namespace
        desc = f"{title}"
        if meta.description:
            desc += f" — {meta.description}"
        if meta.categories:
            cats = " · ".join(meta.categories)
            desc += f"\nCovers: {cats}."
        desc += f"\n{op_count} operations. Always call getOperationSchema before executing."
        return desc

    def build_operation_index(self) -> dict[str, dict[str, Any]]:
        """Build raw lookup index keyed by registered_name (used for logging/counts)."""
        index: dict[str, dict[str, Any]] = {}
        for operation in self.operations:
            index[operation.registered_name] = asdict(operation)
        return index

    def build_discovery_tools(self) -> list[dict[str, Any]]:
        """Build progressive disclosure helper tool schemas."""
        definitions = [
            ToolDefinition(
                name="listOperations",
                description=(
                    "List available API operations, optionally filtered by namespace or search terms. "
                    "Results are ranked by relevance score (descending) when a search term is provided — "
                    "position 1 is the server's highest-confidence match. "
                    "Each result includes relevance_score (higher = stronger match) and match_fields "
                    "(the fields where your tokens were found). "
                    "match_fields containing 'operation_id' is a strong signal; 'search_text' only is weak. "
                    "Use 1-2 keyword tokens for best results. Default limit is 20."
                ),
                inputSchema=ToolInputSchema(
                    properties={
                        "namespace": {"type": "string"},
                        "search": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                        "offset": {"type": "integer", "minimum": 0},
                    }
                ),
            ),
            ToolDefinition(
                name="getOperationSchema",
                description=(
                    "Get full schema details for a specific operation id. "
                    "Returns path/query/header parameters, resolved request_body_schema "
                    "(with all $ref/allOf/oneOf chains expanded), immutable_fields (readOnly), "
                    "and required_body_fields. Always call this before executing a write operation."
                ),
                inputSchema=ToolInputSchema(
                    properties={"operation": {"type": "string"}},
                    required=["operation"],
                ),
            ),
            ToolDefinition(
                name="getCodeSample",
                description="Get a language-specific code sample for an operation when available.",
                inputSchema=ToolInputSchema(
                    properties={
                        "operation": {"type": "string"},
                        "language": {"type": "string"},
                    },
                    required=["operation", "language"],
                ),
            ),
            ToolDefinition(
                name="getOperationPermissions",
                description="Get required roles/permissions metadata for a specific operation id.",
                inputSchema=ToolInputSchema(
                    properties={"operation": {"type": "string"}},
                    required=["operation"],
                ),
            ),
        ]
        return [definition.model_dump(by_alias=True, exclude_none=True) for definition in definitions]

    def list_operations(
        self,
        namespace: str | None = None,
        search: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List operations with optional namespace/search filtering, ranked by relevance."""
        normalized_namespace = namespace.strip() if isinstance(namespace, str) else None
        query_tokens = (
            [t for t in search.lower().split() if t]
            if isinstance(search, str) and search.strip()
            else []
        )

        scored: list[tuple[int, list[str], OperationDiscoveryItem]] = []

        for operation in self.operations:
            if normalized_namespace and operation.namespace != normalized_namespace:
                continue

            if query_tokens:
                score, matched_fields = _score_operation(operation, query_tokens)
                if score == 0:
                    continue
            else:
                score, matched_fields = 0, []

            scored.append((
                score,
                matched_fields,
                OperationDiscoveryItem(
                    namespace=operation.namespace,
                    operation=operation.registered_name,
                    method=operation.method,
                    path=operation.path,
                    summary=operation.summary,
                    permission_name=self._extract_permission_name(operation.permissions),
                    required_roles=operation.required_roles,
                    relevance_score=score if query_tokens else None,
                    match_fields=matched_fields,
                    spec_operation_id=(
                        operation.operation_id
                        if operation.path_variant is not None
                        else None
                    ),
                    path_variant=operation.path_variant,
                ),
            ))

        if query_tokens:
            # Sort by score descending, then alphabetically for stable tie-breaking.
            scored.sort(key=lambda x: (-x[0], x[2].namespace, x[2].operation))
        else:
            scored.sort(key=lambda x: (x[2].namespace, x[2].operation))

        page = scored[offset: offset + limit]
        return [item.model_dump() for _, _, item in page]

    def get_operation_schema(self, operation_id: str) -> dict[str, Any]:
        """Return structured schema for an operation, resolved lazily and cached."""
        if operation_id in self._schema_cache:
            return self._schema_cache[operation_id]
        op = self._find_operation(operation_id)
        result = self._build_structured_schema(op)
        self._schema_cache[operation_id] = result
        return result

    def get_code_sample(self, operation_id: str, language: str) -> dict[str, Any] | None:
        """Return the best matching code sample for operation/language."""
        op = self._find_operation(operation_id)
        requested = language.lower().strip()
        for sample in op.code_samples:
            sample_language = sample.get("lang") or sample.get("language")
            if isinstance(sample_language, str) and sample_language.lower() == requested:
                return sample
        return None

    def get_operation_permissions(self, operation_id: str) -> dict[str, Any]:
        """Return permission metadata and required roles for an operation id."""
        op = self._find_operation(operation_id)
        permissions = op.permissions
        permission_name = (
            permissions.get("operationName")
            if isinstance(permissions, dict) and isinstance(permissions.get("operationName"), str)
            else None
        )
        return {
            "operation": op.registered_name,
            "namespace": op.namespace,
            "method": op.method,
            "path": op.path,
            "permission_name": permission_name,
            "required_roles": op.required_roles,
            "raw_permissions": permissions,
        }

    def _find_operation(self, operation_id: str) -> OperationInfo:
        """Look up an OperationInfo by registered_name, raising KeyError if absent."""
        for op in self.operations:
            if op.registered_name == operation_id:
                return op
        raise KeyError(f"Unknown operation id: {operation_id!r}")

    def _build_structured_schema(self, op: OperationInfo) -> dict[str, Any]:
        """Build the deterministic structured response for getOperationSchema."""
        path_params: list[dict[str, Any]] = []
        query_params: list[dict[str, Any]] = []
        header_params: list[dict[str, Any]] = []

        for param in op.parameters:
            formatted = self._format_param(param)
            if param.location == "path":
                path_params.append(formatted)
            elif param.location == "query":
                query_params.append(formatted)
            elif param.location == "header":
                if param.name == "NTNX-Request-Id":
                    formatted["auto_managed"] = "Auto-injected by server. No action needed."
                elif param.name.lower() == "if-match":
                    formatted["auto_managed"] = self._if_match_guidance(op)
                header_params.append(formatted)

        body_schema: Any = None
        body_required = False
        immutable_fields: list[str] = []
        required_body_fields: list[str] = []

        if op.request_body and isinstance(op.request_body, dict):
            body_required = bool(op.request_body.get("required", False))
            schema_node = (
                op.request_body
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if schema_node:
                resolved = self._resolver.resolve(schema_node)
                rtype = resolved.get("type")
                if rtype == "object":
                    body_schema = resolved.get("properties") or {}
                    # Primitive readOnly fields remain in properties with readOnly:true marker;
                    # complex readOnly fields (arrays, objects) are already filtered by resolver.
                    immutable_fields = [
                        k for k, v in body_schema.items()
                        if isinstance(v, dict) and v.get("readOnly")
                    ]
                    required_body_fields = resolved.get("required", [])
                else:
                    body_schema = resolved

        result: dict[str, Any] = {
            "operation": op.registered_name,
            "method": op.method,
            "path": op.path,
            "path_parameters": path_params,
            "query_parameters": query_params,
            "header_parameters": header_params,
            "how_to_pass": {
                "path_parameters": "path_params",
                "query_parameters": "query_params",
                "header_parameters": "headers",
            },
            "request_body_required": body_required,
            "request_body_schema": body_schema,
            "immutable_fields": immutable_fields,
            "required_body_fields": required_body_fields,
        }
        if op.method.upper() == "POST" and required_body_fields:
            result["post_guidance"] = (
                "Send ONLY required_body_fields plus fields the user explicitly requested. "
                "All other fields must be omitted — server-assigned fields are rejected "
                "even when their values appear schema-valid."
            )
        if op.method.upper() in ("PUT", "PATCH"):
            result["put_guidance"] = (
                "PUT/PATCH requires the complete resource body. "
                "1. GET the resource first. "
                "2. Clone the full GET response body. "
                "3. Strip '_etag' (pass as headers: {'If-Match': '<_etag>'}) and 'links'. "
                "4. Modify only the fields the user asked to change. "
                "5. Send the full cloned body. "
                "Do NOT build the body incrementally from schema — omitting any field the "
                "server expects will fail even if that field was not explicitly changed."
            )
        return result

    def _if_match_guidance(self, op: OperationInfo) -> str:
        """Describe how the If-Match header is supplied for this operation."""
        if self.auto_etag and self.find_etag_source_operation(op) is not None:
            return (
                "Auto-fetched by the server when omitted (AUTO_ETAG enabled). "
                "To pin a specific version, pass headers: {'If-Match': '<_etag from GET>'}."
            )
        return (
            "Pass headers: {'If-Match': '<_etag>'} using the _etag value from the prior "
            "GET response of this resource."
        )

    def find_etag_source_operation(self, op: OperationInfo) -> OperationInfo | None:
        """Return the GET operation whose response carries the ETag for ``op``.

        Action operations (``.../{extId}/$actions/<verb>``) read the ETag from the
        parent resource; PUT/PATCH read it from their own path. The same namespace
        is preferred when several GET operations share the path template.
        """
        if _ACTION_SUFFIX_RE.search(op.path):
            resource_path = _ACTION_SUFFIX_RE.sub("", op.path)
        elif op.method.upper() in ("PUT", "PATCH"):
            resource_path = op.path
        else:
            return None
        candidates = [
            item for item in self.operations
            if item.method.upper() == "GET" and item.path == resource_path
        ]
        if not candidates:
            return None
        same_namespace = [item for item in candidates if item.namespace == op.namespace]
        return (same_namespace or candidates)[0]

    # OData query params whose values depend on response field names — guessing causes errors.
    @classmethod
    def _format_param(cls, param: ParameterInfo) -> dict[str, Any]:
        """Convert a ParameterInfo to the compact schema dict sent to the LLM."""
        result: dict[str, Any] = {"name": param.name, "required": param.required}
        if param.description:
            result["description"] = param.description
        schema = param.schema
        if isinstance(schema, dict):
            for key in ("type", "format", "enum", "pattern", "minimum", "maximum",
                        "maxLength", "minLength"):
                val = schema.get(key)
                if val is not None:
                    result[key] = val
        if param.name in cls._ODATA_FIELD_PARAMS and param.odata_fields:
            result["odata_fields"] = param.odata_fields
        return result

    @staticmethod
    def _extract_permission_name(permissions: dict[str, Any] | None) -> str | None:
        if permissions is None:
            return None
        operation_name = permissions.get("operationName")
        if isinstance(operation_name, str) and operation_name.strip():
            return operation_name.strip()
        return None

    def validate_namespace_operation_request(
        self,
        namespace: str,
        operation: str,
        request_payload: dict[str, Any],
        strict: bool = True,
    ) -> None:
        """Validate namespace, operation, request_body type and parameter keys.

        Parameter buckets must be objects. With ``strict`` enabled (``STRICT_PARAMS``),
        every supplied key — flat top-level or inside a bucket — must map to one of
        the operation's parameters (exact name, OData alias, or bare OData name).
        Unknown keys raise ``unknown_parameter`` instead of being silently dropped.
        """
        grouped = self.group_by_namespace()
        namespace_ops = grouped.get(namespace)
        if namespace_ops is None:
            raise ToolContractError(
                code="unknown_namespace",
                detail=f"Unknown namespace: {namespace}",
            )

        target = next((item for item in namespace_ops if item.registered_name == operation), None)
        if target is None:
            raise ToolContractError(
                code="unknown_operation",
                detail=f"Unknown operation '{operation}' for namespace '{namespace}'",
            )

        if "request_body" in request_payload and request_payload["request_body"] is not None:
            if not isinstance(request_payload["request_body"], dict):
                raise ToolContractError(
                    code="invalid_parameters",
                    detail="request_body must be an object when provided.",
                )

        for bucket in PARAM_BUCKETS:
            value = request_payload.get(bucket)
            if value is not None and not isinstance(value, dict):
                raise ToolContractError(
                    code="invalid_parameters",
                    detail=f"{bucket} must be an object when provided.",
                )

        if not strict:
            return

        unknown = [
            key if source == "top-level" else f"{source}.{key}"
            for source, key in iter_supplied_keys(request_payload)
            if not is_accepted_key(key, effective_parameters(target))
        ]
        if unknown:
            raise ToolContractError(
                code="unknown_parameter",
                detail=(
                    f"Unknown parameter(s) for operation '{operation}': {', '.join(unknown)}. "
                    f"Accepted: {self._describe_accepted_keys(target)}. "
                    "Check exact names with getOperationSchema."
                ),
            )

    @staticmethod
    def _describe_accepted_keys(op: OperationInfo) -> str:
        """Summarize accepted keys per bucket for an unknown_parameter error."""
        grouped: dict[str, list[str]] = {bucket: [] for bucket in PARAM_BUCKETS}
        for param in effective_parameters(op):
            bucket = bucket_for_location(param.location)
            if bucket is not None:
                grouped[bucket].append(param.name)
        parts = [f"{bucket}: [{', '.join(names)}]" for bucket, names in grouped.items() if names]
        parts.append(f"reserved: [{', '.join(sorted(RESERVED_KEYS))}]")
        return "; ".join(parts)
