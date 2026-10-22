import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "verify_outcomes", Path(__file__).parents[2] / "scripts" / "verify_outcomes.py"
)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def evidence():
    return {
        "requests": [
            {
                "id": "id",
                "status": "sent",
                "user_input": "Send email to test@example.org: ready",
                "created_at": 1,
                "activated_at": 2,
                "updated_at": 8,
                "failure_category": None,
                "failure_code": None,
                "repaired": 0,
                "provider_id": "p-1",
                "extracted_json": '{"to":"test@example.org","message":"ready","type":"email"}',
            }
        ],
        "attempts": [
            {
                "request_id": "id",
                "stage": "extract",
                "number": 1,
                "outcome": "succeeded",
                "started_at": 3,
                "finished_at": 6,
                "failure_category": None,
                "provider_id": None,
            },
            {
                "request_id": "id",
                "stage": "notify",
                "number": 1,
                "outcome": "succeeded",
                "started_at": 7,
                "finished_at": 8,
                "failure_category": None,
                "provider_id": "p-1",
            },
        ],
    }


def test_verifier_reconciles_count_payload_and_confirmation():
    report = verifier.assess(evidence(), 1)
    assert report["integrity_passed"]
    assert report["all_sent"]
    assert report["semantically_verified_sent"] == 1
    assert not verifier.assess(evidence(), 2)["integrity_passed"]
    assert not verifier.assess({"requests": [], "attempts": []}, 0)["integrity_passed"]


@pytest.mark.parametrize("defect", ["payload", "confirmation", "extraction", "refusal", "pending"])
def test_verifier_detects_false_success(defect):
    data = copy.deepcopy(evidence())
    if defect == "payload":
        data["requests"][0]["extracted_json"] = data["requests"][0]["extracted_json"].replace(
            "ready", "wrong"
        )
    elif defect == "confirmation":
        data["attempts"][1]["provider_id"] = "wrong"
    elif defect == "extraction":
        data["attempts"].pop(0)
    elif defect == "refusal":
        data["attempts"][0]["failure_category"] = "refusal"
    else:
        data["requests"][0]["status"] = "processing"
    assert not verifier.assess(data, 1)["integrity_passed"]


def test_legitimate_failure_is_reported_without_claiming_delivery():
    data = evidence()
    data["requests"][0].update(
        status="failed", failure_category="refusal", failure_code="ai_refused"
    )
    data["attempts"] = [data["attempts"][0]]
    data["attempts"][0].update(outcome="failed", failure_category="refusal")
    report = verifier.assess(data, 1)
    assert report["integrity_passed"]
    assert not report["all_sent"]
    assert report["failure_categories"] == {"refusal": 1}
