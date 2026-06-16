#!/usr/bin/env python3
"""cld - interactive OpenStack tooling for a Ceph-backed cluster.

Thin entry point; all logic lives in the cld/ package. Equivalent to
`python3 -m cld`. Run `python3 cld.py --help` for subcommands.
"""
import sys

from cld.cli import main

if __name__ == "__main__":
    sys.exit(main())
