#!/usr/bin/env python3
"""Compatibility entry point for :mod:`meta_memory.http_api`.

The implementation is package-native so an installed wheel can run it from
any working directory.  Keep this wrapper for existing source-checkout usage
and imports made by older integrations.
"""
from __future__ import annotations

import sys
from pathlib import Path


try:
    from meta_memory.http_api import (  # noqa: F401
        APIServer,
        MemoryAPI,
        Principal,
        RequestIdentity,
        authorize,
        create_server,
        identity,
        load_principals,
        main,
        remember_args,
        retrieval_args,
        serve,
    )
except ModuleNotFoundError:  # direct ``python /path/to/scripts/memory_api.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from meta_memory.http_api import (  # noqa: F401
        APIServer,
        MemoryAPI,
        Principal,
        RequestIdentity,
        authorize,
        create_server,
        identity,
        load_principals,
        main,
        remember_args,
        retrieval_args,
        serve,
    )


if __name__ == "__main__":
    main()
