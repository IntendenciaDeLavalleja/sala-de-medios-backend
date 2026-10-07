from urllib.parse import urlsplit

from flask import request


def init_public_cors(app):
    """Allow public reads from the configured frontend, never admin credentials."""
    origins = set()
    for origin in app.config.get("CORS_ORIGINS", ()):
        parts = urlsplit(origin)
        if (parts.scheme not in {"http", "https"} or not parts.netloc
                or parts.username or parts.password or parts.path not in {"", "/"}
                or parts.query or parts.fragment):
            raise RuntimeError("CORS_ORIGINS debe contener orígenes HTTP/HTTPS completos, sin rutas ni comodines.")
        origins.add(f"{parts.scheme}://{parts.netloc}")

    @app.after_request
    def public_cors_headers(response):
        if request.blueprint == "public":
            response.vary.add("Origin")
            origin = request.headers.get("Origin")
            if origin in origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
                response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length, Content-Range, Accept-Ranges, X-Request-ID"
        return response
