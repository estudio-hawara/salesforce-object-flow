"""Tests for ``services/composite.py:CompositeExecutor`` and ``export_failures_csv``."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from salesforce_object_flow.core.composite import (
    BodyField,
    CompositeTemplate,
    HttpMethod,
    Subrequest,
)
from salesforce_object_flow.core.credentials import OrgCredentials
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.services.api import ApiError, SalesforceClient
from salesforce_object_flow.services.composite import (
    CompositeExecutor,
    CompositePayloadRenderer,
    ExecutionError,
    ExecutionReport,
    ProgressEvent,
    RenderRow,
    RowResult,
    SalesforceError,
    SubrequestResult,
    export_failures_csv,
)

INSTANCE = "https://acme.my.salesforce.com"
API_VERSION = "v63.0"
COMPOSITE_PATH = f"/services/data/{API_VERSION}/composite"


# =====================================================================
# Helpers
# =====================================================================


Handler = Callable[[httpx.Request], httpx.Response]


def _client(
    handler: Handler,
    *,
    refresh_fn: Callable[[], OrgCredentials] | None = None,
    on_token_refresh: Callable[[OrgCredentials], None] | None = None,
) -> SalesforceClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    creds = OrgCredentials(instance_url=INSTANCE, access_token="AT", refresh_token="RT")
    return SalesforceClient(
        creds=creds,
        api_version=API_VERSION,
        client=http,
        refresh_fn=refresh_fn,
        on_token_refresh=on_token_refresh,
    )


def _fmt(*column_specs: tuple[str, ColumnType]) -> FileFormat:
    return FileFormat(
        name="customer",
        columns=[Column(name=name, type=type_) for name, type_ in column_specs],
    )


def _tpl(
    *,
    all_or_none: bool = True,
    subrequests: list[Subrequest] | None = None,
    format_filename: str = "customer.json",
) -> CompositeTemplate:
    if subrequests is None:
        subrequests = [
            Subrequest(
                reference_id="newAccount",
                method=HttpMethod.POST,
                url=f"/services/data/{API_VERSION}/sobjects/Account",
                body=[BodyField(field="Name", value="{{name}}")],
            )
        ]
    return CompositeTemplate(
        name="T",
        format_filename=format_filename,
        all_or_none=all_or_none,
        subrequests=subrequests,
    )


def _write_csv(path: Path, lines: list[str], encoding: str = "utf-8") -> None:
    path.write_text("\n".join(lines) + "\n", encoding=encoding)


def _success_response(
    reference_id: str = "newAccount", record_id: str = "001x000"
) -> dict[str, Any]:
    return {
        "compositeResponse": [
            {
                "body": {"id": record_id, "success": True, "errors": []},
                "httpHeaders": {},
                "httpStatusCode": 201,
                "referenceId": reference_id,
            }
        ]
    }


def _failure_response(
    *,
    reference_id: str = "newAccount",
    error_code: str = "REQUIRED_FIELD_MISSING",
    message: str = "Required field missing.",
    fields: list[str] | None = None,
    http_status: int = 400,
) -> dict[str, Any]:
    return {
        "compositeResponse": [
            {
                "body": [
                    {
                        "errorCode": error_code,
                        "message": message,
                        "fields": fields or [],
                    }
                ],
                "httpHeaders": {},
                "httpStatusCode": http_status,
                "referenceId": reference_id,
            }
        ]
    }


def _no_progress(_event: ProgressEvent) -> None:
    return None


# =====================================================================
# Executor.run tests
# =====================================================================


def test_run_with_zero_rows_returns_empty_report(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "empty.csv"
    _write_csv(csv_path, ["name"])  # only header

    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("No POST should be made for an empty CSV.")

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert report.total == 0
    assert report.succeeded == 0
    assert report.failed == 0
    assert report.cancelled is False
    assert report.rows == ()


def test_run_with_one_successful_row(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "one.csv"
    _write_csv(csv_path, ["name", "Alice"])

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == COMPOSITE_PATH
        body = json.loads(req.content)
        assert body["compositeRequest"][0]["body"] == {"Name": "Alice"}
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )
    assert report.total == 1
    assert report.succeeded == 1
    assert report.failed == 0
    assert report.rows[0].subrequest_results[0].http_status == 201
    assert report.rows[0].csv_row == {"name": "Alice"}


def test_run_with_three_rows_one_failure(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "three.csv"
    _write_csv(csv_path, ["name", "Alice", "Bob", "Carol"])

    counter = {"i": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        counter["i"] += 1
        if counter["i"] == 2:
            return httpx.Response(
                200,
                json=_failure_response(
                    error_code="REQUIRED_FIELD_MISSING",
                    message="Name is required.",
                    fields=["Name"],
                ),
            )
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    failed_row = report.rows[1]
    assert failed_row.status == "failure"
    assert "REQUIRED_FIELD_MISSING" in (failed_row.error_summary or "")


def test_all_or_none_processing_halted_chooses_real_error(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "one.csv"
    _write_csv(csv_path, ["name", "Alice"])

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "compositeResponse": [
                    {
                        "body": [
                            {
                                "errorCode": "PROCESSING_HALTED",
                                "message": "Processing halted",
                                "fields": [],
                            }
                        ],
                        "httpHeaders": {},
                        "httpStatusCode": 400,
                        "referenceId": "newAccount",
                    },
                    {
                        "body": [
                            {
                                "errorCode": "DUPLICATE_VALUE",
                                "message": "duplicate",
                                "fields": [],
                            }
                        ],
                        "httpHeaders": {},
                        "httpStatusCode": 400,
                        "referenceId": "newContact",
                    },
                ]
            },
        )

    tpl = _tpl(
        subrequests=[
            Subrequest(
                reference_id="newAccount",
                method=HttpMethod.POST,
                url=f"/services/data/{API_VERSION}/sobjects/Account",
                body=[BodyField(field="Name", value="{{name}}")],
            ),
            Subrequest(
                reference_id="newContact",
                method=HttpMethod.POST,
                url=f"/services/data/{API_VERSION}/sobjects/Contact",
                body=[BodyField(field="LastName", value="{{name}}")],
            ),
        ]
    )

    executor = CompositeExecutor()
    report = executor.run(
        tpl,
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert report.total == 1
    assert report.failed == 1
    summary = report.rows[0].error_summary or ""
    assert "DUPLICATE_VALUE" in summary
    assert "PROCESSING_HALTED" not in summary


def test_cancellation_after_second_row_returns_partial_report(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "five.csv"
    _write_csv(csv_path, ["name", "A", "B", "C", "D", "E"])
    cancel = threading.Event()
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body["compositeRequest"][0]["body"]["Name"])
        return httpx.Response(200, json=_success_response())

    def progress(event: ProgressEvent) -> None:
        if event.processed == 2:
            cancel.set()

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=progress,
        cancelled=cancel,
    )

    assert report.cancelled is True
    assert len(report.rows) == 2
    assert seen == ["A", "B"]


def test_run_csv_without_header(tmp_path: Path) -> None:
    fmt = FileFormat(
        name="x",
        has_header=False,
        columns=[Column(name="name", type=ColumnType.STRING)],
    )
    csv_path = tmp_path / "noheader.csv"
    _write_csv(csv_path, ["Alice", "Bob"])

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["compositeRequest"][0]["body"]["Name"])
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )
    assert report.total == 2
    assert seen == ["Alice", "Bob"]


def test_run_csv_with_latin1_encoding(tmp_path: Path) -> None:
    fmt = FileFormat(
        name="x",
        encoding="latin-1",
        columns=[Column(name="name", type=ColumnType.STRING)],
    )
    csv_path = tmp_path / "latin.csv"
    _write_csv(csv_path, ["name", "Beñat", "Aimé"], encoding="latin-1")

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["compositeRequest"][0]["body"]["Name"])
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )
    assert report.total == 2
    assert seen == ["Beñat", "Aimé"]


def test_run_with_malformed_row_marks_failure_without_post(tmp_path: Path) -> None:
    fmt = _fmt(("a", ColumnType.STRING), ("b", ColumnType.STRING))
    csv_path = tmp_path / "mismatch.csv"
    _write_csv(csv_path, ["a,b", "x,y", "only-one"])

    posted: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append(req.url.path)
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert report.total == 2
    assert report.succeeded == 1
    assert report.failed == 1
    assert "Column count mismatch" in (report.rows[1].error_summary or "")
    assert len(posted) == 1


def test_run_with_5xx_transient_marks_row_failure(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "three.csv"
    _write_csv(csv_path, ["name", "A", "B", "C"])

    counter = {"i": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        counter["i"] += 1
        if counter["i"] == 2:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    assert "503" in (report.rows[1].error_summary or "")


def test_run_with_401_triggers_client_refresh_and_retries(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "one.csv"
    _write_csv(csv_path, ["name", "Alice"])

    counter = {"composite_calls": 0, "refresh_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        counter["composite_calls"] += 1
        token = req.headers.get("Authorization", "")
        if token != "Bearer AT_NEW":
            return httpx.Response(401, json={"error": "invalid_grant"})
        return httpx.Response(200, json=_success_response())

    def refresh_fn() -> OrgCredentials:
        counter["refresh_calls"] += 1
        return OrgCredentials(instance_url=INSTANCE, access_token="AT_NEW", refresh_token="RT")

    executor = CompositeExecutor()
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler, refresh_fn=refresh_fn),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )

    assert counter["refresh_calls"] == 1
    assert counter["composite_calls"] == 2
    assert report.succeeded == 1
    assert report.failed == 0


def test_run_with_failed_refresh_aborts_with_execution_error(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "one.csv"
    _write_csv(csv_path, ["name", "Alice"])

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    def refresh_fn() -> OrgCredentials:
        raise ApiError("refresh failed", status_code=401)

    executor = CompositeExecutor()
    with pytest.raises(ExecutionError, match="Authentication failed"):
        executor.run(
            _tpl(),
            fmt,
            csv_path,
            _client(handler, refresh_fn=refresh_fn),
            on_progress=_no_progress,
            cancelled=threading.Event(),
        )


def test_render_failure_marks_row_failure_without_aborting(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    csv_path = tmp_path / "three.csv"
    _write_csv(csv_path, ["name", "A", "B", "C"])

    class _BoomRenderer(CompositePayloadRenderer):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        def render(self, tpl: CompositeTemplate, fmt: FileFormat, row: RenderRow) -> dict[str, Any]:
            self._calls += 1
            if self._calls == 2:
                raise RuntimeError("boom")
            return super().render(tpl, fmt, row)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response())

    executor = CompositeExecutor(renderer=_BoomRenderer())
    report = executor.run(
        _tpl(),
        fmt,
        csv_path,
        _client(handler),
        on_progress=_no_progress,
        cancelled=threading.Event(),
    )
    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    assert "Render error" in (report.rows[1].error_summary or "")


# =====================================================================
# export_failures_csv tests
# =====================================================================


def _report_with_one_failure() -> ExecutionReport:
    fail_sub = SubrequestResult(
        reference_id="newAccount",
        http_status=400,
        body=None,
        errors=(SalesforceError("REQUIRED_FIELD_MISSING", "Name", ("Name",)),),
    )
    rows = (
        RowResult(
            row_index=0,
            csv_row={"name": "Alice", "city": "Paris"},
            status="success",
            subrequest_results=(
                SubrequestResult(
                    reference_id="newAccount",
                    http_status=201,
                    body={"id": "001"},
                    errors=(),
                ),
            ),
            error_summary=None,
        ),
        RowResult(
            row_index=1,
            csv_row={"name": "", "city": "London"},
            status="failure",
            subrequest_results=(fail_sub,),
            error_summary="newAccount: REQUIRED_FIELD_MISSING: Name",
        ),
        RowResult(
            row_index=2,
            csv_row={"name": "Carol", "city": "Madrid"},
            status="success",
            subrequest_results=(
                SubrequestResult(
                    reference_id="newAccount",
                    http_status=201,
                    body={"id": "002"},
                    errors=(),
                ),
            ),
            error_summary=None,
        ),
    )
    return ExecutionReport(total=3, succeeded=2, failed=1, cancelled=False, rows=rows)


def test_export_failures_csv_writes_failed_rows_with_error_column(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING), ("city", ColumnType.STRING))
    out = tmp_path / "failures.csv"
    written = export_failures_csv(_report_with_one_failure(), fmt, out)

    assert written == 1
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "name,city,_error"
    assert lines[1] == ",London,newAccount: REQUIRED_FIELD_MISSING: Name"


def test_export_failures_csv_with_no_failures_writes_only_header(tmp_path: Path) -> None:
    fmt = _fmt(("name", ColumnType.STRING))
    out = tmp_path / "no-failures.csv"
    report = ExecutionReport(total=0, succeeded=0, failed=0, cancelled=False, rows=())

    written = export_failures_csv(report, fmt, out)

    assert written == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines == ["name,_error"]
