"""Gunicorn entry point for the branded subscription API."""

from vpnshopbot.brand_api import app

__all__ = ["app"]
