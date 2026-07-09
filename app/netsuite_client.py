"""
NetSuite REST API client — stub placeholder.
Replace with TBA-authenticated implementation once credentials are available.

Future interface:
    client = NetSuiteClient(account_id, consumer_key, consumer_secret, token_id, token_secret)
    client.upsert_invoice(record)   # PUT /invoice/eid:{external_id}
"""


class NetSuiteClient:
    def upsert_invoice(self, record: dict) -> dict:
        raise NotImplementedError(
            "NetSuite REST connector not yet configured. "
            "Use the CSV export path until TBA credentials are available."
        )
