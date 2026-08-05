from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

from app.sac_codes import classify


class CrstlClient:
    # Server enforces a 20-row page cap regardless of the `limit` param;
    # pagination is offset-based (no cursor). Keep PAGE_SIZE aligned with
    # the server's actual page size so the "short page = last page" heuristic works.
    PAGE_SIZE = 20
    MAX_WORKERS = 8  # empirically optimal — more workers get rate-limited by the server

    def __init__(self, base_url: str, api_key: str):
        if not api_key:
            raise ValueError("CRSTL_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"x-crstl-api-key": api_key, "Accept": "application/json"})
        adapter = HTTPAdapter(pool_connections=self.MAX_WORKERS, pool_maxsize=self.MAX_WORKERS)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def ping(self) -> dict:
        resp = self.session.get(f"{self.base_url}/ping", timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch_invoices(self) -> list[dict]:
        transactions = self._fetch_all_transactions()
        tx_ids = [tx.get("id") or tx.get("transaction_id") for tx in transactions]
        tx_ids = [tid for tid in tx_ids if tid]
        invoices = []
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_transaction_detail, tid): tid for tid in tx_ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    invoices.append(self._extract_invoice_fields(future.result()))
                except requests.exceptions.HTTPError as exc:
                    print(f"  WARNING: failed to fetch detail for {tid}: {exc}")
        return invoices

    def fetch_po_provinces(self) -> dict[str, dict]:
        """
        Fetch all 850 POs and return {po_number: {"province": "ON", "store": "VAUGHAN"|None}}.
        store is set for wholesale orders (identified by ship-to name containing VAUGHAN or CALGARY).
        province is the 2-letter Canadian province code from the ship-to address.
        """
        pos = self._fetch_all_transactions(transaction_type="850")
        # (po_id, po_number) pairs — skip anything missing either
        to_fetch = [
            (po.get("id") or po.get("metadata", {}).get("id"),
             str(po.get("reference_id") or po.get("metadata", {}).get("reference_id") or ""))
            for po in pos
        ]
        to_fetch = [(pid, pnum) for pid, pnum in to_fetch if pid and pnum]

        result = {}
        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
            futures = {pool.submit(self._fetch_transaction_detail, pid): (pid, pnum) for pid, pnum in to_fetch}
            for future in as_completed(futures):
                pid, pnum = futures[future]
                try:
                    detail = future.result()
                    ship_to = (
                        detail.get("file", {})
                              .get("generic_json_edi", {})
                              .get("heading", {})
                              .get("ship_to", {})
                    )
                    province = str(ship_to.get("state_province") or "").upper().strip()
                    if not province:
                        continue

                    ship_to_name = str(ship_to.get("name") or "").upper()
                    store = None
                    if "VAUGHAN" in ship_to_name:
                        store = "VAUGHAN"
                    elif "CALGARY" in ship_to_name:
                        store = "CALGARY"

                    result[pnum] = {"province": province, "store": store}
                except Exception as exc:
                    # Broad catch intentional: detail fetches can fail for structural reasons
                    # (malformed JSON, unexpected schema) beyond HTTP errors. Best-effort bulk fetch
                    # should skip bad records rather than abort the whole job.
                    print(f"WARNING: failed to fetch 850 detail for {pnum}: {exc}")
        return result

    def _fetch_all_transactions(self, transaction_type: str = "810") -> list:
        results = []
        offset = 0
        while True:
            resp = self.session.get(
                f"{self.base_url}/transaction",
                params={"transaction_type": transaction_type, "limit": self.PAGE_SIZE, "offset": offset},
                timeout=30,
            )
            resp.raise_for_status()
            inner = resp.json().get("data", {})
            records = inner.get("transactions") or []
            results.extend(records)
            if len(records) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE
        return results

    def _fetch_transaction_detail(self, transaction_id: str) -> dict:
        resp = self.session.get(
            f"{self.base_url}/transaction/{transaction_id}",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data)

    def _extract_invoice_fields(self, detail: dict) -> dict:
        """
        Extract invoice fields from the v2 detail response.
        Response shape: {metadata: {...}, file: {generic_json_edi: {heading, detail, summary}}}
        """
        meta = detail.get("metadata", {})
        gje = detail.get("file", {}).get("generic_json_edi", {})
        heading = gje.get("heading", {})
        summary = gje.get("summary", {})
        detail_body = gje.get("detail", {})

        raw_lines = detail_body.get("baseline_item_data_invoice_loop", []) or []
        lines = []
        subtotal = 0.0
        for entry in raw_lines:
            item = entry.get("baseline_item_data_invoice", {}) or {}
            qty = self._parse_float(item.get("quantity_invoiced"))
            unit_price = self._parse_float(item.get("unit_price"))
            amount = round(qty * unit_price, 2)
            subtotal += amount
            lines.append({
                "description": item.get("vendors_part_number") or f"Line {item.get('line_item_number', '')}",
                "quantity": qty,
                "unit_price": unit_price,
                "line_amount": amount,
                "uom": item.get("quantity_unit_code") or "EA",
            })

        # metadata.value is Crstl's canonical total; fall back to summary if absent
        total_amount = self._parse_float(
            meta.get("value") or summary.get("total_monetary_value_summary", {}).get("total_amount")
        )

        # Summary-level SAC entries. Per the HD 810 spec (v6.10), each SAC code
        # has a specific meaning — some indicator "C" codes are taxes (D360=GST,
        # H680=HST/QST, H850/F240/G090/G100=BC Eco Tax), some are freight, some
        # are fees. Classify each and sum into the appropriate bucket. tax_amount
        # is the sum of tax-categorized entries — NOT derived from the total.
        category_totals = {"allowance": 0.0, "discount": 0.0, "freight": 0.0, "fee": 0.0, "tax": 0.0}
        tax_breakdown = {}  # {"GST": amount, "HST_QST": amount, "ECO": amount}
        allowances_charges = []
        for entry in summary.get("service_promotion_allowance_or_charge_information_loop", []) or []:
            saci = entry.get("service_promotion_allowance_or_charge_information", {}) or {}
            amt = self._parse_float(saci.get("amount"))
            indicator = saci.get("allowance_or_charge_indicator")
            code = saci.get("service_promotion_allowance_or_charge_code") or ""

            meta = classify(code, indicator)
            category = meta["category"]
            category_totals[category] += amt
            if category == "tax":
                kind = meta.get("tax_kind", "OTHER")
                tax_breakdown[kind] = round(tax_breakdown.get(kind, 0.0) + amt, 2)

            allowances_charges.append({
                "type":     "Allowance" if indicator == "A" else "Charge" if indicator == "C" else indicator,
                "code":     code,
                "label":    meta["label"],
                "category": category,
                "amount":   round(amt, 2),
            })

        if subtotal == 0.0:
            subtotal = total_amount

        allowance_amount = round(category_totals["allowance"], 2)
        discount_amount  = round(category_totals["discount"],  2)
        freight_amount   = round(category_totals["freight"],   2)
        fee_amount       = round(category_totals["fee"],       2)
        # Tax is only ever what HD reports via SAC tax codes (D360/H680/H850/etc).
        # We do NOT derive tax from total − net_taxable — that would silently
        # hide invoices where HD's SAC data doesn't add up to the reported
        # total. Reconciliation is the whole point of this dashboard.
        tax_amount = round(category_totals["tax"], 2)

        # Discrepancy = the amount unexplained by the SAC data. Zero means the
        # invoice fully reconciles; non-zero flags a data quality issue
        # (missing tax SAC, missing allowance, or an HD-side computation error).
        # Positive = total exceeds what the SAC data explains (missing tax /
        # missing charge). Negative = SAC data exceeds the total (extra
        # deduction or double-counted allowance).
        computed_total = subtotal - allowance_amount - discount_amount + freight_amount + fee_amount + tax_amount
        discrepancy = round(total_amount - computed_total, 2)

        charge_amount = round(freight_amount + fee_amount + tax_amount, 2)

        state = meta.get("state") or {}
        status = state.get("value") if isinstance(state, dict) else state

        return {
            "transaction_id":  str(meta.get("id") or detail.get("id") or ""),
            "source_document_id":   str(meta.get("source_document_id") or ""),
            "source_document_type": str(meta.get("source_document_type") or ""),
            "invoice_number":  str(meta.get("reference_id") or heading.get("invoice_number") or ""),
            "po_number":       str(meta.get("source_document_reference_id") or heading.get("purchase_order_number") or ""),
            "trading_partner": str(meta.get("trading_partner_name") or ""),
            "invoice_date":    str(heading.get("invoice_date") or ""),
            "due_date":        str(heading.get("terms_net_due_date") or ""),
            "status":          str(status or ""),
            "subtotal":         round(subtotal, 2),
            "allowance_amount": allowance_amount,
            "discount_amount":  discount_amount,
            "freight_amount":   freight_amount,
            "fee_amount":       fee_amount,
            "charge_amount":    charge_amount,   # freight + fee + tax (legacy)
            "allowances_charges": allowances_charges,
            "tax_amount":       tax_amount,
            "tax_breakdown":    tax_breakdown,
            "discrepancy":      discrepancy,
            "total_amount":     round(total_amount, 2),
            "currency":        str(heading.get("currency", {}).get("currency_code") if isinstance(heading.get("currency"), dict) else heading.get("currency") or "CAD"),
            "created_at":      str(meta.get("created_at") or ""),
            "invoice_lines":   lines,
        }

    @staticmethod
    def _parse_float(val) -> float:
        if val is None:
            return 0.0
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return 0.0
