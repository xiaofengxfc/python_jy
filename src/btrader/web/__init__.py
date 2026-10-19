"""Local web UI package.

Only :mod:`btrader.web.app` depends on FastAPI/uvicorn; the rest of the
application never imports this package, so the desktop build is unaffected.
"""
