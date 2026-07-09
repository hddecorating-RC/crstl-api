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
