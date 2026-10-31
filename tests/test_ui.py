import http.client
import json
import sys
import threading
import time

import pytest

from tiktok_archive.ui import server


def wait_until_finished(job, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.exit_code is not None:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_streams_output_and_reports_exit_code():
    job = server.Job()
    job.start("test", ["-c", "print('one'); print('two')"])
    wait_until_finished(job)
    snap = job.snapshot(0)
    assert snap["lines"] == ["one", "two"] and snap["exit_code"] == 0 and not snap["running"]
    assert job.snapshot(snap["next"])["lines"] == []  # polling from the end returns nothing new


def test_only_one_job_at_a_time_and_stop_works():
    job = server.Job()
    job.start("test", ["-c", "import time; time.sleep(30)"])
    with pytest.raises(RuntimeError):
        job.start("test", ["-c", "print(1)"])
    assert job.stop() is True
    wait_until_finished(job)
    assert job.snapshot(0)["stopped"] is True


def test_build_command_validates_input():
    assert server.build_command("process", {"limit": 5, "workers": 2})[-4:] == ["--limit", "5", "--workers", "2"]
    for bad in ({"limit": 0}, {"limit": 5, "workers": 99}, {}):
        with pytest.raises(ValueError):
            server.build_command("process", bad)
    with pytest.raises(ValueError):
        server.build_command("rm -rf", {})


def test_api_needs_the_token_and_the_right_host():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    server.Handler.token, server.Handler.port = "secret", port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path, headers=None, host=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.putrequest("GET", path, skip_host=True)
            c.putheader("Host", host or f"127.0.0.1:{port}")
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.read()

        assert get("/api/job")[0] == 403                                   # no token
        assert get("/api/job", {"X-UI-Token": "wrong"})[0] == 403
        assert get("/api/job", {"X-UI-Token": "secret"}, host="evil.example")[0] == 403  # rebinding
        status, body = get("/api/job", {"X-UI-Token": "secret"})
        assert status == 200 and json.loads(body)["running"] is False
        status, body = get("/")
        assert status == 200 and b"secret" in body                           # page carries this run's token
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_second_server_cannot_share_a_port():
    first = server.Server(("127.0.0.1", 0), server.Handler)
    try:
        with pytest.raises(OSError):
            server.Server(("127.0.0.1", first.server_address[1]), server.Handler)
    finally:
        first.server_close()
