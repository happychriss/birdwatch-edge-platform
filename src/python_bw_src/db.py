"""
BirdWatch database schema — SQLAlchemy model + schema initialisation.

Table: bw_photos  (database: birdwatch on 192.168.1.110)

Run directly to create the table and apply any missing column migrations:
    python db.py

Connection is configured via environment variables (see .env.example):
    DB_HOST, DB_PORT, DB_NAME, DB_USERNAME, DB_PASSWORD
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

_db_url = URL.create(
    drivername="postgresql+psycopg2",
    username=os.getenv("DB_USERNAME"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST", "192.168.1.110"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME", "birdwatch"),
)

engine = create_engine(_db_url)
Session = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class BwPhoto(Base):
    """One row per PIR-triggered capture cycle."""

    __tablename__ = "bw_photos"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    source     = Column(String)   # device identifier (e.g. BW_DEV)
    date       = Column(DateTime) # server-side timestamp of upload
    voltage    = Column(Float)    # battery voltage at capture (V)
    debug      = Column(String)   # freeform debug / trigger info from firmware
    filename   = Column(String)   # saved JPEG filename (no path)
    brightdiff = Column(Float)    # brightness delta (legacy; firmware no longer sends this)
    cc_label   = Column(String)   # cloud-check decision: "cloud" | "non-cloud"
    cc_stage   = Column(String)   # cloud-check stage: WARMUP | DARK_OBJ | QUIET | SCENE_DRIFT | AMBIGUOUS


def _migrate_columns():
    """Add new columns to an existing table without dropping data."""
    migrations = [
        ("cc_label", "VARCHAR"),
        ("cc_stage", "VARCHAR"),
    ]
    with engine.connect() as conn:
        for col_name, col_type in migrations:
            try:
                conn.execute(text(f"ALTER TABLE bw_photos ADD COLUMN {col_name} {col_type}"))
                conn.commit()
                print(f"bw_photos: added column '{col_name}'.")
            except Exception:
                pass  # column already exists


def init_schema():
    """Create bw_photos if it does not already exist, then apply any missing column migrations."""
    Base.metadata.create_all(engine, checkfirst=True)
    _migrate_columns()
    print("bw_photos: schema verified / created.")


if __name__ == "__main__":
    init_schema()
