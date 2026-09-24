"""Database engine creation from environment variables.

Credentials never appear in code or in the mapping file. They are read from the
environment (or ``.env``), and the URL is built with ``sqlalchemy.URL.create`` so
special characters in passwords are escaped correctly.

Supported: sqlite (default, no server needed), postgresql, mysql / mariadb, mssql.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.exc import ArgumentError

log = logging.getLogger(__name__)

DEFAULT_DRIVERS = {
    "sqlite": "sqlite",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pyodbc",
}
# pip extra (see pyproject.toml) that installs the default driver of each dialect.
DRIVER_EXTRAS = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "mssql": "mssql"}
SSL_MODES = ("disable", "require", "verify-ca", "verify-full")
DEFAULT_SQLITE_DB = "data/idcards.db"


class DatabaseConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseSettings:
    dialect: str = "sqlite"
    driver: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    name: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    ssl_mode: str = "disable"
    ssl_ca: Optional[str] = None
    ssl_cert: Optional[str] = None
    ssl_key: Optional[str] = None
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    connect_timeout: Optional[int] = 10
    url: Optional[str] = field(default=None, repr=False)
    echo: bool = False

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        def env(name: str, default: Optional[str] = None) -> Optional[str]:
            value = os.getenv(name)
            return value.strip() if value and value.strip() else default

        def env_int(name: str, default: Optional[int]) -> Optional[int]:
            value = env(name)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise DatabaseConfigError(f"{name} must be an integer, got {value!r}") from exc

        ssl_mode = (env("DB_SSL_MODE") or "disable").lower()
        if ssl_mode not in SSL_MODES:
            raise DatabaseConfigError(f"DB_SSL_MODE must be one of {', '.join(SSL_MODES)}")

        password = os.getenv("DB_PASSWORD") or None  # not stripped: passwords may contain spaces
        password_file = env("DB_PASSWORD_FILE")
        if password_file:
            if password:
                raise DatabaseConfigError("Set DB_PASSWORD or DB_PASSWORD_FILE, not both")
            try:
                password = Path(password_file).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as exc:
                raise DatabaseConfigError(f"Cannot read DB_PASSWORD_FILE: {exc.strerror}") from exc

        dialect = (env("DB_DIALECT") or "sqlite").lower()
        return cls(
            dialect=dialect,
            driver=env("DB_DRIVER"),
            host=env("DB_HOST"),
            port=env_int("DB_PORT", None),
            name=env("DB_NAME", DEFAULT_SQLITE_DB if dialect == "sqlite" else None),
            user=env("DB_USER"),
            password=password,
            ssl_mode=ssl_mode,
            ssl_ca=env("DB_SSL_CA"),
            ssl_cert=env("DB_SSL_CERT"),
            ssl_key=env("DB_SSL_KEY"),
            odbc_driver=env("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
            connect_timeout=env_int("DB_CONNECT_TIMEOUT", 10) or None,
            url=env("DATABASE_URL"),
            echo=(env("DB_ECHO") or "").lower() in {"1", "true", "yes", "on"},
        )


def build_url(settings: DatabaseSettings, base_dir: Optional[Path] = None) -> URL:
    """Build the connection URL; a relative SQLite path is resolved against ``base_dir``."""
    if settings.url:
        try:
            return make_url(settings.url)
        except ArgumentError as exc:
            # Do not echo the URL: it may contain the password.
            raise DatabaseConfigError("DATABASE_URL is not a valid database URL") from exc

    dialect = settings.dialect
    if dialect not in DEFAULT_DRIVERS:
        raise DatabaseConfigError(
            f"Unsupported DB_DIALECT {dialect!r}; use one of {', '.join(DEFAULT_DRIVERS)}"
        )
    drivername = settings.driver or DEFAULT_DRIVERS[dialect]

    if dialect == "sqlite":
        name = settings.name or DEFAULT_SQLITE_DB
        if name != ":memory:":
            path = Path(name).expanduser()
            if not path.is_absolute():
                path = (base_dir or Path.cwd()) / path
            name = str(path)
        return URL.create(drivername, database=name)

    missing = [var for var, value in (("DB_HOST", settings.host), ("DB_NAME", settings.name)) if not value]
    if missing:
        raise DatabaseConfigError(f"{' and '.join(missing)} must be set when DB_DIALECT={dialect}")

    query: dict[str, str] = {}
    if dialect == "mssql":
        query["driver"] = settings.odbc_driver
        if settings.ssl_mode == "disable":
            query["Encrypt"] = "no"
        else:
            query["Encrypt"] = "yes"
            # ODBC validates the chain and the host name together, so both verify
            # modes check the certificate fully; "require" only encrypts.
            query["TrustServerCertificate"] = "yes" if settings.ssl_mode == "require" else "no"

    return URL.create(
        drivername,
        username=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.name,
        query=query,
    )


def _mysql_ssl_args(settings: DatabaseSettings, driver: str) -> dict:
    mode = settings.ssl_mode
    if mode in {"verify-ca", "verify-full"} and not settings.ssl_ca:
        raise DatabaseConfigError(f"DB_SSL_MODE={mode} requires DB_SSL_CA for MySQL / MariaDB")

    files = {
        key: value
        for key, value in (("ca", settings.ssl_ca), ("cert", settings.ssl_cert), ("key", settings.ssl_key))
        if value
    }
    if driver == "pymysql":
        # A non-empty dict enables TLS; without a CA the certificate is not verified.
        return {"ssl": {**files, "check_hostname": mode == "verify-full"}}
    if driver == "mysqldb":
        modes = {"require": "REQUIRED", "verify-ca": "VERIFY_CA", "verify-full": "VERIFY_IDENTITY"}
        args: dict = {"ssl_mode": modes[mode]}
        if files:
            args["ssl"] = files
        return args

    log.warning("DB_SSL_MODE is not applied for driver %r; configure SSL in DATABASE_URL", driver)
    return {}


def connect_args(settings: DatabaseSettings, url: URL) -> dict:
    """Driver-specific arguments for TLS and the connection timeout."""
    backend, driver = url.get_backend_name(), url.get_driver_name()
    mode, timeout = settings.ssl_mode, settings.connect_timeout
    args: dict = {}

    if backend == "postgresql":
        if timeout:
            args["connect_timeout"] = timeout
        if mode != "disable":
            args["sslmode"] = mode
            for key, value in (("sslrootcert", settings.ssl_ca),
                               ("sslcert", settings.ssl_cert),
                               ("sslkey", settings.ssl_key)):
                if value:
                    args[key] = value
    elif backend in {"mysql", "mariadb"}:
        if timeout:
            args["connect_timeout"] = timeout
        if mode != "disable":
            args.update(_mysql_ssl_args(settings, driver))
    elif backend == "mssql":
        if timeout:
            args["timeout"] = timeout  # pyodbc login timeout
        if settings.ssl_ca:
            log.warning("DB_SSL_CA is ignored for SQL Server; add the CA to the operating system trust store")

    return args


def create_db_engine(
    settings: Optional[DatabaseSettings] = None,
    base_dir: Optional[Path] = None,
    hide_parameters: bool = True,
) -> Engine:
    """Create the engine. ``hide_parameters`` keeps record values out of SQL error
    messages and logs."""
    settings = settings or DatabaseSettings.from_env()
    url = build_url(settings, base_dir)
    backend = url.get_backend_name()

    if backend == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    try:
        engine = create_engine(
            url,
            connect_args=connect_args(settings, url),
            pool_pre_ping=True,
            echo=settings.echo,
            hide_parameters=hide_parameters,
        )
    except ModuleNotFoundError as exc:
        extra = DRIVER_EXTRAS.get(backend)
        hint = f' Install it with: pip install -e ".[{extra}]"' if extra else ""
        raise DatabaseConfigError(f"The database driver {exc.name!r} is not installed.{hint}") from exc

    # render_as_string hides the password by default.
    log.info("Database: %s (ssl=%s)", url.render_as_string(hide_password=True), settings.ssl_mode)
    return engine
