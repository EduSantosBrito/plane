import { describe, expect, it, vi, beforeEach } from "vitest";
import { Hocuspocus } from "@hocuspocus/server";
import type { afterUnloadDocumentPayload, onLoadDocumentPayload } from "@hocuspocus/server";

const { values, client } = vi.hoisted(() => {
  const values = new Map<string, string>();
  return {
    values,
    client: {
      set: vi.fn(async (key: string, owner: string) => {
        if (values.has(key)) return null;
        values.set(key, owner);
        return "OK";
      }),
      quit: vi.fn(async () => "OK"),
      eval: vi.fn(async (_script: string, _count: number, key: string, owner: string) => {
        if (values.get(key) === owner) values.delete(key);
      }),
    },
  };
});
vi.mock("@/redis", () => ({ redisManager: { getClient: () => ({ duplicate: () => client }) } }));
import { PageApiLock } from "../../src/extensions/page-api-lock";

const load = { documentName: "page-id" } as onLoadDocumentPayload;
const unload = { documentName: "page-id" } as afterUnloadDocumentPayload;

beforeEach(() => values.clear());

describe("Page API write coordination", () => {
  it("coordinates actual Hocuspocus load/store/unload hooks", async () => {
    const server = new Hocuspocus({ extensions: [new PageApiLock()], debounce: 0 });
    const connection = await server.openDirectConnection("page-id");
    expect(values.has("plane:page-write:page-id")).toBe(true);
    await connection.transact((document) => { document.getText("test").insert(0, "content"); });
    await connection.disconnect();
    await server.destroy();
    expect(values.has("plane:page-write:page-id")).toBe(false);
  });

  it("holds a live lock until the editor document has been stored and unloaded", async () => {
    const extension = new PageApiLock();
    await extension.onLoadDocument(load);
    expect(values.get("plane:page-write:page-id")).toMatch(/^live:/);
    await extension.afterUnloadDocument(unload);
    expect(values.has("plane:page-write:page-id")).toBe(false);
  });

  it("refuses to load a document while the REST API is writing it", async () => {
    values.set("plane:page-write:page-id", "api:in-progress");
    const extension = new PageApiLock();
    await expect(extension.onLoadDocument(load)).rejects.toThrow("Page is being changed");
    await extension.afterUnloadDocument(unload);
    expect(values.get("plane:page-write:page-id")).toBe("api:in-progress");
  });

  it("does not release another live instance's lock", async () => {
    const first = new PageApiLock();
    const second = new PageApiLock();
    await first.onLoadDocument(load);
    await second.afterUnloadDocument(unload);
    expect(values.has("plane:page-write:page-id")).toBe(true);
    await first.afterUnloadDocument(unload);
    expect(values.has("plane:page-write:page-id")).toBe(false);
  });
});
