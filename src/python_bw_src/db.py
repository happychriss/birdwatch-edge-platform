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
from sqlalchemy.dialects.postgresql import JSONB
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
    cc_label   = Column(String)   # cloud-check decision: "clouds" | "process"
    cc_stage   = Column(String)   # cloud-check stage: WARMUP | DARK_OBJ | QUIET | SCENE_DRIFT | AMBIGUOUS
    photo_mode = Column(String)   # camera exposure mode used for the JPEG: "NORMAL" | "LOWLIGHT"


class BwFrame(Base):
    """One row per ESP capture cycle — new schema-less telemetry table.

    Only captured_at and result are promoted to columns for fast filtering/charting.
    Everything else (battery, trigger, photo_mode, cloud-check intermediates, …)
    lives in the meta JSONB column.  Adding a new ESP telemetry key requires no
    schema change — it just appears in meta automatically.
    """

    __tablename__ = "bw_frames"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    captured_at = Column(DateTime, index=True)  # from meta["captured_at"] or server receive-time
    result      = Column(String, index=True)    # promoted from meta["result"] for fast queries
    filename    = Column(String)                # saved JPEG filename (no path)
    meta        = Column(JSONB)                 # all ESP telemetry keys verbatim
    # label and downloaded_at live inside meta["label"] / meta["downloaded_at"] — no column needed


def _migrate_columns():
    """Add new columns to an existing table without dropping data."""
    migrations = [
        ("cc_label",   "VARCHAR"),
        ("cc_stage",   "VARCHAR"),
        ("photo_mode", "VARCHAR"),
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
    """Create tables if they do not exist, then apply any missing column migrations.

    bw_photos — existing table, never altered (data preserved).
    bw_frames  — new schema-less telemetry table, created on first run.
    """
    Base.metadata.create_all(engine, checkfirst=True)
    _migrate_columns()
    print("schema verified / created (bw_photos + bw_frames).")


if __name__ == "__main__":
    init_schema()
