"""
Production entry point for Termux WebOS.
Uses Waitress (pure-Python WSGI) — works on Android/Termux
where gunicorn's fcntl-based workers fail.
"""
from waitress import serve
from server import app, init_db

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  Termux WebOS — Production Mode (Waitress)")
    print("  Listening on http://0.0.0.0:5000")
    print("=" * 60)
    serve(app, host="0.0.0.0", port=5000, threads=8)
