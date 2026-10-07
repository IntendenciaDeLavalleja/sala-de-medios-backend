import pytest

from app.cors import init_public_cors


def allow_frontend(app):
    app.config["CORS_ORIGINS"] = ("https://medios.example.test",)
    init_public_cors(app)


@pytest.mark.parametrize("path", ["/api/public/events", "/api/public/stats", "/api/public/events/missing"])
def test_public_api_allows_configured_origin_including_errors(app, client, path):
    allow_frontend(app)
    response = client.get(path, headers={"Origin": "https://medios.example.test"})
    assert response.headers["Access-Control-Allow-Origin"] == "https://medios.example.test"
    assert "Origin" in response.vary
    assert "Access-Control-Allow-Credentials" not in response.headers


def test_cors_does_not_expose_admin_or_allow_foreign_origins(app, client):
    allow_frontend(app)
    for path, origin in (("/admin/login", "https://medios.example.test"),
                         ("/admin/users", "https://medios.example.test"),
                         ("/api/public/events", "https://foreign.example.test")):
        response = client.get(path, headers={"Origin": origin})
        assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_allows_public_reads_only(app, client):
    allow_frontend(app)
    response = client.options("/api/public/events", headers={"Origin": "https://medios.example.test",
                              "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Content-Type"})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"
    assert "Access-Control-Allow-Credentials" not in response.headers


@pytest.mark.parametrize("origin", ["*", "null", "https://medios.example.test/path", "https://user:pass@medios.example.test"])
def test_invalid_cors_configuration_fails_at_startup(app, origin):
    app.config["CORS_ORIGINS"] = (origin,)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        init_public_cors(app)


def test_backend_root_opens_its_own_administration(client):
    response = client.get("/")
    assert response.status_code == 302 and response.location.endswith("/admin/dashboard")


def test_admin_link_returns_to_configured_frontend(app, client):
    app.config["PUBLIC_BASE_URL"] = "https://medios.example.test"
    assert 'href="https://medios.example.test"' in client.get("/admin/login").get_data(as_text=True)
