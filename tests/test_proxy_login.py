import re

from werkzeug.middleware.proxy_fix import ProxyFix


def test_https_login_keeps_session_and_relative_redirects_behind_proxy(app, client):
    """Exercise CSRF, secure cookies and both auth steps on the public HTTPS host."""
    app.config.update(SESSION_COOKIE_SECURE=True, WTF_CSRF_ENABLED=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    origin = "https://mapi.medios.lavalleja.uy"
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "mapi.medios.lavalleja.uy",
               "X-Forwarded-For": "203.0.113.10", "User-Agent": "Production-login-regression"}

    def token(response):
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]

    denied = client.get("/admin/dashboard", base_url=origin, headers=headers)
    assert denied.headers["Location"] == "/admin/login?next=/admin/dashboard"
    page = client.get(denied.headers["Location"], base_url=origin, headers=headers)
    assert "Secure" in page.headers["Set-Cookie"] and "HttpOnly" in page.headers["Set-Cookie"]
    question = re.search(r"¿Cuánto es (\d+) \+ (\d+)\?", page.get_data(as_text=True))
    response = client.post("/admin/login", base_url=origin,
                           headers={**headers, "Referer": origin + "/admin/login"},
                           data={"email": "superadmin@lavalleja.uy", "password": "Contraseña-pruebas-123",
                                 "captcha": str(int(question[1]) + int(question[2])),
                                 "next": "/admin/dashboard", "csrf_token": token(page)})
    assert response.status_code == 302 and response.headers["Location"] == "/admin/2fa"
    assert client.get("/admin/dashboard", base_url=origin, headers=headers).status_code == 302
    code = app.extensions["outbox"][-1]["code"]
    page = client.get("/admin/2fa", base_url=origin, headers=headers)
    response = client.post("/admin/2fa", base_url=origin,
                           headers={**headers, "Referer": origin + "/admin/2fa"},
                           data={"code": code, "csrf_token": token(page)})
    assert response.status_code == 302 and response.headers["Location"] == "/admin/dashboard"
    assert "Secure" in response.headers["Set-Cookie"]
    assert client.get(response.headers["Location"], base_url=origin, headers=headers).status_code == 200
    assert client.get("/admin/events", base_url=origin, headers=headers).status_code == 200
