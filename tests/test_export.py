import io
import csv
from app.export import build_csv

INVOICES = [
    {
        "invoice_number": "INV-001",
        "po_number": "PO-123",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-01",
        "due_date": "2026-08-01",
        "status": "Open",
        "subtotal": 4180.0,
        "tax_amount": 320.0,
        "total_amount": 4500.0,
        "currency": "USD",
        "transaction_id": "tx-001",
        "created_at": "2026-07-01T00:00:00Z",
        "invoice_lines": [],
    },
    {
        "invoice_number": "INV-002",
        "po_number": "PO-456",
        "trading_partner": "Home Depot",
        "invoice_date": "2026-07-02",
        "due_date": "2026-08-02",
        "status": "Completed",
        "subtotal": 1950.0,
        "tax_amount": 150.0,
        "total_amount": 2100.0,
        "currency": "USD",
        "transaction_id": "tx-002",
        "created_at": "2026-07-02T00:00:00Z",
        "invoice_lines": [],
    },
]


def test_build_csv_returns_bytes():
    result = build_csv(INVOICES)
    assert isinstance(result, bytes)


def test_build_csv_has_header():
    result = build_csv(INVOICES)
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    header = next(reader)
    assert "Invoice Number" in header
    assert "Status" in header
    assert "Tax Amount" in header


def test_build_csv_row_count():
    result = build_csv(INVOICES)
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 3  # header + 2 data rows


def test_build_csv_subset_by_ids():
    result = build_csv(INVOICES, ids=["tx-001"])
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 2  # header + 1 row
    assert rows[1][0] == "INV-001"


def test_build_csv_empty():
    result = build_csv([])
    reader = csv.reader(io.StringIO(result.decode("utf-8")))
    rows = list(reader)
    assert len(rows) == 1  # header only


def test_build_csv_emits_per_code_columns():
    invoices = [
        {
            "invoice_number": "INV1",
            "province": "ON",
            "subtotal": 48.0, "allowance_amount": 0.6, "charge_amount": 0.0,
            "tax_amount": 6.16, "total_amount": 53.56,
            "allowances_charges": [
                {"type": "Allowance", "code": "C300", "amount": 0.60},
            ],
        },
        {
            "invoice_number": "INV2",
            "province": "AB",
            "subtotal": 10273.56, "allowance_amount": 590.73, "charge_amount": 484.14,
            "tax_amount": 0.0, "total_amount": 10166.97,
            "allowances_charges": [
                {"type": "Allowance", "code": "E210", "amount": 359.57},
                {"type": "Allowance", "code": "H090", "amount": 102.74},
                {"type": "Allowance", "code": "H000", "amount": 128.42},
                {"type": "Charge",    "code": "D360", "amount": 484.14},
            ],
        },
    ]
    rows = list(csv.DictReader(io.StringIO(build_csv(invoices).decode("utf-8"))))

    # Union of codes present as columns, sorted alpha (no duplicates even if a
    # code shows up as both allowance and charge across different invoices)
    header_codes = [k for k in rows[0].keys() if k.startswith("Code ")]
    assert header_codes == ["Code C300", "Code D360", "Code E210", "Code H000", "Code H090"]

    # Row 1: only C300 has a value (negative), other code cells blank
    assert rows[0]["Code C300"] == "-0.6"
    assert rows[0]["Code E210"] == ""
    assert rows[0]["Code D360"] == ""

    # Row 2: allowances negative, charge positive
    assert rows[1]["Code E210"] == "-359.57"
    assert rows[1]["Code H090"] == "-102.74"
    assert rows[1]["Code H000"] == "-128.42"
    assert rows[1]["Code D360"] == "484.14"
    assert rows[1]["Code C300"] == ""
