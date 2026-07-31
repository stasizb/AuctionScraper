"""Railway-compatible HTTP API for AuctionScraper."""

from __future__ import annotations

from flask import Flask, jsonify, request

from clients.auction_lookup import AuctionLookup


def create_app(lookup: AuctionLookup | None = None) -> Flask:
    app = Flask(__name__)
    service = lookup or AuctionLookup()

    @app.get("/")
    def index():
        return jsonify({
            "service": "AuctionScraper",
            "endpoints": {"health": "/health", "search": "/search?q=<VIN_OR_LOT>"},
        })

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/search")
    def search():
        query = request.args.get("q", "")
        auction = request.args.get("auction", "all").strip().lower()
        try:
            payload = service.search(query, auction)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        status = 502 if payload["errors"] and not payload["results"] else 200
        return jsonify(payload), status

    return app


app = create_app()


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
