"""Entry point: `gunicorn app:app` (unchanged start command)."""
from lama import create_app

app = create_app()

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
