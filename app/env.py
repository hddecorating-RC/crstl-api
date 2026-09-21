"""Load .env into os.environ (setdefault: the process environment wins). Shared by
the web app and the worker entry points."""
import os
import pathlib


def load_env(path: str = ".env") -> None:
    env_file = pathlib.Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())
