"""
InvoiceIQ – Application Entry Point
Run with: python run.py
Production: gunicorn -w 4 -b 0.0.0.0:5000 "run:create_app()"
"""
import os
from dotenv import load_dotenv

load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
