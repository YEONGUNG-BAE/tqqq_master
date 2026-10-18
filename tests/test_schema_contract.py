"""Offline request contract against the pinned official portal export."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from autotrader.core import D, Journal
from autotrader.meritz import Meritz

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT/"vendor/meritz-portal-20261015.json"   # not redistributed; download per README
DOC = json.loads(SPEC.read_text()) if SPEC.exists() else {"groups": []}
APIS = {(a["method"], a["url"]): a for group in DOC["groups"] for a in group["apis"]}


class SchemaClient:
    def __init__(self, responses):
        self.responses, self.requests = responses, []

    def call(self, method, path, *, query=None, body=None, **kwargs):
        api = APIS[(method, path)]
        fields = api["trs"][0]["request"]["body"]
        params = (query if method == "GET" else body) or {}
        allowed = {f["element"] for f in fields}
        required = {f["element"] for f in fields if f["required"] == "Y"}
        assert set(params) <= allowed, f"Undocumented request fields for {path}: {set(params)-allowed}"
        assert required <= set(params), f"Missing required fields for {path}: {required-set(params)}"
        self.requests.append(dict(method=method, path=path, params=params))
        return self.responses[path]


@unittest.skipUnless(SPEC.exists(), "official portal export not present; see README to download it")
class OfficialSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name)/"j.sqlite")

    def tearDown(self):
        self.j.db.close()
        self.tmp.cleanup()

    def test_order_buy_sell_and_cancel_request_fields_match_official_schema(self):
        for side in ["buy", "sell"]:
            ledger = dict(oder_no=17, oder_date="20261015", ovrs_stck_symb_iscd="TQQQ",
                          seln_byov_clcd="2" if side=="buy" else "1", odqt=3,
                          oder_rctf_cncl_cls_code="0")
            client = SchemaClient({
                "/trading/v1/overseas/orders/"+side:dict(data=dict(oder_no=17, warn_cls_code="0")),
                "/trading/v1/overseas/orders/today":dict(rsp_cd="5766", data=[ledger]),
                "/trading/v1/overseas/orders/cancel":dict(data=dict(oder_no=18, warn_cls_code="0"))})
            adapter = Meritz(client, self.j)
            receipt = adapter.order("TQQQ", side, D(3), D("70.05"), datetime(2026,10,15,tzinfo=timezone.utc))
            self.assertEqual(receipt["qty"], "3")
            adapter.cancel(receipt, D(2))
            request = client.requests[0]["params"]
            self.assertEqual(request["oder_cls_code"], "01")
            self.assertEqual(request["warn_cnfr_yn"], "N")

    def test_fx_uses_customer_applied_buy_rate_and_no_duplicate_fee(self):
        client = SchemaClient({
            "/forex/v1/rates/applied":dict(rsp_cd="0000",data=dict(exrt_stnd_time="20261015120000",
                tltr_byrt="1400",tltr_slrt="1400",tltr_aply_byrt="1400.7",tltr_aply_slrt="1399.3")),
            "/forex/v1/exchanges":dict(data=dict(tr_amt=140070, frcr_tr_amt="100", tr_srno=19,
                frcr_tfam_nwrm="100",fee=70))})
        adapter = Meritz(client, self.j)
        now = datetime(2026,10,15,3,tzinfo=timezone.utc)
        q = adapter.fx_quote(now)
        self.assertEqual(q.ask, D("1400.7"))
        receipt = adapter.exchange(D(140070),q)
        self.assertEqual(client.requests[-1]["params"]["iqry_exrt"], "1400.7")
        self.assertEqual(client.requests[-1]["params"]["wncr_amt"], 140070)
        self.assertEqual(receipt["usd_after"], "100")


if __name__ == "__main__":
    unittest.main()
