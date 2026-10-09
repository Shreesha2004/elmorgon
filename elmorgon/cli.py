"""Command line: `elmorgon <command>`. Every command is safe to re-run."""

from __future__ import annotations

import argparse
import logging
import sys

from elmorgon import bronze, silver, warehouse


def cmd_ingest(_: argparse.Namespace) -> None:
    bronze.ingest_prices()
    bronze.ingest_weather_archive()
    bronze.ingest_weather_forecast()


def cmd_build(_: argparse.Namespace) -> None:
    print(silver.build_all())
    warehouse.run_dbt("build")


def cmd_backtest(_: argparse.Namespace) -> None:
    from elmorgon import backtest, valuation

    predictions = backtest.run(warehouse.load_features())
    backtest.score(predictions)
    valuation.summarise(valuation.run(predictions))


def cmd_forecast(_: argparse.Namespace) -> None:
    from elmorgon import forecast

    path = forecast.issue()
    print(f"issued {path}" if path else "nothing new to issue")


def cmd_site(_: argparse.Namespace) -> None:
    from elmorgon import report

    report.export()
    print(f"wrote {report.OUT}")


def cmd_daily(args: argparse.Namespace) -> None:
    """The scheduled job: new data in, warehouse rebuilt, tomorrow forecast, site refreshed."""
    cmd_ingest(args)
    cmd_build(args)
    cmd_forecast(args)
    cmd_site(args)


def cmd_serve(args: argparse.Namespace) -> None:
    import functools
    import http.server

    from elmorgon.config import SITE_DIR

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    print(f"Elmorgon dashboard on http://127.0.0.1:{args.port}")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="elmorgon", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in [
        ("ingest", cmd_ingest, "download new prices and weather into bronze"),
        ("build", cmd_build, "validate into silver and rebuild the dbt warehouse"),
        ("backtest", cmd_backtest, "walk-forward backtest and SEK valuation (slow)"),
        ("forecast", cmd_forecast, "issue the forecast for the next unpublished day"),
        ("site", cmd_site, "export dashboard data"),
        ("daily", cmd_daily, "ingest + build + forecast + site"),
    ]:
        sub.add_parser(name, help=help_text).set_defaults(func=func)
    serve = sub.add_parser("serve", help="serve the dashboard locally")
    serve.add_argument("--port", type=int, default=8800)
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr
    )
    args.func(args)


if __name__ == "__main__":
    main()
