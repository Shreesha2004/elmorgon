"""The DuckDB warehouse: build it with dbt, read from it with pandas."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd

from elmorgon.config import DATA_DIR, DBT_DIR, WAREHOUSE


def dbt_env() -> None:
    """dbt's profile reads the data directory from the environment."""
    os.environ["ELMORGON_DATA_DIR"] = DATA_DIR.resolve().as_posix()


def run_dbt(*args: str, data_dir: Path | None = None) -> None:
    """Run a dbt command (e.g. "build") against the project in transform/.

    dbt runs in its own process: in-process, dbt-duckdb keeps the database
    open with its own settings, and DuckDB then refuses our read connection.
    """
    env = os.environ | {"ELMORGON_DATA_DIR": (data_dir or DATA_DIR).resolve().as_posix()}
    command = [
        sys.executable,
        "-c",
        "from dbt.cli.main import cli; cli()",
        *args,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(DBT_DIR),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"dbt {' '.join(args)} failed:\n{result.stdout[-3000:]}{result.stderr[-2000:]}"
        )


def connect() -> duckdb.DuckDBPyConnection:
    """Read-only connection; timestamps come back in UTC."""
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    con.execute("set TimeZone = 'UTC'")
    return con


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    with connect() as con:
        return con.execute(sql, params or []).df()


def load_features() -> pd.DataFrame:
    """The model's feature table: one row per zone and delivery hour."""
    frame = query("select * from feat_price_hourly order by delivery_date, zone, hour_start")
    frame["delivery_date"] = pd.to_datetime(frame["delivery_date"]).dt.date
    frame["issue_date"] = pd.to_datetime(frame["issue_date"]).dt.date
    return frame


def load_interval_prices(start, end) -> pd.DataFrame:
    """Actual cleared prices at market resolution for delivery days start..end."""
    frame = query(
        """
        select zone, delivery_date, interval_start, resolution_minutes, price_sek_kwh
        from fct_price_interval
        where delivery_date between ? and ?
        order by zone, interval_start
        """,
        [start, end],
    )
    frame["delivery_date"] = pd.to_datetime(frame["delivery_date"]).dt.date
    return frame
