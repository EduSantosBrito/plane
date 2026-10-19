# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from functools import lru_cache
import os

from redis import Redis, ConnectionPool
from plane.settings.redis import redis_instance


@lru_cache(maxsize=1)
def page_lock_redis():
    # Django clears its cache DB during API startup. Keep coordination locks
    # in a dedicated DB so restarting API cannot unlock a live editor.
    source = redis_instance().connection_pool
    options = {**source.connection_kwargs, "db": int(os.environ.get("PAGE_API_LOCK_REDIS_DB", "15"))}
    return Redis(connection_pool=ConnectionPool(connection_class=source.connection_class, **options))
