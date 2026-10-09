"""Dagster view of the pipeline: run `dagster dev` in the project root.

Each step is an asset, and dbt models appear one by one with their lineage,
so the graph reads bronze -> silver -> staging -> marts -> features -> forecast
-> dashboard. The daily job runs at 10:00 Stockholm time; the backtest, which
retrains 68 models, runs on the first of each month.
"""

import os
import shutil
import sys
from pathlib import Path

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    Definitions,
    MaterializeResult,
    ScheduleDefinition,
    asset,
    define_asset_job,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

from elmorgon import backtest, bronze, forecast, report, silver, valuation, warehouse
from elmorgon.config import DBT_DIR

warehouse.dbt_env()


def find_dbt() -> str:
    """The dbt installed in this environment, so it works without an activated venv."""
    candidates = [
        Path(sys.prefix) / "Scripts",
        Path(sys.prefix) / "bin",
        Path(sys.executable).parent,
    ]
    for folder in candidates:
        for name in ("dbt.exe", "dbt"):
            if (folder / name).exists():
                return str(folder / name)
    return shutil.which("dbt") or "dbt"


DBT_EXECUTABLE = find_dbt()
# dagster-dbt's dev-time manifest step builds its own resource that only looks on PATH.
os.environ["PATH"] = str(Path(DBT_EXECUTABLE).parent) + os.pathsep + os.environ.get("PATH", "")
dbt_project = DbtProject(project_dir=DBT_DIR, profiles_dir=DBT_DIR)
dbt_project.prepare_if_dev()


# Bronze: raw API responses.
@asset(group_name="bronze", description="Day-ahead prices, one raw JSON file per zone and day.")
def bronze_prices() -> MaterializeResult:
    written = bronze.ingest_prices()
    return MaterializeResult(metadata={"files_written": len(written)})


@asset(group_name="bronze", description="Archived hourly weather forecasts, one file per month.")
def bronze_weather_archive() -> MaterializeResult:
    return MaterializeResult(metadata={"months_written": len(bronze.ingest_weather_archive())})


@asset(
    group_name="bronze", description="Today's weather forecast, snapshotted once and never revised."
)
def bronze_weather_forecast() -> MaterializeResult:
    return MaterializeResult(metadata={"path": str(bronze.ingest_weather_forecast())})


# Silver: validated Parquet. Keys match the dbt source names, so dbt models hang off them.
@asset(key=AssetKey(["silver", "prices"]), deps=[bronze_prices], group_name="silver")
def silver_prices() -> MaterializeResult:
    stats = silver.build_prices()
    return MaterializeResult(metadata=stats)


@asset(
    key=AssetKey(["silver", "weather_archive"]), deps=[bronze_weather_archive], group_name="silver"
)
def silver_weather_archive() -> MaterializeResult:
    return MaterializeResult(metadata={"months_built": silver.build_weather_archive()})


@asset(
    key=AssetKey(["silver", "weather_forecast"]),
    deps=[bronze_weather_forecast],
    group_name="silver",
)
def silver_weather_forecast() -> MaterializeResult:
    return MaterializeResult(metadata={"snapshots_built": silver.build_weather_forecasts()})


# Warehouse: every dbt model and test.
class WarehouseTranslator(DagsterDbtTranslator):
    """Put every dbt model and seed in one 'warehouse' group in the Dagster graph."""

    def get_group_name(self, dbt_resource_props):
        return "warehouse"


@dbt_assets(manifest=dbt_project.manifest_path, dagster_dbt_translator=WarehouseTranslator())
def warehouse_models(context: AssetExecutionContext, dbt: DbtCliResource):
    yield from dbt.cli(["build"], context=context).stream()


FEATURES = AssetKey("feat_price_hourly")


@asset(
    deps=[FEATURES],
    group_name="forecast",
    description="Tomorrow's P10/P50/P90 per zone, written once to forecasts/issued/.",
)
def live_forecast() -> MaterializeResult:
    path = forecast.issue()
    return MaterializeResult(metadata={"issued": str(path) if path else "already issued"})


@asset(
    deps=[FEATURES],
    group_name="evaluation",
    description="Monthly walk-forward backtest of every model since Jan 2024.",
)
def backtest_results() -> MaterializeResult:
    predictions = backtest.run(warehouse.load_features())
    overall = backtest.score(predictions)["overall"]
    return MaterializeResult(metadata={f"mae_{k}": round(v["mae"], 4) for k, v in overall.items()})


@asset(
    deps=[backtest_results],
    group_name="evaluation",
    description="SEK value of each forecast when scheduling an EV and a home battery.",
)
def schedule_value() -> MaterializeResult:
    import pandas as pd

    summary = valuation.summarise(valuation.run(pd.read_parquet(backtest.PREDICTIONS)))
    ev = summary["overall"]["ev"]["saving_sek_per_year"]
    return MaterializeResult(metadata={f"ev_saving_{k}": round(v) for k, v in ev.items()})


@asset(
    deps=[live_forecast, schedule_value, AssetKey("mart_daily_zone_stats")],
    group_name="publish",
    description="site/data/dashboard.json for the static dashboard.",
)
def dashboard() -> MaterializeResult:
    report.export()
    return MaterializeResult(metadata={"path": str(report.OUT)})


daily_job = define_asset_job(
    "daily",
    selection=AssetSelection.all() - AssetSelection.assets(backtest_results, schedule_value),
)
monthly_job = define_asset_job(
    "monthly_backtest",
    selection=AssetSelection.assets(backtest_results, schedule_value, dashboard),
)

defs = Definitions(
    assets=[
        bronze_prices,
        bronze_weather_archive,
        bronze_weather_forecast,
        silver_prices,
        silver_weather_archive,
        silver_weather_forecast,
        warehouse_models,
        live_forecast,
        backtest_results,
        schedule_value,
        dashboard,
    ],
    jobs=[daily_job, monthly_job],
    schedules=[
        ScheduleDefinition(
            job=daily_job, cron_schedule="0 10 * * *", execution_timezone="Europe/Stockholm"
        ),
        ScheduleDefinition(
            job=monthly_job, cron_schedule="0 6 1 * *", execution_timezone="Europe/Stockholm"
        ),
    ],
    resources={"dbt": DbtCliResource(project_dir=dbt_project, dbt_executable=DBT_EXECUTABLE)},
)
