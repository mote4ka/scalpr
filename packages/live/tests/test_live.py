import base64
import hashlib
import hmac
from datetime import UTC, datetime

import pytest
from scalpr_live import bybit, htx
from scalpr_live.check import BAD, OK, WARN, Report, sizing
from scalpr_strategy import FEES
from scalpr_trader.config import Coin, Config

NOW = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)


def test_bybit_signature_covers_timestamp_key_window_and_payload():
    expected = hmac.new(b"secret", b"1700000000000key5000category=linear&symbol=DOTUSDT", hashlib.sha256).hexdigest()
    assert bybit.sign("secret", 1700000000000, "key", "category=linear&symbol=DOTUSDT") == expected


def test_htx_canonical_query_is_sorted_and_encoded():
    params = {"order-id": 1, "Timestamp": "2026-11-01T10:00:00", "AccessKeyId": "k"}
    assert htx.canonical(params) == "AccessKeyId=k&Timestamp=2026-11-01T10%3A00%3A00&order-id=1"


def test_htx_signed_query():
    query = htx.signed_query("key", "secret", "POST", "api.hbdm.com", "/linear-swap-api/v1/swap_fee", now=NOW)
    auth = "AccessKeyId=key&SignatureMethod=HmacSHA256&SignatureVersion=2&Timestamp=2026-11-01T10%3A00%3A00"
    payload = f"POST\napi.hbdm.com\n/linear-swap-api/v1/swap_fee\n{auth}"
    signature = base64.b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha256).digest()).decode()
    assert query.startswith(auth + "&Signature=")
    # base64 of 32 bytes ends with "=", which has to go out encoded
    assert query.endswith("%3D")
    assert query.removeprefix(auth + "&Signature=").replace("%2B", "+").replace("%2F", "/").replace("%3D", "=") == signature


def test_htx_get_signs_its_own_parameters_too():
    with_uid = htx.signed_query("key", "secret", "GET", "api.huobi.pro", "/v2/user/api-key", {"uid": 7}, now=NOW)
    assert "&uid=7&Signature=" in with_uid
    assert with_uid != htx.signed_query("key", "secret", "GET", "api.huobi.pro", "/v2/user/api-key", {"uid": 8}, now=NOW).replace("uid=8", "uid=7")


@pytest.mark.parametrize("answer", [{"status": "ok", "data": [1]}, {"code": 200, "data": [1], "success": True}])
def test_htx_result_shapes(answer):
    assert htx.result("/x", answer) == [1]


@pytest.mark.parametrize("answer, code", [
    ({"status": "error", "err_code": 1030, "err_msg": "bad"}, 1030),
    ({"status": "error", "err-code": "api-signature-not-valid", "err-msg": "bad"}, "api-signature-not-valid"),
    ({"code": 1002, "message": "unauthorized"}, 1002),
])
def test_htx_result_errors(answer, code):
    with pytest.raises(htx.HtxError) as error:
        htx.result("/x", answer)
    assert error.value.code == code


def _reports(contract_size: float, qty_step: float, maker: float = FEES["htx"]["maker"], taker: float = FEES["bybit"]["taker"]) -> tuple[Report, Report]:
    htx_report, bybit_report = Report("HTX"), Report("Bybit")
    htx_report.coins["DOTUSDT"] = {"price": 1.0, "contract_size": contract_size, "maker": maker, "taker": 0.05, "position": 0.0}
    bybit_report.coins["DOTUSDT"] = {"price": 1.0, "qty_step": qty_step, "min_qty": qty_step, "min_notional": 5.0,
                                     "maker": 0.02, "taker": taker, "position": 0.0, "one_way": True}
    htx_report.balance, bybit_report.balance = 200.0, 100.0
    htx_report.leverage["DOTUSDT"] = bybit_report.leverage["DOTUSDT"] = 5.0
    return htx_report, bybit_report


CONFIG = Config(coins=(Coin("DOTUSDT", 0.25, 100, 300),))


def test_sizing_flags_small_fills_and_margin():
    _, notes = sizing(CONFIG, *_reports(contract_size=1, qty_step=0.1))
    texts = {(where, level): what for where, what, level in notes}
    # bybit takes no less than $5: a one-contract fill ($1) waits for more
    assert "can't be hedged alone" in texts["DOTUSDT", WARN]
    # $300 at 5x is $60 of margin: $200 covers it twice, $100 doesn't
    assert texts["HTX", OK].startswith("max exposure $300 at leverage 5x takes $60 margin")
    assert ("Bybit", WARN) in texts
    assert not any(level == BAD for _, _, level in notes)


def test_sizing_flags_contracts_that_are_not_whole_steps():
    _, notes = sizing(CONFIG, *_reports(contract_size=0.1, qty_step=1))
    assert any(level == BAD and "not a whole number" in what for _, what, level in notes)


def test_sizing_flags_fees_above_the_model():
    _, notes = sizing(CONFIG, *_reports(contract_size=1, qty_step=1, taker=0.06))
    assert any("fees" in what and level == WARN for _, what, level in notes)
