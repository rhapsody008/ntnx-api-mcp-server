"""Unit tests for runtime dispatch wiring."""

from __future__ import annotations

from typing import Any

from src.config import Settings
from src.parsers import OperationInfo, ParameterInfo
from src.server import StartupLoadResult
from src.tools import RuntimeToolDispatcher


def _build_dispatcher() -> RuntimeToolDispatcher:
    operation = OperationInfo(
        namespace="vmm",
        operation_id="getVmById",
        path="/vms/{vmId}",
        method="GET",
        summary="Get VM",
        description="Get VM by ID",
        parameters=[
            ParameterInfo(name="vmId", location="path", required=True),
            ParameterInfo(name="$limit", location="query", required=False),
        ],
        code_samples=[{"lang": "python", "source": "print('hello')"}],
        permissions={
            "operationName": "View VM",
            "roleList": [{"name": "Prism Viewer"}, {"name": "Prism Admin"}],
        },
        required_roles=["Prism Viewer", "Prism Admin"],
    )
    from src.generators import ToolGenerator
    load_result = StartupLoadResult(
        artifacts_source="runtime",
        artifact_directory=Settings().artifacts_dir,
        files=[],
        operations=[operation],
        namespace_tools=[],
        discovery_tools=[],
        operation_index={},
        generator=ToolGenerator([operation], schemas={}, namespace_metadata={}),
    )
    settings = Settings(pc_host="127.0.0.1", pc_port=9440)
    return RuntimeToolDispatcher(settings=settings, load_result=load_result)


def test_list_operations_helper() -> None:
    dispatcher = _build_dispatcher()
    result = dispatcher.call_tool("listOperations", {"namespace": "vmm"})
    assert result.ok is True
    assert len(result.payload) == 1
    assert result.payload[0]["operation"] == "getVmById"


def test_get_operation_schema_helper() -> None:
    dispatcher = _build_dispatcher()
    result = dispatcher.call_tool("getOperationSchema", {"operation": "getVmById"})
    assert result.ok is True
    # New structured shape: 'operation' holds the registered name.
    assert result.payload["operation"] == "getVmById"
    assert result.payload["method"] == "GET"
    assert result.payload["path"] == "/vms/{vmId}"


def test_get_code_sample_helper() -> None:
    dispatcher = _build_dispatcher()
    result = dispatcher.call_tool("getCodeSample", {"operation": "getVmById", "language": "python"})
    assert result.ok is True
    assert result.payload["lang"] == "python"


def test_get_operation_permissions_helper() -> None:
    dispatcher = _build_dispatcher()
    result = dispatcher.call_tool("getOperationPermissions", {"operation": "getVmById"})
    assert result.ok is True
    assert result.payload["permission_name"] == "View VM"
    assert "Prism Viewer" in result.payload["required_roles"]


def test_namespace_execute_with_odata_alias(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _build_dispatcher()

    captured = {}

    def _fake_execute_request(
        method, path, path_params=None, query_params=None, headers=None, body=None
    ):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["path"] = path
        captured["path_params"] = path_params
        captured["query_params"] = query_params
        captured["headers"] = headers
        captured["body"] = body
        return {"metadata": {"messages": []}, "data": [{"extId": "x"}]}

    monkeypatch.setattr(dispatcher.api_handler, "execute_request", _fake_execute_request)

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "getVmById",
            "vmId": "vm-123",
            "_limit": 10,
        },
    )

    assert result.ok is True
    assert captured["method"] == "GET"
    assert captured["path"] == "/vms/{vmId}"
    assert captured["path_params"]["vmId"] == "vm-123"
    assert captured["query_params"]["$limit"] == 10
    assert captured["body"] is None


def test_namespace_execute_for_post_with_request_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    operation = OperationInfo(
        namespace="vmm",
        operation_id="createVm",
        path="/vms",
        method="POST",
        summary="Create VM",
        description="Create VM",
        request_body={"content": {"application/json": {"schema": {"type": "object"}}}},
    )
    from src.generators import ToolGenerator
    load_result = StartupLoadResult(
        artifacts_source="runtime",
        artifact_directory=Settings().artifacts_dir,
        files=[],
        operations=[operation],
        namespace_tools=[],
        discovery_tools=[],
        operation_index={},
        generator=ToolGenerator([operation], schemas={}, namespace_metadata={}),
    )
    dispatcher = RuntimeToolDispatcher(
        settings=Settings(pc_host="127.0.0.1", pc_port=9440, read_only_mode=False),
        load_result=load_result,
    )

    captured = {}

    def _fake_execute_request(
        method, path, path_params=None, query_params=None, headers=None, body=None
    ):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"data": {"ok": True}}

    monkeypatch.setattr(dispatcher.api_handler, "execute_request", _fake_execute_request)

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "createVm",
            "request_body": {"name": "vm-1"},
        },
    )

    assert result.ok is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/vms"
    assert captured["body"] == {"name": "vm-1"}


