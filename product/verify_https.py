"""Verify the local HTTPS profile using explicit CA and hostname validation."""
import hashlib
import http.cookiejar
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import socket
import ssl
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

HTTPS_PORT = int(os.getenv("PRODUCT_HTTPS_PORT", "3443"))
HTTP_PORT = int(os.getenv("PRODUCT_HTTP_PORT", "3100"))
HTTPS = f"https://localhost:{HTTPS_PORT}"
HTTP = f"http://127.0.0.1:{HTTP_PORT}"
os.environ["PRODUCT_TEST_BASE"] = HTTPS

from verify_import import CONFIG, IMPORTS, INVOICES, call, upload, wait_for, xlsx


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def verify_redirect():
    path = "/api/enterprises?test=tls"
    client = build_opener(RejectRedirect())
    request = Request(HTTP + path, headers={"Host": "untrusted.example"})
    try:
        with client.open(request, timeout=10):
            raise AssertionError("HTTP did not redirect")
    except HTTPError as error:
        assert error.code == 308, error.code
        assert error.headers["Location"] == HTTPS + path
        assert not error.headers.get_all("Set-Cookie")


def session_cookie(headers):
    cookie = SimpleCookie()
    for value in headers.get_all("Set-Cookie", []):
        cookie.load(value)
    session = cookie["tax_product_session"]
    assert session["secure"] and session["httponly"]
    assert session["samesite"].lower() == "strict" and session["path"] == "/"
    return session


def main():
    verify_redirect()
    ca = Path(__file__).with_name("tls") / "local" / "ca.crt"
    context = ssl.create_default_context(cafile=str(ca))
    with socket.create_connection(("127.0.0.1", HTTPS_PORT), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname="localhost") as tls:
            protocol = tls.version()
            assert protocol in ("TLSv1.2", "TLSv1.3")
    jar = http.cookiejar.CookieJar()
    client = build_opener(HTTPSHandler(context=context), HTTPCookieProcessor(jar))
    with client.open(HTTPS + "/", timeout=10) as response:
        assert response.status == 200 and "财税资料核验工作台" in response.read().decode()
    call(client, "/api/enterprises", expected=401)
    login = Request(HTTPS + "/api/session", method="POST",
                    data=json.dumps({"username": "alpha", "password": CONFIG["PRODUCT_ALPHA_PASSWORD"]}).encode(),
                    headers={"Content-Type": "application/json"})
    with client.open(login, timeout=10) as response:
        session_cookie(response.headers)
        assert json.load(response)["username"] == "alpha"
    assert any(cookie.name == "tax_product_session" and cookie.secure for cookie in jar)
    plain = Request(f"http://localhost:{HTTP_PORT}/api/enterprises")
    jar.add_cookie_header(plain)
    assert not plain.has_header("Cookie")
    assert "C001" in {row["enterprise_id"] for row in call(client, "/api/enterprises")}

    probe = Request(HTTPS + "/api/enterprises/C001/probe", method="POST")
    with client.open(probe, timeout=15) as response:
        assert response.headers.get_content_type() == "text/event-stream"
        frames = [line.decode().rstrip("\r\n") for line in response]
        assert [line for line in frames if line.startswith("event: ")] == ["event: status", "event: done"]
        assert "data: Agent 连接成功" in frames

    invoice = "TLS-" + uuid4().hex[:12]
    raw = xlsx([(invoice, "2026-09", None, "HTTPS 验收")])
    job = upload(client, raw)
    assert job["file_sha256"] == hashlib.sha256(raw).hexdigest()
    preview = wait_for(client, IMPORTS + "/" + job["job_id"], "ready")["preview"]
    assert not preview["errors"] and [row["invoice_id"] for row in preview["rows"]] == [invoice]
    call(client, INVOICES + invoice, expected=404)

    logout = Request(HTTPS + "/api/session", method="DELETE")
    with client.open(logout, timeout=10) as response:
        assert response.status == 204 and session_cookie(response.headers)["max-age"] == "0"
    call(client, "/api/enterprises", expected=401)
    print("PASS: trusted TLS, HTTP 308 redirect, Secure Cookie, login/logout, Excel preview and SSE")
    print(f"Negotiated {protocol}")
    print(f"HTTPS import preview: {job['job_id']}")


if __name__ == "__main__":
    main()
