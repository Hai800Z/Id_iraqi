"""Command-line interface: ``idcard-extract`` (or ``python -m idcard_extractor``).

Exit codes: 0 success, 1 error (nothing was produced), 2 finished with problems
(for example some records could not be written to the database).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Optional

from idcard_extractor import __version__
from idcard_extractor.config import ConfigError, Settings, load_settings
from idcard_extractor.logging_utils import configure_logging

log = logging.getLogger("idcard_extractor")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="idcard-extract",
        description="Extract, cross-validate and store data from Iraqi unified national ID cards.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", help="settings file (default: the nearest .env above the working directory)")
    parser.add_argument("--log-level", help="override LOG_LEVEL: DEBUG, INFO, WARNING or ERROR")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", help="process card images (front and back sides)")
    run.add_argument("inputs", nargs="+", help="image files and/or folders")
    run.add_argument("--excel", help="Excel output file (default: OUTPUT_DIR/cards_<timestamp>.xlsx)")
    run.add_argument("--no-excel", action="store_true", help="do not write an Excel file")
    run.add_argument("--db", action="store_true", help="also write the accepted cards to the database")
    run.add_argument("--mapping", help="database mapping file (default: DB_MAPPING_FILE)")

    db = commands.add_parser("db", help="database utilities")
    db_commands = db.add_subparsers(dest="db_command", required=True, metavar="DB_COMMAND")
    check = db_commands.add_parser("check", help="compare the mapping with the database table (writes nothing)")
    init = db_commands.add_parser("init", help="create the mapped table if it does not exist (for trials)")
    retry = db_commands.add_parser("retry", help="write the records of a failed-records file again")
    retry.add_argument("file", nargs="?", help="failed-records file (default: DB_FAILED_RECORDS_FILE)")
    for sub in (check, init, retry):
        sub.add_argument("--mapping", help="database mapping file (default: DB_MAPPING_FILE)")

    return parser


# ----------------------------------------------------------------------------
# Database helpers
# ----------------------------------------------------------------------------

class DatabaseSetupError(Exception):
    pass


def _open_database(settings: Settings, mapping_arg: Optional[str]):
    """Return ``(engine, mapping)``; raise DatabaseSetupError with a readable message."""
    from sqlalchemy.exc import SQLAlchemyError

    from idcard_extractor.db.connection import DatabaseSettings, create_db_engine
    from idcard_extractor.db.mapping import load_mapping

    mapping_path = Path(mapping_arg) if mapping_arg else settings.db_mapping_file
    try:
        mapping = load_mapping(mapping_path)
        engine = create_db_engine(DatabaseSettings.from_env(), settings.base_dir, settings.mask_pii)
    except ValueError as exc:  # MappingError, DatabaseConfigError
        raise DatabaseSetupError(str(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseSetupError(f"Cannot create the database engine: {type(exc).__name__}") from exc
    return engine, mapping


def _prepare_writer(settings: Settings, mapping_arg: Optional[str], failed_records_file: Optional[Path]):
    from sqlalchemy.exc import SQLAlchemyError

    from idcard_extractor.db.writer import DatabaseWriter, describe_db_error

    engine, mapping = _open_database(settings, mapping_arg)
    writer = DatabaseWriter(engine, mapping, failed_records_file)
    try:
        writer.prepare()
    except ValueError as exc:  # MappingError
        raise DatabaseSetupError(str(exc)) from exc
    except SQLAlchemyError as exc:
        raise DatabaseSetupError(f"Cannot reach the database: {describe_db_error(exc)}") from exc
    return writer


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def cmd_run(args, settings: Settings) -> int:
    from idcard_extractor.pipeline import CardPipeline, collect_images

    images = collect_images(args.inputs)
    if not images:
        log.error("No images found in: %s", ", ".join(args.inputs))
        return EXIT_ERROR

    # Check the database before the slow part, so mistakes show up at once.
    writer = None
    problems: list[str] = []
    if args.db:
        try:
            writer = _prepare_writer(settings, args.mapping, settings.db_failed_records_file)
        except DatabaseSetupError as exc:
            log.error("%s", exc)
            log.error("The database stage is disabled for this run; the other outputs are still produced")
            problems.append("database stage disabled")

    pipeline = CardPipeline(settings)
    try:
        pipeline.load_models()
    except (FileNotFoundError, ImportError) as exc:
        log.error("Cannot load the models: %s", exc)
        return EXIT_ERROR

    log.info("Processing %d image(s)", len(images))
    result = pipeline.run(images)

    excel_path: Optional[Path] = None
    if not args.no_excel:
        from idcard_extractor.exporters.excel import export_to_excel

        excel_path = Path(args.excel) if args.excel else (
            settings.output_dir / f"cards_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        try:
            export_to_excel(result.records, excel_path, result.rejected)
        except OSError as exc:  # e.g. the file is open in Excel
            log.error("Cannot write %s: %s", excel_path, exc.strerror or exc)
            excel_path = None
            problems.append("Excel file not written")

    report = None
    if writer is not None:
        from idcard_extractor.db.mapping import record_context

        processed_at = datetime.now().replace(microsecond=0)
        report = writer.write(record_context(card, processed_at) for card in result.accepted)
        if not report.ok:
            problems.append("some records were not written to the database")

    print()
    print(f"Card sides detected : {len(result.sides)}")
    print(f"Accepted cards      : {len(result.accepted)}")
    print(f"Rejected            : {len(result.rejected)}")
    if excel_path:
        print(f"Excel file          : {excel_path}")
    if report is not None:
        print(f"Database            : {report.summary()}")
    for problem in problems:
        print(f"Problem             : {problem} (see the log above)")
    return EXIT_PARTIAL if problems else EXIT_OK


def cmd_db_check(args, settings: Settings) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from idcard_extractor.db.writer import DatabaseWriter, describe_db_error

    try:
        engine, mapping = _open_database(settings, args.mapping)
        issues = DatabaseWriter(engine, mapping).check()
    except DatabaseSetupError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR
    except SQLAlchemyError as exc:
        print(f"Cannot reach the database: {describe_db_error(exc)}", file=sys.stderr)
        return EXIT_ERROR

    for issue in issues:
        print(f"{issue.level.upper()}: {issue.message}")
    errors = sum(1 for issue in issues if issue.level == "error")
    status = "OK" if not errors else f"{errors} error(s)"
    print(f"{mapping.source} -> table {mapping.qualified_table!r} (mode={mapping.mode}): {status}")
    return EXIT_ERROR if errors else EXIT_OK


def cmd_db_init(args, settings: Settings) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from idcard_extractor.db.writer import create_table, describe_db_error

    try:
        engine, mapping = _open_database(settings, args.mapping)
        created = create_table(engine, mapping)
    except DatabaseSetupError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR
    except SQLAlchemyError as exc:
        print(f"Cannot create the table: {describe_db_error(exc)}", file=sys.stderr)
        return EXIT_ERROR

    if created:
        print(f"Table {mapping.qualified_table!r} created.")
    else:
        print(f"Table {mapping.qualified_table!r} already exists; nothing was changed.")
    return EXIT_OK


def cmd_db_retry(args, settings: Settings) -> int:
    from idcard_extractor.db.writer import load_failed_records, replace_failed_records

    path = Path(args.file) if args.file else settings.db_failed_records_file
    if path is None or not path.is_file():
        print(f"No failed-records file found at {path}.")
        return EXIT_OK
    try:
        records = load_failed_records(path)
    except (OSError, ValueError) as exc:
        print(f"Cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not records:
        print(f"{path} is empty; nothing to retry.")
        return EXIT_OK

    try:
        writer = _prepare_writer(settings, args.mapping, failed_records_file=None)
    except DatabaseSetupError as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    report = writer.write(records)
    replace_failed_records(path, report.failures, writer)
    print(f"Retried {len(records)} record(s): {report.summary()}")
    if report.failures:
        print(f"{report.failed} record(s) still failing are kept in {path}")
        return EXIT_PARTIAL
    print(f"All records written; {path} was removed.")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    configure_logging(args.log_level or settings.log_level, settings.log_file, settings.mask_pii)

    if args.command == "run":
        return cmd_run(args, settings)
    handlers = {"check": cmd_db_check, "init": cmd_db_init, "retry": cmd_db_retry}
    return handlers[args.db_command](args, settings)
