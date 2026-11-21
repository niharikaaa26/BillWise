from fastapi.testclient import TestClient

from billwise.api import MAX_BODY_BYTES, MAX_TEXT_CHARS, app

client = TestClient(app)

BILL = """Patient Name: DOE, JANE
Member ID: W123456789
Service Date | Code  | Description  | Billed  | Allowed | Plan Paid | You Owe
03/14/2025   | 99213 | Office visit | $250.00 | $140.00 | $112.00   | $28.00
"""


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_redact_endpoint_hides_values_and_mapping():
    r = client.post("/redact", json={"text": BILL})
    assert r.status_code == 200
    body = r.json()
    assert "W123456789" not in r.text and "DOE" not in r.text
    assert body["counts"]["NAME"] == 1 and body["counts"]["MEMBER_ID"] == 1
    assert {"type", "start", "end", "placeholder"} == set(body["entities"][0])


def test_parse_endpoint():
    r = client.post("/parse", json={"text": BILL})
    assert r.status_code == 200
    items = r.json()["line_items"]
    assert len(items) == 1 and items[0]["code"] == "99213" and items[0]["patient_responsibility"] == "28.00"


def test_rejects_empty_and_blank():
    assert client.post("/redact", json={"text": ""}).status_code == 422
    assert client.post("/parse", json={"text": "   \n "}).status_code == 422


def test_rejects_wrong_shape():
    assert client.post("/parse", json={"txt": "x"}).status_code == 422
    assert client.post("/parse", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_rejects_nul_bytes():
    assert client.post("/parse", json={"text": "a\x00b"}).status_code == 422


def test_rejects_too_many_chars():
    assert client.post("/redact", json={"text": "a" * (MAX_TEXT_CHARS + 1)}).status_code == 422


def test_rejects_oversized_body():
    big = b'{"text": "' + b"a" * (MAX_BODY_BYTES + 10) + b'"}'
    r = client.post("/redact", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
