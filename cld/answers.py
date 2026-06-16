"""Save / replay a createvm spec as YAML."""

import sys

try:
    import yaml
except ImportError:
    yaml = None

from cld.ui import out, warn, err


def save_answers(path, spec):
    if yaml is None:
        warn("PyYAML not available; cannot save answers")
        return
    serial = {k: v for k, v in spec.items()}
    with open(path, "w") as f:
        yaml.safe_dump(serial, f, sort_keys=False)
    out(f"[dim]Saved answers to {path}[/dim]")


def load_answers(path):
    if yaml is None:
        err("PyYAML required for --non-interactive")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)
