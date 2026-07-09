import json
import os
import pathlib
from datetime import datetime, timezone

import requests


class CrstlClient:
    def __init__(self, base_url: str, email: str, password: str, org_id: str = "", token_cache_path: str = ".tmp/crstl_tokens.json"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.org_id = org_id
        self.token_cache_path = pathlib.Path(token_cache_path)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_access_token(self) -> str:
        tokens = self._load_cached_tokens()
        if self._token_is_valid(tokens):
            return tokens["access_token"]

        if tokens.get("refresh_token"):
            refreshed = self._refresh(tokens["refresh_token"])
            if refreshed:
                return refreshed

        return self._login()

    def _load_cached_tokens(self) -> dict:
        if self.token_cache_path.exists():
            try:
                return json.loads(self.token_cache_path.read_text())
            except Exception:
                pass
        return {}

    def _save_tokens(self, tokens: dict) -> None:
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_cache_path, "w", opener=lambda p, f: os.open(p, f, 0o600)) as fh:
            fh.write(json.dumps(tokens, indent=2))

    def _token_is_valid(self, tokens: dict) -> bool:
        expires_at = tokens.get("access_token_expires_at")
        if not expires_at or not tokens.get("access_token"):
            return False
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            return exp > datetime.now(timezone.utc)
        except Exception:
            return False

    def _refresh(self, refresh_token: str) -> str | None:
        resp = requests.post(
            f"{self.base_url}/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=30,
        )
        if resp.status_code == 200:
            tokens = resp.json()
            self._save_tokens(tokens)
            return tokens["access_token"]
        # Clear stale cache so next call goes straight to login
        self._save_tokens({})
        return None

    def _login(self) -> str:
        payload = {"email": self.email, "password": self.password}
        if self.org_id:
            payload["organization_id"] = self.org_id
        resp = requests.post(f"{self.base_url}/auth/token", json=payload, timeout=30)
        resp.raise_for_status()
        tokens = resp.json()
        self._save_tokens(tokens)
        return tokens["access_token"]

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_invoices(self) -> list[dict]:
        token = self.get_access_token()
        transactions = self._fetch_all_transactions(token)
        invoices = []
        for tx in transactions:
            tx_id = tx.get("id") or tx.get("transaction_id")
            if not tx_id:
                continue
            try:
                detail = self._fetch_transaction_detail(tx_id, token)
                invoices.append(self._extract_invoice_fields(detail))
            except requests.exceptions.HTTPError as exc:
                print(f"  WARNING: failed to fetch detail for {tx_id}: {exc}")
        return invoices

    def fetch_po_provinces(self) -> dict[str, dict]:
        """
        Fetch all 850 POs and return {po_number: {"province": "ON", "store": "VAUGHAN"|None}}.
        store is set for wholesale orders (identified by ship-to name containing VAUGHAN or CALGARY).
        province is the 2-letter Canadian province code from the ship-to address.
        """
        token = self.get_access_token()
        pos = self._fetch_all_transactions(token, transaction_type="850")
        result = {}
        for po in pos:
            po_id = po.get("id") or po.get("transaction_id")
            po_number = str(po.get("reference_id") or "")
            if not po_id or not po_number:
                continue
            try:
                detail = self._fetch_transaction_detail(po_id, token)
                tx_data = detail.get("transaction_data") or detail.get("document_data") or {}

                province = str(
                    tx_data.get("ship_to_party_state") or
                    tx_data.get("ship_to_state") or
                    detail.get("ship_to_state") or ""
                ).upper().strip()

                ship_to_name = str(
                    tx_data.get("ship_to_party_name") or
                    tx_data.get("ship_to_name") or
                    detail.get("ship_to_name") or ""
                ).upper()

                if not province:
                    continue

                store = None
                if "VAUGHAN" in ship_to_name:
                    store = "VAUGHAN"
                elif "CALGARY" in ship_to_name:
                    store = "CALGARY"

                result[po_number] = {"province": province, "store": store}
            except Exception as exc:
                print(f"WARNING: failed to fetch 850 detail for {po_number}: {exc}")
        return result

    def _fetch_all_transactions(self, token: str, transaction_type: str = "810") -> list:
        results = []
        params = {"transaction_type": transaction_type, "limit": 100}
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                f"{self.base_url}/transaction",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            inner = data.get("data", data)
            records = inner.get("transactions") or inner.get("results") or inner.get("items") or []
            results.extend(records)
            cursor = inner.get("next_cursor")
            if not cursor or len(records) < 100:
                break
        return results

    def _fetch_transaction_detail(self, transaction_id: str, token: str) -> dict:
        resp = requests.get(
            f"{self.base_url}/transaction/{transaction_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data)

    def _extract_invoice_fields(self, detail: dict) -> dict:
        tx_data = detail.get("transaction_data") or detail.get("document_data") or {}

        def first(*keys):
            for k in keys:
                for src in (tx_data, detail):
                    v = src.get(k)
                    if v is not None and v != "":
                        return v
            return None

        total_amount = self._parse_float(first("total_amount", "invoice_amount", "amount"))

        lines = tx_data.get("invoice_lines") or tx_data.get("line_items") or []
        subtotal = sum(
            self._parse_float(line.get("line_amount") or line.get("amount") or line.get("extended_amount"))
            for line in lines
        )

        tax_raw = first("tax_amount", "tax_total", "total_tax")
        tax_amount = self._parse_float(tax_raw)

        if tax_amount == 0.0 and subtotal > 0.0 and total_amount > subtotal:
            tax_amount = round(total_amount - subtotal, 2)
        if subtotal == 0.0:
            subtotal = round(total_amount - tax_amount, 2)

        return {
            "transaction_id":  str(detail.get("id") or detail.get("transaction_id") or ""),
            "invoice_number":  str(first("invoice_number", "document_number", "number") or ""),
            "po_number":       str(detail.get("reference_id") or first("purchase_order_number", "po_number") or ""),
            "trading_partner": str(detail.get("trading_partner_name") or first("trading_partner", "retailer") or ""),
            "invoice_date":    str(first("invoice_date", "issue_date", "date") or ""),
            "due_date":        str(first("due_date", "payment_due_date") or ""),
            "status":          str(detail.get("state") or detail.get("status") or first("status") or ""),
            "subtotal":        round(subtotal, 2),
            "tax_amount":      round(tax_amount, 2),
            "total_amount":    round(total_amount, 2),
            "currency":        str(first("currency", "currency_code") or "USD"),
            "created_at":      str(detail.get("created_at") or ""),
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
