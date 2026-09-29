#!/usr/bin/env python3
"""Local-only AWS/DuckLake refresh regression. Requires a matching libduckdb + extensions.

python3 upstream_refresh_probe.py /path/to/libduckdb.dylib [--extensions /path/to/extensions]
The library may instead have httpfs, aws and ducklake linked in. No pip packages,
AWS account, real credentials, network service or sleeping until expiration is needed.
"""
import argparse
import ctypes as c
import datetime
import http.server
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import urllib.parse


class Result(c.Structure):
    _fields_ = [
        (name, kind)
        for name, kind in (
            ("columns", c.c_uint64),
            ("rows", c.c_uint64),
            ("changed", c.c_uint64),
            ("column_data", c.c_void_p),
            ("error", c.c_void_p),
            ("internal", c.c_void_p),
        )
    ]


class API:
    def __init__(self, library):
        self.lib = c.CDLL(str(Path(library).resolve()))
        signatures = {
            "duckdb_create_config": (c.c_int, [c.POINTER(c.c_void_p)]),
            "duckdb_set_config": (c.c_int, [c.c_void_p, c.c_char_p, c.c_char_p]),
            "duckdb_destroy_config": (None, [c.POINTER(c.c_void_p)]),
            "duckdb_open_ext": (c.c_int, [c.c_char_p, c.POINTER(c.c_void_p), c.c_void_p, c.POINTER(c.c_void_p)]),
            "duckdb_close": (None, [c.POINTER(c.c_void_p)]),
            "duckdb_connect": (c.c_int, [c.c_void_p, c.POINTER(c.c_void_p)]),
            "duckdb_disconnect": (None, [c.POINTER(c.c_void_p)]),
            "duckdb_query": (c.c_int, [c.c_void_p, c.c_char_p, c.POINTER(Result)]),
            "duckdb_result_error": (c.c_char_p, [c.POINTER(Result)]),
            "duckdb_value_varchar": (c.c_void_p, [c.POINTER(Result), c.c_uint64, c.c_uint64]),
            "duckdb_destroy_result": (None, [c.POINTER(Result)]),
            "duckdb_free": (None, [c.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args

    def open(self):
        config, db, error = c.c_void_p(), c.c_void_p(), c.c_void_p()
        assert self.lib.duckdb_create_config(c.byref(config)) == 0
        try:
            for name, value in {
                "allow_unsigned_extensions": "true",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
            }.items():
                assert self.lib.duckdb_set_config(config, name.encode(), value.encode()) == 0
            if self.lib.duckdb_open_ext(None, c.byref(db), config, c.byref(error)):
                message = c.string_at(error).decode() if error else "duckdb_open_ext failed"
                self.lib.duckdb_free(error)
                raise RuntimeError(message)
        finally:
            self.lib.duckdb_destroy_config(c.byref(config))
        return db

    def connect(self, db):
        con = c.c_void_p()
        assert self.lib.duckdb_connect(db, c.byref(con)) == 0
        return con

    def sql(self, con, query, scalar=False):
        result = Result()
        try:
            if self.lib.duckdb_query(con, query.encode(), c.byref(result)):
                raise RuntimeError(self.lib.duckdb_result_error(c.byref(result)).decode())
            if scalar:
                value = self.lib.duckdb_value_varchar(c.byref(result), 0, 0)
                try:
                    return c.string_at(value).decode() if value else None
                finally:
                    self.lib.duckdb_free(value)
        finally:
            self.lib.duckdb_destroy_result(c.byref(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library")
    parser.add_argument("--extensions", type=Path)
    args = parser.parse_args()
    # Prevent the mock credential-chain test from using the caller's real credentials.
    for name in list(os.environ):
        if name.startswith("AWS_"):
            del os.environ[name]
    with tempfile.TemporaryDirectory(prefix="duckdb-secret-refresh-") as directory:
        work = Path(directory)
        (work / "empty-config").write_text("")
        state = {"expired": False}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reply(self, status, body=b"", headers=None):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/credentials":
                    body = json.dumps(
                        {
                            "AccessKeyId": "FAKENEWKEY" if state["expired"] else "FAKEOLDKEY",
                            "SecretAccessKey": "fake-secret-never-valid-in-aws",
                            "Token": "new" if state["expired"] else "old",
                            "Expiration": (
                                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        }
                    ).encode()
                    self.reply(200, body, {"Content-Type": "application/json"})
                    return
                if state["expired"] and self.headers.get("x-amz-security-token") != "new":
                    self.reply(
                        403,
                        b"<Error><Code>ExpiredToken</Code><Message>mock expiration</Message></Error>",
                        {"Content-Type": "application/xml"},
                    )
                    return
                data = (work / "file.parquet").read_bytes()
                if "list-type" in urllib.parse.parse_qs(url.query):
                    body = (
                        "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                        "<Name>probe</Name><Prefix>run/</Prefix><KeyCount>1</KeyCount>"
                        "<IsTruncated>false</IsTruncated><Contents><Key>run/part/file.parquet</Key>"
                        "<LastModified>2026-09-28T12:00:00.000Z</LastModified>"
                        "<ETag>&quot;mock&quot;</ETag><Size>" + str(len(data)) + "</Size>"
                        "<StorageClass>STANDARD</StorageClass></Contents></ListBucketResult>"
                    ).encode()
                    self.reply(200, body, {"Content-Type": "application/xml"})
                    return
                status = 200
                headers = {"ETag": '"mock"', "Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
                byte_range = self.headers.get("Range")
                if byte_range:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", byte_range)
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else len(data) - 1, len(data) - 1)
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                    data, status = data[start : end + 1], 206
                self.reply(status, data, headers)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"127.0.0.1:{server.server_port}"
        os.environ.update(
            {
                "AWS_CONTAINER_CREDENTIALS_FULL_URI": f"http://{endpoint}/credentials",
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_REGION": "us-west-2",
                "AWS_CONFIG_FILE": str(work / "empty-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(work / "empty-config"),
            }
        )
        api = API(args.library)
        s3 = "s3://probe/run/**/*.parquet"
        create_secret = (
            "CREATE OR REPLACE SECRET secret (TYPE S3, PROVIDER credential_chain, "
            f"REFRESH 'auto', REGION 'us-west-2', ENDPOINT '{endpoint}', URL_STYLE 'path', USE_SSL false)"
        )
        failures = 0
        try:
            for scenario in ("outer transaction then DuckLake internal connection", "older transaction snapshot"):
                db = api.open()
                con = api.connect(db)
                stale = None
                state["expired"] = False
                try:
                    for extension in ("httpfs", "aws", "ducklake"):
                        target = (
                            str(args.extensions.resolve() / f"{extension}.duckdb_extension")
                            if args.extensions
                            else extension
                        )
                        api.sql(con, "LOAD '" + target.replace("'", "''") + "'")
                    api.sql(con, "SET threads=1")
                    api.sql(con, "SET enable_external_file_cache=false")
                    api.sql(con, "SET http_retries=0")
                    print(
                        "VERSION",
                        api.sql(con, "SELECT library_version || ' ' || source_id FROM pragma_version()", True),
                        flush=True,
                    )
                    api.sql(con, f"COPY (SELECT 42::INTEGER AS i) TO '{work / 'file.parquet'}' (FORMAT PARQUET)")
                    api.sql(con, create_secret)
                    if scenario.startswith("outer"):
                        api.sql(con, f"ATTACH 'ducklake:{work / 'lake.duckdb'}' AS lake (DATA_PATH '{work / 'data'}')")
                        api.sql(con, "CREATE TABLE lake.t(i INTEGER)")
                        state["expired"] = True
                        api.sql(con, "BEGIN")
                        api.sql(con, "DROP TABLE lake.t")
                        api.sql(con, f"CREATE TABLE lake.t AS SELECT * FROM read_parquet('{s3}') LIMIT 0")
                        api.sql(con, f"CALL ducklake_add_data_files('lake', 't', '{s3}')")
                        api.sql(con, "COMMIT")
                        assert api.sql(con, "SELECT count(*) FROM lake.t", True) == "1"
                    else:
                        stale = api.connect(db)
                        api.sql(stale, "BEGIN")
                        initial = api.sql(
                            stale,
                            "SELECT contains(secret_string, 'FAKEOLDKEY') FROM duckdb_secrets() WHERE name='secret'",
                            True,
                        )
                        assert initial == "true", f"Expected initial old credentials, got {initial!r}"
                        state["expired"] = True
                        api.sql(con, create_secret)
                        assert api.sql(stale, f"SELECT count(*) FROM read_parquet('{s3}')", True) == "1"
                        api.sql(stale, "COMMIT")
                    print("PASS", scenario, flush=True)
                except Exception as error:
                    failures += 1
                    print("FAIL", scenario, str(error), flush=True)
                finally:
                    if stale:
                        api.lib.duckdb_disconnect(c.byref(stale))
                    api.lib.duckdb_disconnect(c.byref(con))
                    api.lib.duckdb_close(c.byref(db))
        finally:
            server.shutdown()
            server.server_close()
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
