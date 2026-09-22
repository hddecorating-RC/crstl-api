"""TXI tax codes on dropship 810s: ST is provincial sales tax generically -- QST in
Quebec, PST anywhere else (read at 7% on BC and 6% on SK; HD pays neither)."""
from app.report import txi_kind


def test_st_is_qst_only_in_quebec():
    assert txi_kind("ST", "QC") == "QST"
    assert txi_kind("ST", "BC") == "PST"
    assert txi_kind("ST", "SK") == "PST"
    assert txi_kind("ST", "") == "PST"


def test_other_txi_codes_are_province_independent():
    assert txi_kind("CG", "AB") == "GST"
    assert txi_kind("VA", "ON") == "HST"
    assert txi_kind("XX", "ON") == "XX"
    assert txi_kind(None, "ON") == "(no code)"
