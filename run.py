"""Entry point for the Realbook Excel Upload Flask app.

Usage:
    python3 run.py                # http://127.0.0.1:5000
    PORT=8000 python3 run.py      # custom port
    HOST=0.0.0.0 python3 run.py   # bind all interfaces
"""
import os

from app import app


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "1") not in ("0", "false", "False", "")
    print(f" * Realbook Excel Upload running on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
