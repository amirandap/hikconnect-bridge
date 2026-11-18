"""Protección del API local frente a páginas web y accesos no autorizados; límites y errores."""
from __future__ import annotations

import http.client

import pytest
from net import fetch

from hikconnect_bridge.cli import EXIT_USAGE, main
from hikconnect_bridge.service import describe_error, retry_delay

TOKEN = "s3cr3t-token-for-tests"


def raw_get(port: int, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.endheaders()
    resp = conn.getresponse()
    status, hdrs = resp.status, dict(resp.getheaders())
    conn.close()
    return status, hdrs


def test_health_works_for_a_legitimate_local_client(start_bridge, replay):
    bridge = start_bridge(replay())
    status, headers, body = fetch(f"{bridge.url}/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b'"state"' in body


def test_unknown_path_is_404(start_bridge, replay):
    bridge = start_bridge(replay())
    assert fetch(f"{bridge.url}/etc/passwd")[0] == 404
    assert fetch(f"{bridge.url}/../../etc/passwd")[0] == 404


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8558", "127.0.0.1.evil.com", ""])
def test_dns_rebinding_host_is_rejected_and_source_stays_closed(start_bridge, replay, host):
    source = replay()
    bridge = start_bridge(source)
    port = bridge.server.server_address[1]
    for path in ("/health", "/frame.jpg", "/stream.ts"):
        status, _ = raw_get(port, path, {"Host": host} if host else {})
        assert status == 403, f"{path} con Host={host!r} debería ser 403"
    assert source.opens == 0, "una petición rechazada no debe abrir la nube"


def test_cross_site_browser_request_is_rejected(start_bridge, replay):
    source = replay()
    bridge = start_bridge(source)
    port = bridge.server.server_address[1]
    status, _ = raw_get(port, "/frame.jpg", {"Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert source.opens == 0


def test_same_origin_and_none_fetch_site_are_allowed(start_bridge, replay):
    bridge = start_bridge(replay())
    port = bridge.server.server_address[1]
    for site in ("same-origin", "none"):
        status, _ = raw_get(port, "/health", {"Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": site})
        assert status == 200


def test_token_is_required_when_configured(start_bridge, replay):
    bridge = start_bridge(replay(), token=TOKEN)
    assert fetch(f"{bridge.url}/health")[0] == 403
    assert fetch(f"{bridge.url}/health?token=wrong")[0] == 403
    assert fetch(f"{bridge.url}/health?token={TOKEN}")[0] == 200
    assert fetch(f"{bridge.url}/health", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert fetch(f"{bridge.url}/health", headers={"Authorization": "Bearer wrong"})[0] == 403


def test_token_never_appears_in_health_or_errors(start_bridge, replay):
    bridge = start_bridge(replay(), token=TOKEN)
    _, _, body = fetch(f"{bridge.url}/health?token={TOKEN}")
    assert TOKEN.encode() not in body


def test_describe_error_redacts_urls_and_long_hex_and_truncates():
    text = describe_error(RuntimeError("fallo en https://x.example/api?token=abc y clave " + "f" * 40 + " " + "z" * 500))
    assert "https://" not in text and "f" * 24 not in text
    assert len(text) <= 120
    assert text.startswith("RuntimeError:")


def test_retry_delay_grows_is_capped_and_has_jitter():
    import random

    rng = random.Random(7)
    delays = [retry_delay(n, rng=rng) for n in (1, 2, 3, 4, 5, 6, 7)]
    assert delays[1] > delays[0] * 1.3
    assert max(delays) <= 30 * 1.21
    assert len({round(retry_delay(3, rng=rng), 3) for _ in range(5)}) > 1


def test_cli_refuses_non_loopback_without_token(capsys):
    assert main(["serve", "1", "--host", "0.0.0.0"]) == EXIT_USAGE
    assert "--token" in capsys.readouterr().err


def test_cli_fails_cleanly_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("HIK_CONNECT_USER", raising=False)
    monkeypatch.delenv("HIK_CONNECT_PASSWORD", raising=False)
    assert main(["serve", "1"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "HIK_CONNECT_USER" in err and "Traceback" not in err


def test_cloud_source_never_exposes_the_password(monkeypatch):
    from hikconnect_bridge.sources import CloudSource

    monkeypatch.setenv("HIK_CONNECT_USER", "alguien@example.com")
    monkeypatch.setenv("HIK_CONNECT_PASSWORD", "clave-super-secreta")
    source = CloudSource(1, 2)
    for text in (repr(source), str(source), f"{source!r}"):
        assert "clave-super-secreta" not in text
        assert "alguien@example.com" not in text
    assert "camera=1" in repr(source)


def test_extra_allowed_host_is_accepted_and_others_still_rejected(replay):
    from hikconnect_bridge.http_api import BridgeServer
    from hikconnect_bridge.service import LiveService

    server = BridgeServer(("127.0.0.1", 0), LiveService(replay()), extra_hosts=("hikconnect-bridge", "otro:9000"))
    port = server.server_address[1]
    try:
        assert f"hikconnect-bridge:{port}" in server.allowed_hosts
        assert "otro:9000" in server.allowed_hosts
        assert "evil.example.com:80" not in server.allowed_hosts
    finally:
        server.server_close()


def test_serve_token_defaults_to_the_environment_variable(monkeypatch):
    from hikconnect_bridge.cli import build_parser

    monkeypatch.setenv("BRIDGE_TOKEN", "from-env")
    assert build_parser().parse_args(["serve", "1"]).token == "from-env"
    assert build_parser().parse_args(["serve", "1", "--token", "cli"]).token == "cli"
