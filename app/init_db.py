"""Create (or upgrade) the SQLite database.

    python -m app.init_db
"""
from . import db

if __name__ == "__main__":
    db.init_db()
    print(f"Database ready at {db.get_path()}")
