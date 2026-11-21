import os
from pathlib import Path
import time


def marker(name):
    return Path(os.environ["MCP_TEST_DIRECTORY"]) / name


def echo(arguments, _context):
    marker("echo.called").touch()
    print("PRIVATE-WORKER-STDOUT")
    return {"echo": arguments["text"], "result_marker": "PRIVATE-RESULT"}


def fail(_arguments, _context):
    raise RuntimeError("PRIVATE-EXCEPTION")


def slow(_arguments, _context):
    marker("slow.started").touch()
    while not marker("slow.release").exists():
        time.sleep(0.01)
    marker("slow.completed").touch()
    return {"late": True}


def protected_write(_arguments, context):
    if "fixture.write" not in context.grants:
        raise PermissionError("PRIVATE-PERMISSION")
    marker("protected.called").touch()
    return {"written": True}
