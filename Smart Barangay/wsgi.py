import os

from waitress import serve

from main import app


if __name__ == "__main__":
    serve(
        app,
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
    )
