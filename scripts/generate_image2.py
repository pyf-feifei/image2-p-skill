#!/usr/bin/env python3
"""Compatibility entry point for the original image2-proxy command."""

from __future__ import annotations

import sys

from image2_p import run


def compatibility_args(argv: list[str]) -> list[str]:
    values = list(argv)
    if not any(value == "--out" or value.startswith("--out=") for value in values):
        values.extend(("--out", "output/imagegen/image2-skill-test.png"))
    if not any(
        value == "--response-out" or value.startswith("--response-out=")
        for value in values
    ):
        values.extend(
            ("--response-out", "output/imagegen/image2-skill-test-response.json")
        )
    return values


if __name__ == "__main__":
    raise SystemExit(run(compatibility_args(sys.argv[1:])))
