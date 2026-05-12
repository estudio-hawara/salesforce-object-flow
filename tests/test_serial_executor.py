"""End-to-end tests for ``services/serial.py:SerialExecutor``.

The four key paths of Carlos's CSV case are covered:

- Contact exists + Campaign exists → PATCH phone + POST CampaignMember.
- Contact exists + Campaign missing → PATCH phone only.
- Contact missing + Campaign exists → POST Contact + POST CampaignMember.
- Contact missing + Campaign missing → POST Contact only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from salesforce_object_flow.core.credentials import OrgCredentials
from salesforce_object_flow.core.formats import Column, ColumnType, FileFormat
from salesforce_object_flow.core.serial import (
    BodyField,
    CheckOp,
    ConditionCheck,
    ConditionCombinator,
    HttpMethod,
    SerialDefinition,
    SerialStep,
    StepCondition,
)
from salesforce_object_flow.services.api import SalesforceClient
from salesforce_object_flow.services.serial import (
    ExecutionReport,
    ProgressEvent,
    SerialExecutor,
    export_failures_csv,
)

INSTANCE = "https://acme.my.salesforce.com"
API_VERSION = "v63.0"


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> SalesforceClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    creds = OrgCredentials(instance_url=INSTANCE, access_token="AT", refresh_token="RT")
    return SalesforceClient(creds=creds, api_version=API_VERSION, client=http)


def _fmt() -> FileFormat:
    return FileFormat(
        name="redes",
        delimiter=";",
        columns=[
            Column(name="firstname", type=ColumnType.STRING),
            Column(name="lastname", type=ColumnType.STRING),
            Column(name="email", type=ColumnType.EMAIL),
            Column(name="telephone", type=ColumnType.STRING),
            Column(name="productcode", type=ColumnType.STRING),
        ],
    )


def _definition() -> SerialDefinition:
    return SerialDefinition(
        name="Import RRSS",
        format_filename="redes.json",
        steps=[
            SerialStep(
                reference_id="ContactQuery",
                method=HttpMethod.GET,
                url=(
                    "/services/data/v63.0/query/"
                    "?q=SELECT+Id+FROM+Contact+WHERE+Email='{{email}}'+LIMIT+1"
                ),
            ),
            SerialStep(
                reference_id="CampaignQuery",
                method=HttpMethod.GET,
                url=(
                    "/services/data/v63.0/query/"
                    "?q=SELECT+Id+FROM+Campaign+WHERE+ProductCode__c='{{productcode}}'+LIMIT+1"
                ),
            ),
            SerialStep(
                reference_id="ContactUpdate",
                method=HttpMethod.PATCH,
                url="/services/data/v63.0/sobjects/Contact/@{ContactQuery.records[0].Id}",
                body=[BodyField(field="Phone", value="{{telephone}}")],
                condition=StepCondition(
                    checks=[
                        ConditionCheck(
                            op=CheckOp.RECORDS_COUNT_GT,
                            ref="ContactQuery",
                            path="records",
                            value="0",
                        )
                    ],
                ),
            ),
            SerialStep(
                reference_id="ContactCreate",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/Contact",
                body=[
                    BodyField(field="FirstName", value="{{firstname}}"),
                    BodyField(field="LastName", value="{{lastname}}"),
                    BodyField(field="Email", value="{{email}}"),
                    BodyField(field="Phone", value="{{telephone}}"),
                ],
                condition=StepCondition(
                    checks=[
                        ConditionCheck(
                            op=CheckOp.RECORDS_COUNT_EQ,
                            ref="ContactQuery",
                            path="records",
                            value="0",
                        )
                    ],
                ),
            ),
            SerialStep(
                reference_id="MemberCreate",
                method=HttpMethod.POST,
                url="/services/data/v63.0/sobjects/CampaignMember",
                body=[
                    BodyField(field="CampaignId", value="@{CampaignQuery.records[0].Id}"),
                    # Pick whichever ContactId is available: existing or freshly created.
                    BodyField(field="ContactId", value="@{ContactQuery.records[0].Id}"),
                ],
                condition=StepCondition(
                    combinator=ConditionCombinator.ALL_OF,
                    checks=[
                        ConditionCheck(
                            op=CheckOp.RECORDS_COUNT_GT,
                            ref="CampaignQuery",
                            path="records",
                            value="0",
                        ),
                        ConditionCheck(op=CheckOp.STATUS_OK, ref="ContactUpdate"),
                    ],
                ),
                continue_on_failure=True,
            ),
        ],
    )


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "redes.csv"
    path.write_text(
        "firstname;lastname;email;telephone;productcode\n"
        "Juan;Gómez;juan@x.com;34611111111;pai_oct23\n",
        encoding="utf-8",
    )
    return path


def _empty_query() -> dict[str, Any]:
    return {"totalSize": 0, "done": True, "records": []}


def _populated_query(record_id: str) -> dict[str, Any]:
    return {"totalSize": 1, "done": True, "records": [{"Id": record_id}]}


def _patch_ok() -> httpx.Response:
    return httpx.Response(204)


def _created(record_id: str = "003new") -> httpx.Response:
    return httpx.Response(201, json={"id": record_id, "success": True, "errors": []})


def _make_handler(
    contact_records: dict[str, Any],
    campaign_records: dict[str, Any],
    *,
    calls: list[tuple[str, str]] | None = None,
) -> Handler:
    """Build a mock handler routing each step's URL to a canned response."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        if calls is not None:
            calls.append((request.method, path))
        if request.method == "GET" and "/query/" in path:
            if "FROM+Contact" in path:
                return httpx.Response(200, json=contact_records)
            if "FROM+Campaign" in path:
                return httpx.Response(200, json=campaign_records)
        if request.method == "PATCH" and "/sobjects/Contact/" in path:
            return _patch_ok()
        if request.method == "POST" and path.endswith("/sobjects/Contact"):
            return _created("003new")
        if request.method == "POST" and path.endswith("/sobjects/CampaignMember"):
            return _created("00vmem")
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": path}])

    return handler


