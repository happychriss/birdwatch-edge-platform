"""
BirdWatch database schema — SQLAlchemy model + schema initialisation.

Table: bw_frames  (database: birdwatch on 192.168.1.110)

Run directly to create the table:
    python db.py

Connection is configured via environment variables (see .env.example):
    DB_HOST, DB_PORT, DB_NAME, DB_USERNAME, DB_PASSWORD
"""

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker, scoped_session

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
# scoped_session gives each thread its own Session instance — safe with Flask threaded=True.
Session = scoped_session(sessionmaker(bind=engine))


class Base(DeclarativeBase):
    pass



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



def init_schema():
    """Create bw_frames table if it does not exist."""
    Base.metadata.create_all(engine, checkfirst=True)
    print("schema verified / created (bw_frames).")


if __name__ == "__main__":
    init_schema()
