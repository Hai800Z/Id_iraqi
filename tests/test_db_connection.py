import pytest
from sqlalchemy import text

from idcard_extractor.db.connection import (
    DatabaseConfigError,
    DatabaseSettings,
    build_url,
    connect_args,
    create_db_engine,
)


def test_sqlite_path_is_relative_to_base_dir(tmp_path):
    url = build_url(DatabaseSettings(), base_dir=tmp_path)
    assert url.drivername == "sqlite"
    assert url.database == str(tmp_path / "data" / "idcards.db")


def test_sqlite_engine_creates_the_folder(tmp_path):
    engine = create_db_engine(DatabaseSettings(name="db/test.db"), base_dir=tmp_path)
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1
    assert (tmp_path / "db" / "test.db").is_file()


def test_server_database_needs_host_and_name():
    with pytest.raises(DatabaseConfigError, match="DB_HOST and DB_NAME"):
        build_url(DatabaseSettings(dialect="postgresql"))


def test_password_with_special_characters_is_escaped_and_hidden():
    password = "p@ss:w/rd#?%"
    settings = DatabaseSettings(dialect="postgresql", host="db.local", name="registry",
                                user="app", password=password)
    url = build_url(settings)
    assert url.password == password
    assert url.drivername == "postgresql+psycopg"
    assert password not in url.render_as_string(hide_password=True)
    assert password not in repr(settings)


def test_postgres_tls_and_timeout():
    settings = DatabaseSettings(dialect="postgresql", host="h", name="n", ssl_mode="verify-full",
                                ssl_ca="/certs/ca.pem", connect_timeout=5)
    assert connect_args(settings, build_url(settings)) == {
        "connect_timeout": 5, "sslmode": "verify-full", "sslrootcert": "/certs/ca.pem",
    }


@pytest.mark.parametrize(
    ("mode", "encrypt", "trust"),
    [("disable", "no", None), ("require", "yes", "yes"), ("verify-ca", "yes", "no"), ("verify-full", "yes", "no")],
)
def test_sql_server_encryption(mode, encrypt, trust):
    url = build_url(DatabaseSettings(dialect="mssql", host="h", name="n", ssl_mode=mode))
    assert url.query["Encrypt"] == encrypt
    assert url.query.get("TrustServerCertificate") == trust
    assert url.query["driver"] == "ODBC Driver 18 for SQL Server"


def test_mysql_tls_arguments():
    require = DatabaseSettings(dialect="mysql", host="h", name="n", ssl_mode="require")
    assert connect_args(require, build_url(require))["ssl"] == {"check_hostname": False}

    full = DatabaseSettings(dialect="mysql", host="h", name="n", ssl_mode="verify-full", ssl_ca="ca.pem")
    assert connect_args(full, build_url(full))["ssl"] == {"ca": "ca.pem", "check_hostname": True}

    mysqldb = DatabaseSettings(dialect="mysql", driver="mysql+mysqldb", host="h", name="n",
                               ssl_mode="verify-full", ssl_ca="ca.pem")
    assert connect_args(mysqldb, build_url(mysqldb))["ssl_mode"] == "VERIFY_IDENTITY"

    no_ca = DatabaseSettings(dialect="mysql", host="h", name="n", ssl_mode="verify-ca")
    with pytest.raises(DatabaseConfigError, match="DB_SSL_CA"):
        connect_args(no_ca, build_url(no_ca))


def test_from_env(monkeypatch, tmp_path):
    secret = tmp_path / "password.txt"
    secret.write_text("s3cret with spaces\n", encoding="utf-8")
    monkeypatch.setenv("DB_DIALECT", "PostgreSQL")
    monkeypatch.setenv("DB_HOST", "db.local")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DB_SSL_MODE", "Verify-Full")

    settings = DatabaseSettings.from_env()
    assert settings.dialect == "postgresql"
    assert settings.port == 6543
    assert settings.password == "s3cret with spaces"
    assert settings.ssl_mode == "verify-full"
    assert settings.name is None  # no SQLite default for server databases


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [("DB_PORT", "abc", "DB_PORT"), ("DB_SSL_MODE", "maybe", "DB_SSL_MODE"), ("DB_CONNECT_TIMEOUT", "x", "integer")],
)
def test_invalid_environment_values(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(DatabaseConfigError, match=message):
        DatabaseSettings.from_env()


def test_password_and_password_file_are_exclusive(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PASSWORD", "a")
    monkeypatch.setenv("DB_PASSWORD_FILE", str(tmp_path / "p.txt"))
    with pytest.raises(DatabaseConfigError, match="not both"):
        DatabaseSettings.from_env()


def test_database_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("DB_DIALECT", "mysql")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///from_url.db")
    assert build_url(DatabaseSettings.from_env()).database == "from_url.db"


def test_invalid_database_url_does_not_leak_it():
    with pytest.raises(DatabaseConfigError) as info:
        build_url(DatabaseSettings(url="not a url with secret-password"))
    assert "secret-password" not in str(info.value)


def test_missing_driver_gives_install_hint():
    pytest.importorskip("sqlalchemy")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("psycopg is installed")
    settings = DatabaseSettings(dialect="postgresql", host="h", name="n")
    with pytest.raises(DatabaseConfigError, match=r'pip install -e "\.\[postgres\]"'):
        create_db_engine(settings)