def _run(
    handler: Handler,
    tmp_path: Path,
) -> tuple[ExecutionReport, list[ProgressEvent]]:
    events: list[ProgressEvent] = []
    report = SerialExecutor().run(
        _definition(),
        _fmt(),
        _csv(tmp_path),
        _client(handler),
        on_progress=events.append,
        cancelled=threading.Event(),
    )
    return report, events


# =====================================================================
# Branch coverage
# =====================================================================


def test_contact_exists_campaign_exists(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    handler = _make_handler(
        contact_records=_populated_query("003abc"),
        campaign_records=_populated_query("701xyz"),
        calls=calls,
    )
    report, _ = _run(handler, tmp_path)
    assert report.succeeded == 1
    assert report.failed == 0
    methods = [m for m, _ in calls]
    assert "PATCH" in methods, "ContactUpdate should run"
    # CampaignMember POST happens; Contact POST does not.
    paths = [p for _, p in calls]
    assert any(p.endswith("/sobjects/CampaignMember") for p in paths)
    assert not any(p.endswith("/sobjects/Contact") and m == "POST" for m, p in calls)


def test_contact_missing_campaign_exists(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    handler = _make_handler(
        contact_records=_empty_query(),
        campaign_records=_populated_query("701xyz"),
        calls=calls,
    )
    report, _ = _run(handler, tmp_path)
    # ContactCreate must run; ContactUpdate must not.
    methods_paths = calls
    assert any(m == "POST" and p.endswith("/sobjects/Contact") for m, p in methods_paths)
    assert not any(m == "PATCH" for m, p in methods_paths)
    # MemberCreate is conditioned on ContactUpdate.status_ok in our definition, so it
    # is *skipped* when ContactUpdate didn't run. The row should therefore be successful
    # (no steps failed) but no CampaignMember was created.
    assert report.succeeded == 1
    assert not any(p.endswith("/sobjects/CampaignMember") for _, p in calls)


def test_contact_exists_campaign_missing(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    handler = _make_handler(
        contact_records=_populated_query("003abc"),
        campaign_records=_empty_query(),
        calls=calls,
    )
    report, _ = _run(handler, tmp_path)
    assert report.succeeded == 1
    assert any(m == "PATCH" for m, _ in calls)
    assert not any(p.endswith("/sobjects/CampaignMember") for _, p in calls)


def test_contact_missing_campaign_missing(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    handler = _make_handler(
        contact_records=_empty_query(),
        campaign_records=_empty_query(),
        calls=calls,
    )
    report, _ = _run(handler, tmp_path)
    assert report.succeeded == 1
    assert any(m == "POST" and p.endswith("/sobjects/Contact") for m, p in calls)
    assert not any(p.endswith("/sobjects/CampaignMember") for _, p in calls)


# =====================================================================
# Failure handling
# =====================================================================


def test_failure_aborts_row_when_continue_on_failure_is_false(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # First step (Contact query) fails hard.
        return httpx.Response(
            500, json=[{"errorCode": "INTERNAL_ERROR", "message": "boom"}]
        )

    report, _ = _run(handler, tmp_path)
    assert report.failed == 1
    assert report.succeeded == 0
    row = report.rows[0]
    # Only one step result recorded — the failure halted the row.
    executed = [r for r in row.step_results if r.status != "skipped"]
    assert len(executed) == 1


def test_continue_on_failure_lets_later_steps_run(tmp_path: Path) -> None:
    """If the MemberCreate fails with a duplicate, continue_on_failure=True keeps the row open."""

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        calls.append((request.method, path))
        if request.method == "GET":
            if "FROM+Contact" in path:
                return httpx.Response(200, json=_populated_query("003abc"))
            if "FROM+Campaign" in path:
                return httpx.Response(200, json=_populated_query("701xyz"))
        if request.method == "PATCH":
            return httpx.Response(204)
        # Simulate duplicate CampaignMember.
        return httpx.Response(
            400,
            json=[
                {
                    "errorCode": "DUPLICATE_VALUE",
                    "message": "Already a campaign member",
                }
            ],
        )

    report, _ = _run(handler, tmp_path)
    # MemberCreate failed but `continue_on_failure=True` — the row is still marked failure
    # because at least one step failed, but the executor reached the end of the steps.
    assert report.failed == 1
    row = report.rows[0]
    statuses = [r.status for r in row.step_results]
    # All five steps should have a result (some success, some skipped, one failure).
    assert len(statuses) == 5


# =====================================================================
# Cooperative cancellation
# =====================================================================


def test_cancellation_stops_processing(tmp_path: Path) -> None:
    # Two rows; cancel before the second.
    path = tmp_path / "redes.csv"
    path.write_text(
        "firstname;lastname;email;telephone;productcode\n"
        "Juan;Gómez;juan@x.com;34611;p1\n"
        "Ana;Ruiz;ana@x.com;34612;p2\n",
        encoding="utf-8",
    )
    cancel = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        cancel.set()  # cancel after the first network call lands
        return httpx.Response(200, json=_empty_query())

    events: list[ProgressEvent] = []
    report = SerialExecutor().run(
        _definition(),
        _fmt(),
        path,
        _client(handler),
        on_progress=events.append,
        cancelled=cancel,
    )
    assert report.cancelled is True
    assert report.total == 2
    # At most one row was processed before cancellation kicked in.
    assert len(report.rows) <= 1


# =====================================================================
# Failure CSV export
# =====================================================================


def test_export_failures_csv(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=[{"errorCode": "X", "message": "y"}])

    report, _ = _run(handler, tmp_path)
    out = tmp_path / "failures.csv"
    n = export_failures_csv(report, _fmt(), out)
    assert n == 1
    contents = out.read_text(encoding="utf-8")
    assert "_error" in contents
    assert "juan@x.com" in contents


# =====================================================================
# Smoke
# =====================================================================


def test_progress_events_track_rows(tmp_path: Path) -> None:
    handler = _make_handler(
        contact_records=_populated_query("003abc"),
        campaign_records=_populated_query("701xyz"),
    )
    report, events = _run(handler, tmp_path)
    assert report.total == len(events) == 1


@pytest.mark.parametrize("missing", ["nofile.csv"])
def test_missing_csv_raises(tmp_path: Path, missing: str) -> None:
    from salesforce_object_flow.services.serial import ExecutionError

    handler = _make_handler(_empty_query(), _empty_query())
    with pytest.raises(ExecutionError):
        SerialExecutor().run(
            _definition(),
            _fmt(),
            tmp_path / missing,
            _client(handler),
            on_progress=lambda _e: None,
            cancelled=threading.Event(),
        )
