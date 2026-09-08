"""
Assemble a NetSuite REST `invoice` body from the flat line records that
app.netsuite.transform_invoice emits (all of which share one external_id).

transform_invoice already does the business mapping -- customer, tax code,
merchandise line plus one line per allowance/charge. This module only reshapes
those flat rows into the JSON body NetSuite's REST Record API expects, and keeps
that reshaping separate so the transport (app.netsuite_client) stays generic.

VERIFY BEFORE GOING LIVE: the field names below follow NetSuite's REST invoice
schema, but the exact reference shapes -- whether `item`, `taxCode` and
`currency` want {"id": ...} (internal id), {"externalId": ...}, or
{"refName": ...} -- depend on how this NetSuite account is set up and can only
be confirmed against the sandbox. transform_invoice carries human-readable names
(e.g. item "Merchandise Sales", tax_code "CA-HST ONT"), so refName is the
starting assumption; swap to id refs if the sandbox rejects names. This is the
one seam that needs a real credential to close.
"""


def build_invoice_payload(line_records: list[dict]) -> dict:
    """One NetSuite REST invoice body from transform_invoice's line records.

    Raises ValueError on an empty list. The header fields are read from the first
    record (all records share them); every record becomes one item line.
    """
    if not line_records:
        raise ValueError("build_invoice_payload needs at least one line record")

    head = line_records[0]
    return {
        "externalId": head["external_id"],
        "entity": {"id": head["customer_id"]},
        "tranDate": head["tran_date"],
        "dueDate": head["due_date"],
        "memo": head["memo"],
        "otherRefNum": head["other_ref_num"],
        "currency": {"refName": head["currency"]},
        "item": {
            "items": [
                {
                    "item": {"refName": r["item"]},
                    "description": r.get("description", ""),
                    "quantity": r["quantity"],
                    "rate": r["rate"],
                    "amount": r["amount"],
                    "taxCode": {"refName": r["tax_code"]},
                }
                for r in line_records
            ]
        },
    }
