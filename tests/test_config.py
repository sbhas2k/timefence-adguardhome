"""Config validation."""

from __future__ import annotations

import os
import stat

import pytest

from timefence.config import ConfigError, Settings, normalize_base_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", "/"), ("/", "/"), ("timer", "/timer"), ("/timer/", "/timer"), ("a/b/", "/a/b")],
)
def test_normalize_base_path(raw, expected):
    assert normalize_base_path(raw) == expected


def _env(**over):
    env = {
        "ADGUARD_PASSWORD": "secret",
        "DATA_DIR": "/tmp/timefence-test-data",
    }
    env.update(over)
    return env


def test_defaults_are_documented_values(tmp_path):
    settings = Settings.from_env(
        _env(),
        timezone_file=tmp_path / "no-timezone",
        localtime_file=tmp_path / "no-localtime",
    )
    assert settings.listen_host == "127.0.0.1"
    assert settings.listen_port == 8099
    assert settings.base_path == "/"
    assert (settings.tz, settings.tz_source) == ("UTC", "default")
    assert settings.reconcile_interval == 30
    assert settings.secure_cookies is True


def test_my_instance_style_settings():
    settings = Settings.from_env(
        _env(
            BASE_PATH="timer", TZ="Asia/Kolkata", LISTEN_PORT="8099", TRUSTED_PROXIES="127.0.0.1/32"
        )
    )
    assert settings.base_path == "/timer"
    assert settings.zone.key == "Asia/Kolkata"
    assert str(settings.trusted_proxies[0]) == "127.0.0.1/32"


def test_password_file_is_read(tmp_path):
    path = tmp_path / "pass"
    path.write_text("from-file\n")
    settings = Settings.from_env(_env(ADGUARD_PASSWORD="", ADGUARD_PASSWORD_FILE=str(path)))
    assert settings.adguard_password == "from-file"
    assert "***" in str(settings.redacted()["adguard_password"])


def test_missing_password_is_rejected_at_validation():
    settings = Settings.from_env(_env(ADGUARD_PASSWORD=""))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_timezone():
    with pytest.raises(ConfigError):
        Settings.from_env(_env(TZ="Mars/Olympus"))


def test_bad_port():
    settings = Settings.from_env(_env(LISTEN_PORT="99999"))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_trusted_proxy():
    with pytest.raises(ConfigError):
        Settings.from_env(_env(TRUSTED_PROXIES="not-a-cidr"))


def test_reconcile_interval_floor():
    settings = Settings.from_env(_env(RECONCILE_INTERVAL="1"))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_adguard_url():
    settings = Settings.from_env(_env(ADGUARD_URL="ftp://x"))
    with pytest.raises(ConfigError):
        settings.validate()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_data_dir_is_private(tmp_path):
    target = tmp_path / "private"
    settings = Settings.from_env({"ADGUARD_PASSWORD": "secret", "DATA_DIR": str(target)})
    settings.validate()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700


# --------------------------------------------------------------------------- #
# credentials inside ADGUARD_URL never reach a log line, a print or the API
# --------------------------------------------------------------------------- #

URL_WITH_CREDENTIALS = [
    # (url, what must not survive redaction)
    ("http://bob:hunter2@10.0.0.1:3000", ("bob", "hunter2")),
    ("http://bob@10.0.0.1:3000", ("bob",)),
    ("http://bob:pa@ss@10.0.0.1:3000", ("bob", "pa@ss")),
    ("http://bob:pa/ss@10.0.0.1:3000", ("bob", "pa/ss")),
    ("http://bob:pa?ss@10.0.0.1:3000", ("bob", "pa?ss")),
    ("http://bob:pa#ss@10.0.0.1:3000", ("bob", "pa#ss")),
    ("http://bob:hunter2@[::1]:3000", ("bob", "hunter2")),
    ("http://bob:hunter2@10.0.0.1:3000/adguard/home", ("bob", "hunter2")),
    ("https://bob:hunter2@adguard.example.com", ("bob", "hunter2")),
]


@pytest.mark.parametrize(("url", "forbidden"), URL_WITH_CREDENTIALS)
def test_the_url_in_the_config_dump_carries_no_credentials(url, forbidden, tmp_path):
    settings = Settings.from_env(_env(ADGUARD_URL=url, DATA_DIR=str(tmp_path / "data")))
    dump = repr(settings.redacted())
    for secret in forbidden:
        assert secret not in dump, dump
    # the endpoint is still recognisable, which is the point of printing it
    assert "10.0.0.1" in dump or "::1" in dump or "adguard.example.com" in dump, dump


def test_a_url_without_userinfo_is_printed_unchanged(tmp_path):
    settings = Settings.from_env(
        _env(ADGUARD_URL="http://10.0.0.1:3000", DATA_DIR=str(tmp_path / "data"))
    )
    assert settings.redacted()["adguard_url"] == "http://10.0.0.1:3000"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http://",
        "@",
        "http://@",
        "http://bob:hunter2@",
        "://@:::@",
        "http://[::1@",
        "http://bob:hunter2@[::1]",
        None,
        0,
    ],
)
def test_redacting_a_url_never_raises(url):
    from timefence.config import redact_url

    assert isinstance(redact_url(url), str)
    assert "hunter2" not in redact_url(url)


def test_the_http_client_still_gets_the_url_and_the_credentials():
    """Redaction is display only: the client keeps the exact URL it was given."""
    from timefence.adguard import AdGuardClient

    url = "http://bob:hunter2@10.0.0.1:3000"
    client = AdGuardClient(url, "admin", "secret")
    assert client.url == url
    assert client.auth == ("admin", "secret")


def _stub_status_server() -> tuple[object, int, list[dict]]:
    """A local stub that answers /control/status like AdGuard does."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib spelling
            seen.append({"path": self.path, "headers": dict(self.headers)})
            body = json.dumps({"version": "0.107.0"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ARG002 - silence the stub
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], seen


def test_no_route_logs_the_credentials_from_the_url(tmp_path, caplog):
    """Startup log, CLI dump and the HTTP library at DEBUG: none leak it."""
    import logging

    from timefence.adguard import AdGuardClient

    server, port, seen = _stub_status_server()
    url = f"http://bob:hunter2@127.0.0.1:{port}"
    try:
        with caplog.at_level(logging.DEBUG):
            settings = Settings.from_env(_env(ADGUARD_URL=url, DATA_DIR=str(tmp_path / "data")))
            settings.validate()
            # api.py's startup line
            logging.getLogger("timefence.api").info("configuration: %s", settings.redacted())
            # __main__'s `check` command
            print("config:", settings.redacted())
            client = AdGuardClient(
                settings.adguard_url, settings.adguard_user, settings.adguard_password
            )
            ok, detail = client.test_connection()
            logging.getLogger("timefence.api").info("AdGuard Home connection OK: %s", detail)
    finally:
        server.shutdown()
        server.server_close()

    assert ok, detail
    assert seen, "the stub saw no request"
    assert seen[0]["path"] == "/control/status"
    text = caplog.text
    for secret in ("hunter2", "bob", "Ym9iOmh1bnRlcjI="):
        assert secret not in text, text
