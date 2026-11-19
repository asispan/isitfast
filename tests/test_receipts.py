"""Signed byte receipts from the speed Worker bound the claimed result."""
import hashlib
import hmac
import os
import sys
import time

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("IP_SALT", "test-salt-0123456789")
os.environ.setdefault("DATA_DIR", __import__("tempfile").mkdtemp(prefix="isitfast-receipts-"))

from app import main  # noqa: E402

KEY = "k" * 64
SID = "0123456789abcdef"


def rc(d, b, t0, t1, sid=SID, key=KEY):
    msg = f"{sid}|{d}|{b}|{t0}|{t1}"
    return f"{msg}|{hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]}"


def rc2(d, b, t0, t1, rid, sid=SID, key=KEY):
    msg = f"{sid}|{d}|{b}|{t0}|{t1}|{rid}"
    return f"{msg}|{hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]}"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(main, "RECEIPT_KEY", KEY)


def session(down_mbps=100.0, up_mbps=20.0, secs=6):
    """Receipts a real 6 s download at down_mbps on 4 streams, then a 6 s upload at up_mbps on 3 streams."""
    t = int(time.time() * 1000) - 20_000
    per_stream = int(down_mbps * 1e6 / 8 * secs / 4)
    rs = [rc2("d", per_stream, t + 10 * k, t + secs * 1000 - 40 + k, f"{k:08x}") for k in range(4)]
    t_up = t + secs * 1000 + 300
    per_up = int(up_mbps * 1e6 / 8 * secs / 3)
    rs += [rc("u", per_up, t_up + 5 * k, t_up + secs * 1000) for k in range(3)]
    return rs, (t // 1000) - 1


def test_genuine_result_passes():
    rs, iat = session()
    out = main.check_receipts(rs, SID, iat, down=110, up=22)  # client's trimmed mean is a bit above average
    assert 90 < out["down_bound"] < 110 and 15 < out["up_bound"] < 25


def test_inflated_claim_rejected():
    rs, iat = session(down_mbps=20)
    with pytest.raises(HTTPException) as e:
        main.check_receipts(rs, SID, iat, down=300, up=5)
    assert e.value.status_code == 403


@pytest.mark.parametrize("mutate", [
    lambda rs: [rs[0][:-4] + "0000"] + rs[1:],                 # forged signature
    lambda rs: [rc("d", 10**9, *rs[0].split("|")[3:5], sid="ffffffffffffffff")] + rs[1:],  # other session
    lambda rs: [rc("d", 100, 1000000000000, 1000000000001)] + rs,  # timestamps from long ago
    lambda rs: rs + ["garbage"],
])
def test_tampering_rejected(mutate):
    rs, iat = session()
    with pytest.raises(HTTPException):
        main.check_receipts(mutate(rs), SID, iat, down=100, up=20)


def test_cumulative_receipts_not_double_counted():
    rs, iat = session(down_mbps=50)
    p = rs[0].split("|")
    extra = [rc2("d", 1000, p[3], p[3], p[5])]  # an earlier cumulative receipt from the same response
    out = main.check_receipts(extra + rs, SID, iat, down=50, up=20)
    assert out["down_bound"] < 60



def test_parallel_responses_sharing_t0_are_all_counted():
    """Workers' clock is frozen between I/O, so 4 parallel streams report the same t0. Each has its own
    rid and must count: this is the 'Couldn't verify' bug on phones (only 1 of 4 streams was counted)."""
    t = int(time.time() * 1000) - 20_000
    per = int(100e6 / 8 * 6 / 4)  # 100 Mbps for 6 s over 4 streams
    rs = [rc2("d", per, t, t + 5960, f"{k:08x}") for k in range(4)]
    rs += [rc2("d", per // 2, t, t + 3000, f"{k:08x}") for k in range(4)]  # earlier cumulative receipts
    t_up = t + 6300
    rs += [rc2("u", int(20e6 / 8 * 6 / 3), t_up, t_up + 6000, f"{9 + k:08x}") for k in range(3)]
    out = main.check_receipts(rs, SID, t // 1000 - 1, down=110, up=21)
    assert 90 < out["down_bound"] < 110  # all four streams, not one


def test_rid_is_signed():
    t = int(time.time() * 1000) - 20_000
    good = rc2("d", 10**6, t, t + 10, "0000000a")
    forged = good.replace("|0000000a|", "|0000000b|")
    rs = [forged, rc2("u", 10**6, t + 500, t + 900, "0000000c")]
    with pytest.raises(HTTPException):
        main.check_receipts(rs, SID, t // 1000 - 1, down=1, up=1)


def test_missing_upload_receipts_is_unverified_not_rejected():
    """Slow uplink: every upload chunk was cut off at the time limit, so there are no upload receipts."""
    rs, iat = session()
    assert main.check_receipts([r for r in rs if "|u|" not in r], SID, iat, down=100, up=3) is None
