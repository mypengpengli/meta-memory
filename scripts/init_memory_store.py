#!/usr/bin/env python3
from __future__ import annotations

from _common import emit, ensure_store_ready, parse_args, store_root


def main() -> None:
    args = parse_args("Initialize a person-centered memory-data store.")
    root = store_root(args.store)
    result = ensure_store_ready(root)
    result["status"] = "ok"
    emit(result)


if __name__ == "__main__":
    main()