def test_readonly_mode_blocks_non_get(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """read_only_mode=True must reject POST/PUT/PATCH/DELETE before reaching the API."""
    operation = OperationInfo(
        namespace="vmm",
        operation_id="createVm",
        path="/vms",
        method="POST",
        summary="Create VM",
        description="Create VM",
        request_body={"content": {"application/json": {"schema": {"type": "object"}}}},
    )
    from src.generators import ToolGenerator
    load_result = StartupLoadResult(
        artifacts_source="runtime",
        artifact_directory=Settings().artifacts_dir,
        files=[],
        operations=[operation],
        namespace_tools=[],
        discovery_tools=[],
        operation_index={},
        generator=ToolGenerator([operation], schemas={}, namespace_metadata={}),
    )
    dispatcher = RuntimeToolDispatcher(
        settings=Settings(pc_host="127.0.0.1", pc_port=9440, read_only_mode=True),
        load_result=load_result,
    )

    called = {"count": 0}

    def _should_not_be_called(**kwargs):  # type: ignore[no-untyped-def]
        called["count"] += 1
        return {}

    monkeypatch.setattr(dispatcher.api_handler, "execute_request", _should_not_be_called)

    result = dispatcher.call_tool(
        "vmm_execute",
        {"operation": "createVm", "request_body": {"name": "vm-1"}},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error["code"] == "read_only_mode"
    assert called["count"] == 0


# ── Declared parameter buckets ───────────────────────────────────────────────


def _list_vms_operation() -> OperationInfo:
    return OperationInfo(
        namespace="vmm",
        operation_id="listVms",
        path="/vmm/v4.2/ahv/config/vms",
        method="GET",
        summary="List VMs",
        description="List VMs",
        parameters=[
            ParameterInfo(name="$filter", location="query", required=False),
            ParameterInfo(name="$select", location="query", required=False),
            ParameterInfo(name="$limit", location="query", required=False),
        ],
    )


def _get_vm_operation() -> OperationInfo:
    return OperationInfo(
        namespace="vmm",
        operation_id="getVmById",
        path="/vmm/v4.2/ahv/config/vms/{extId}",
        method="GET",
        summary="Get VM",
        description="Get VM",
        parameters=[ParameterInfo(name="extId", location="path", required=True)],
    )


def _update_vm_operation() -> OperationInfo:
    return OperationInfo(
        namespace="vmm",
        operation_id="updateVmById",
        path="/vmm/v4.2/ahv/config/vms/{extId}",
        method="PUT",
        summary="Update VM",
        description="Update VM",
        parameters=[
            ParameterInfo(name="extId", location="path", required=True),
            ParameterInfo(name="If-Match", location="header", required=True),
        ],
        request_body={"content": {"application/json": {"schema": {"type": "object"}}}},
    )


def _associate_categories_operation() -> OperationInfo:
    return OperationInfo(
        namespace="vmm",
        operation_id="associateCategories",
        path="/vmm/v4.2/ahv/config/vms/{extId}/$actions/associate-categories",
        method="POST",
        summary="Associate categories",
        description="Associate categories",
        parameters=[
            ParameterInfo(name="extId", location="path", required=True),
            ParameterInfo(name="If-Match", location="header", required=True),
        ],
        request_body={"content": {"application/json": {"schema": {"type": "object"}}}},
    )


def _dispatcher_for(operations: list[OperationInfo], **settings_kwargs: Any) -> RuntimeToolDispatcher:
    from src.generators import ToolGenerator

    settings = Settings(pc_host="127.0.0.1", pc_port=9440, **settings_kwargs)
    generator = ToolGenerator(operations, schemas={}, namespace_metadata={}, auto_etag=settings.auto_etag)
    load_result = StartupLoadResult(
        artifacts_source="runtime",
        artifact_directory=settings.artifacts_dir,
        files=[],
        operations=operations,
        namespace_tools=generator.build_namespace_tools(),
        discovery_tools=generator.build_discovery_tools(),
        operation_index={},
        generator=generator,
    )
    return RuntimeToolDispatcher(settings=settings, load_result=load_result)


def _capture_requests(monkeypatch, dispatcher: RuntimeToolDispatcher, responses=None):  # type: ignore[no-untyped-def]
    """Record every execute_request call; return queued responses (default: empty data)."""
    calls: list[dict[str, Any]] = []
    queue = list(responses or [])

    def _fake_execute_request(
        method, path, path_params=None, query_params=None, headers=None, body=None
    ):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "method": method,
                "path": path,
                "path_params": path_params or {},
                "query_params": query_params or {},
                "headers": headers or {},
                "body": body,
            }
        )
        return queue.pop(0) if queue else {"data": []}

    monkeypatch.setattr(dispatcher.api_handler, "execute_request", _fake_execute_request)
    return calls


