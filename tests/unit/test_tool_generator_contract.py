"""Unit tests for namespace tool contract behavior."""

from __future__ import annotations

import pytest

from src.generators import ToolContractError, ToolGenerator
from src.parsers import OperationInfo, ParameterInfo


def _operation() -> OperationInfo:
    return OperationInfo(
        namespace="vmm",
        operation_id="getVmById",
        path="/vms/{vmId}",
        method="GET",
        summary="Get VM",
        description="Get VM by id",
        parameters=[
            ParameterInfo(name="vmId", location="path", required=True),
            ParameterInfo(name="$filter", location="query", required=False),
        ],
    )


def test_namespace_tool_description_is_compact() -> None:
    tools = ToolGenerator([_operation()]).build_namespace_tools()
    assert len(tools) == 1
    description = tools[0]["description"]
    assert "Use the operation field" in description
    assert "Available operations:" not in description


def test_validate_operation_request_accepts_allowed_fields() -> None:
    generator = ToolGenerator([_operation()])
    generator.validate_namespace_operation_request(
        namespace="vmm",
        operation="getVmById",
        request_payload={
            "operation": "getVmById",
            "vmId": "1234",
            "_filter": "name eq 'a'",
            "request_body": {"foo": "bar"},
        },
    )


def test_validate_operation_request_rejects_unknown_keys_when_strict() -> None:
    generator = ToolGenerator([_operation()])
    with pytest.raises(ToolContractError) as exc:
        generator.validate_namespace_operation_request(
            namespace="vmm",
            operation="getVmById",
            request_payload={
                "operation": "getVmById",
                "someExtraField": "x",
                "query_params": {"$bogus": 1, "filter": "name eq 'a'"},
            },
            strict=True,
        )
    assert exc.value.code == "unknown_parameter"
    # Offending keys are named with their source; accepted names are listed per bucket.
    assert "someExtraField" in exc.value.detail
    assert "query_params.$bogus" in exc.value.detail
    assert "query_params.filter" not in exc.value.detail
    assert "path_params: [vmId]" in exc.value.detail
    assert "query_params: [$filter]" in exc.value.detail


def test_validate_operation_request_ignores_unknown_keys_when_not_strict() -> None:
    generator = ToolGenerator([_operation()])
    generator.validate_namespace_operation_request(
        namespace="vmm",
        operation="getVmById",
        request_payload={
            "operation": "getVmById",
            "someExtraField": "x",
            "query_params": {"$bogus": 1},
        },
        strict=False,
    )  # must NOT raise


def test_validate_operation_request_accepts_bucket_aliases_and_header_case() -> None:
    operation = _operation()
    operation.parameters.append(ParameterInfo(name="If-Match", location="header", required=True))
    generator = ToolGenerator([operation])
    generator.validate_namespace_operation_request(
        namespace="vmm",
        operation="getVmById",
        request_payload={
            "operation": "getVmById",
            "path_params": {"vmId": "1234"},
            "query_params": {"_filter": "name eq 'a'"},
            "headers": {"if-match": "etag-1"},
        },
    )


def test_validate_operation_request_rejects_non_object_bucket() -> None:
    generator = ToolGenerator([_operation()])
    with pytest.raises(ToolContractError) as exc:
        generator.validate_namespace_operation_request(
            namespace="vmm",
            operation="getVmById",
            request_payload={"operation": "getVmById", "query_params": "$limit=5"},
        )
    assert exc.value.code == "invalid_parameters"


def test_namespace_tool_declares_parameter_buckets() -> None:
    schema = ToolGenerator([_operation()]).build_namespace_tools()[0]["inputSchema"]
    for bucket in ("path_params", "query_params", "headers"):
        assert schema["properties"][bucket]["type"] == "object"
        assert schema["properties"][bucket]["additionalProperties"] is True
    # Legacy flat-key clients still need top-level additionalProperties.
    assert schema["additionalProperties"] is True


def test_operation_schema_includes_how_to_pass() -> None:
    schema = ToolGenerator([_operation()]).get_operation_schema("getVmById")
    assert schema["how_to_pass"] == {
        "path_parameters": "path_params",
        "query_parameters": "query_params",
        "header_parameters": "headers",
    }


def test_validate_operation_request_rejects_non_object_body() -> None:
    generator = ToolGenerator([_operation()])
    with pytest.raises(ToolContractError) as exc:
        generator.validate_namespace_operation_request(
            namespace="vmm",
            operation="getVmById",
            request_payload={"operation": "getVmById", "request_body": "bad"},
        )
    assert exc.value.code == "invalid_parameters"
