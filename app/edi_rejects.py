"""HD's EDI 864 error messages -- what Home Depot sends back when it rejects an ASN
or an invoice, in HD's own words.

An 864 is a teletype page: boilerplate first (who to call, what to do), then one line
per error, each beginning with the rejected document's own number.

    ASN  NUMBER          E R R O R M E S S A G E
    ASN1715250 E114-ASN rejected due to duplicate ASN #
    ASN1715250 E712-ASN failed due to duplicate UCC128 code send --UCC128=000062794...

HD writes one line per offending carton, so a single reject can run to thirty lines of
the same code; `reject_note` collapses them by code and keeps HD's wording verbatim --
what HD said, not what we think it meant.

CRSTL mapped 864s for our account around 2026-09-24; before that the detail call 400s
("Mapping not configured"), which is why every reader here tolerates an empty body.
Pure: nothing decides anything -- app.alerts turns these into alert rows.
"""
import re

# An error line: the document number, then HD's message. The column header
# ("ASN  NUMBER  E R R O R ...") has no digits in its first token, so it never matches.
DOC_ERROR = re.compile(r"^(?P<doc>[A-Z]{2,4}\d{5,})\s+(?P<message>\S.*)$")
ERROR_CODE = re.compile(r"^([A-Z]{1,3}\d{2,4})\b")

ASN, INVOICE = "asn", "invoice"


def message_lines(detail: dict | None) -> list[str]:
    """Every line of free text in an 864, in order, from a CRSTL transaction detail."""
    gje = ((detail or {}).get("file") or {}).get("generic_json_edi") or {}
    out = []
    for loop in (gje.get("detail") or {}).get("message_identification_loop") or []:
        for msg in loop.get("message_text") or []:
            text = str(msg.get("free_form_message_text") or "").strip()
            if text:
                out.append(text)
    return out


def parse_864(detail: dict | None) -> dict:
    """{"kind": "asn"|"invoice"|"", "documents": {document number: [HD's messages]}}.

    `kind` comes from HD's own banner ("HOME DEPOT EDI ASN REJECT ERRORS" /
    "HOME DEPOT EDI INVOICE ERRORS"), falling back to the document number's prefix.
    An 864 we cannot read at all comes back with no documents -- the caller reports
    that rather than going quiet."""
    lines = message_lines(detail)
    documents: dict[str, list[str]] = {}
    for line in lines:
        m = DOC_ERROR.match(line)
        if m:
            documents.setdefault(m.group("doc"), []).append(m.group("message").strip())
    banner = (lines[0] if lines else "").upper()
    kind = INVOICE if "INVOICE" in banner else ASN if "ASN" in banner else ""
    if not kind and documents:
        kind = INVOICE if next(iter(documents)).upper().startswith("INV") else ASN
    return {"kind": kind, "documents": documents}


def reject_note(messages: list[str], *, limit: int = 3, width: int = 90) -> str:
    """HD's messages for one document as a single line: one entry per error code, HD's
    own text (trimmed at `width`), repeats counted rather than repeated."""
    groups: dict[str, list[str]] = {}
    for msg in messages:
        code = ERROR_CODE.match(msg)
        groups.setdefault(code.group(1) if code else msg[:8], []).append(msg)
    parts = []
    for msgs in list(groups.values())[:limit]:
        text = msgs[0]
        if len(text) > width:
            text = text[:width - 1].rstrip() + "…"
        parts.append(f"{text} (×{len(msgs)})" if len(msgs) > 1 else text)
    if len(groups) > limit:
        parts.append(f"+{len(groups) - limit} more")
    return "; ".join(parts)