def test_namespace_tool_schema_declares_buckets() -> None:
    dispatcher = _dispatcher_for([_list_vms_operation()])
    tool = next(t for t in dispatcher.list_tools() if t["name"] == "vmm_execute")
    properties = tool["inputSchema"]["properties"]
    assert {"path_params", "query_params", "headers"} <= set(properties)
    assert all(properties[b]["type"] == "object" for b in ("path_params", "query_params", "headers"))


def test_execute_bucket_form(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_get_vm_operation(), _list_vms_operation()])
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "listVms",
            "query_params": {"$filter": "startswith(name,'zy-')", "$select": "extId,name", "$limit": 100},
        },
    )
    assert result.ok is True
    assert calls[0]["query_params"] == {
        "$filter": "startswith(name,'zy-')",
        "$select": "extId,name",
        "$limit": 100,
    }

    result = dispatcher.call_tool(
        "vmm_execute", {"operation": "getVmById", "path_params": {"extId": "vm-1"}}
    )
    assert result.ok is True
    assert calls[1]["path_params"] == {"extId": "vm-1"}


def test_execute_legacy_flat_form(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()])
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute", {"operation": "listVms", "$filter": "name eq 'a'", "_limit": 5}
    )
    assert result.ok is True
    assert calls[0]["query_params"] == {"$filter": "name eq 'a'", "$limit": 5}


def test_execute_alias_and_bare_names_inside_query_params(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()])
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute",
        {"operation": "listVms", "query_params": {"_filter": "name eq 'a'", "limit": 5}},
    )
    assert result.ok is True
    assert calls[0]["query_params"] == {"$filter": "name eq 'a'", "$limit": 5}


def test_execute_wrong_bucket_routes_by_spec_location(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_update_vm_operation()], read_only_mode=False)
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "updateVmById",
            # extId belongs in path_params and If-Match in headers; both are misplaced.
            "query_params": {"extId": "vm-1"},
            "path_params": {"If-Match": "etag-1"},
            "request_body": {"name": "vm"},
        },
    )
    assert result.ok is True
    assert calls[0]["path_params"] == {"extId": "vm-1"}
    assert calls[0]["query_params"] == {}
    assert calls[0]["headers"] == {"If-Match": "etag-1"}


def test_execute_bucket_value_overrides_flat_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()])
    calls = _capture_requests(monkeypatch, dispatcher)

    dispatcher.call_tool(
        "vmm_execute",
        {"operation": "listVms", "$limit": 5, "query_params": {"$limit": 50}},
    )
    assert calls[0]["query_params"] == {"$limit": 50}


def test_execute_unknown_parameter_is_rejected_before_api_call(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()])
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute", {"operation": "listVms", "query_params": {"$fliter": "name eq 'a'"}}
    )
    assert result.ok is False
    assert result.error["code"] == "unknown_parameter"
    assert "query_params.$fliter" in result.error["detail"]
    assert calls == []


def test_execute_unknown_parameter_dropped_when_not_strict(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()], strict_params=False)
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool("vmm_execute", {"operation": "listVms", "bogus": 1})
    assert result.ok is True
    assert calls[0]["query_params"] == {}


def test_execute_forwards_undeclared_if_match_on_writes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # vmm v4.3 omits If-Match on the AHV associate-categories action; it must still pass through.
    operation = _associate_categories_operation()
    operation.parameters = [p for p in operation.parameters if p.name != "If-Match"]
    dispatcher = _dispatcher_for([operation], read_only_mode=False)
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "associateCategories",
            "path_params": {"extId": "vm-1"},
            "headers": {"If-Match": "etag-1"},
            "request_body": {},
        },
    )
    assert result.ok is True
    assert calls[0]["headers"] == {"If-Match": "etag-1"}


def test_execute_rejects_if_match_on_get(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_list_vms_operation()])
    _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute", {"operation": "listVms", "headers": {"If-Match": "etag-1"}}
    )
    assert result.error["code"] == "unknown_parameter"


