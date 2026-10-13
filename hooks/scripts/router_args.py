"""Argument parsing for the hook router entry point."""

import argparse
import json
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified hook router")
    parser.add_argument("--event", required=True, help="Hook event name")
    return parser.parse_args()


def read_input() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return {}
