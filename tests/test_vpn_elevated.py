"""openconnect 9 elevated flow: parsing and plain-language errors (no network)."""
from mouseion import vpn_elevated as e


def test_parse_authenticate_output():
    out = ("POST https://gw/remote/logincheck\n"
           "COOKIE='SVPNCOOKIE=abc; path=/'\nHOST='1.2.3.4'\n"
           "CONNECT_URL='https://gw:31443'\nFINGERPRINT='pin-sha256:AAAA='\n")
    v = e._parse_auth(out)
    assert v["COOKIE"] == "SVPNCOOKIE=abc; path=/"
    assert v["FINGERPRINT"] == "pin-sha256:AAAA="
    assert v["CONNECT_URL"] == "https://gw:31443"


def test_missing_cookie_is_not_parsed():
    assert "COOKIE" not in e._parse_auth("Login failed.\nFailed to complete authentication\n")


def test_errors_are_explained():
    assert "rejected" in e._explain("Login failed.")
    assert "more than a password" in e._explain("User input required in non-interactive mode")


def test_runner_uses_cookie_not_password():
    assert "--cookie-on-stdin" in e._RUNNER and "passwd" not in e._RUNNER
    assert "stop.flag" in e._RUNNER