# ── Server-side ETag (AUTO_ETAG) ─────────────────────────────────────────────


def _etag_operations() -> list[OperationInfo]:
    return [_get_vm_operation(), _update_vm_operation(), _associate_categories_operation()]


def test_auto_etag_for_action_uses_parent_resource_get(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for(_etag_operations(), read_only_mode=False, auto_etag=True)
    calls = _capture_requests(
        monkeypatch, dispatcher, responses=[{"data": {"extId": "vm-1"}, "_etag": "etag-42"}, {"data": {}}]
    )

    result = dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "associateCategories",
            "path_params": {"extId": "vm-1"},
            "request_body": {"categories": [{"extId": "cat-1"}]},
        },
    )
    assert result.ok is True
    assert [c["method"] for c in calls] == ["GET", "POST"]
    assert calls[0]["path"] == "/vmm/v4.2/ahv/config/vms/{extId}"
    assert calls[0]["path_params"] == {"extId": "vm-1"}
    assert calls[1]["headers"]["If-Match"] == "etag-42"


def test_auto_etag_for_put_uses_own_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for(_etag_operations(), read_only_mode=False, auto_etag=True)
    calls = _capture_requests(monkeypatch, dispatcher, responses=[{"_etag": "etag-7"}, {"data": {}}])

    result = dispatcher.call_tool(
        "vmm_execute",
        {"operation": "updateVmById", "path_params": {"extId": "vm-1"}, "request_body": {"name": "x"}},
    )
    assert result.ok is True
    assert calls[0]["path"] == calls[1]["path"] == "/vmm/v4.2/ahv/config/vms/{extId}"
    assert calls[1]["headers"]["If-Match"] == "etag-7"


def test_auto_etag_respects_supplied_if_match(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for(_etag_operations(), read_only_mode=False, auto_etag=True)
    calls = _capture_requests(monkeypatch, dispatcher)

    dispatcher.call_tool(
        "vmm_execute",
        {
            "operation": "associateCategories",
            "path_params": {"extId": "vm-1"},
            "headers": {"if-match": "mine"},
            "request_body": {},
        },
    )
    assert [c["method"] for c in calls] == ["POST"]
    assert calls[0]["headers"] == {"If-Match": "mine"}


def test_auto_etag_disabled_does_not_fetch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for(_etag_operations(), read_only_mode=False, auto_etag=False)
    calls = _capture_requests(monkeypatch, dispatcher)

    dispatcher.call_tool(
        "vmm_execute",
        {"operation": "associateCategories", "path_params": {"extId": "vm-1"}, "request_body": {}},
    )
    assert [c["method"] for c in calls] == ["POST"]
    assert "If-Match" not in calls[0]["headers"]


def test_auto_etag_without_matching_get_proceeds_unchanged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for([_associate_categories_operation()], read_only_mode=False, auto_etag=True)
    calls = _capture_requests(monkeypatch, dispatcher)

    result = dispatcher.call_tool(
        "vmm_execute",
        {"operation": "associateCategories", "path_params": {"extId": "vm-1"}, "request_body": {}},
    )
    assert result.ok is True
    assert [c["method"] for c in calls] == ["POST"]
    assert "If-Match" not in calls[0]["headers"]


def test_auto_etag_missing_etag_returns_error_without_write(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dispatcher = _dispatcher_for(_etag_operations(), read_only_mode=False, auto_etag=True)
    calls = _capture_requests(
        monkeypatch, dispatcher, responses=[{"data": {"error": [{"code": "VMM-404"}]}}]
    )

    result = dispatcher.call_tool(
        "vmm_execute",
        {"operation": "associateCategories", "path_params": {"extId": "missing"}, "request_body": {}},
    )
    assert result.ok is False
    assert result.error["code"] == "auto_etag_failed"
    assert "VMM-404" in result.error["detail"]
    assert [c["method"] for c in calls] == ["GET"]


def test_operation_schema_if_match_guidance_reflects_auto_etag() -> None:
    on = _dispatcher_for(_etag_operations(), auto_etag=True)
    off = _dispatcher_for(_etag_operations(), auto_etag=False)

    def _if_match(dispatcher: RuntimeToolDispatcher) -> str:
        schema = dispatcher.call_tool("getOperationSchema", {"operation": "associateCategories"}).payload
        return next(h for h in schema["header_parameters"] if h["name"] == "If-Match")["auto_managed"]

    assert "Auto-fetched by the server" in _if_match(on)
    assert "headers: {'If-Match'" in _if_match(off)
