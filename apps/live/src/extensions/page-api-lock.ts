/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 */
import type Redis from "ioredis";
import { randomUUID } from "node:crypto";
import type { Extension, onLoadDocumentPayload, afterUnloadDocumentPayload } from "@hocuspocus/server";
import { redisManager } from "@/redis";

/**
 * Coordinate the single live instance with REST Page writers. Hold the lock
 * through the final store, so an editor can never overwrite replacement HTML
 * with an older Yjs document. Locks deliberately fail closed after a crash.
 */
export class PageApiLock implements Extension {
  name = "PageApiLock";
  priority = 10000;
  private readonly owner = `live:${randomUUID()}`;

  private client: Redis | undefined;

  private getClient() {
    if (!this.client) {
      const source = redisManager.getClient();
      if (!source) throw new Error("Page write coordination is unavailable");
      this.client = source.duplicate({ db: Number(process.env.PAGE_API_LOCK_REDIS_DB ?? "15") });
    }
    return this.client;
  }

  async onDestroy() {
    await this.client?.quit();
  }

  async onLoadDocument({ documentName }: onLoadDocumentPayload) {
    const redis = this.getClient();
    if (!redis) throw new Error("Page write coordination is unavailable");
    const key = `plane:page-write:${documentName}`;
    const acquired = await redis.set(key, this.owner, "NX");
    if (!acquired) throw new Error("Page is being changed through the API or requires lock recovery");
  }

  async afterUnloadDocument({ documentName }: afterUnloadDocumentPayload) {
    const redis = this.getClient();
    if (!redis) return;
    await redis.eval(
      "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end",
      1,
      `plane:page-write:${documentName}`,
      this.owner
    );
  }
}
