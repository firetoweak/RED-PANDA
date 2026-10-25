import { z } from "zod";

const identity = {
  id: z.string().min(1),
  name: z.string().min(1),
  description: z.string(),
};
const origin = z.enum(["builtin", "installed"]);
export const capabilityInventorySchema = z.object({
  skills: z.array(z.object({
    ...identity, origin, enabled: z.boolean(), source: z.string(),
    version: z.string().nullable(), path: z.string(),
  }).strict()),
  mcp: z.array(z.object({
    ...identity, origin, enabled: z.boolean(), transport: z.enum(["stdio", "streamable_http"]),
    read_only: z.boolean(), last_status: z.enum(["unknown", "available", "unavailable"]),
    last_checked_at: z.string().nullable(), last_error_summary: z.string().nullable(),
  }).strict()),
  cli: z.array(z.object({
    ...identity, source: z.string(), version: z.string().nullable(), path: z.string().nullable(),
    health: z.object({
      help_ok: z.boolean(), version_ok: z.boolean(), help_mentions_json: z.boolean(), checked_at: z.string(),
    }).strict().nullable(),
  }).strict()),
}).strict();

export const capabilityEnabledSchema = z.object({ enabled: z.boolean() }).strict();
export type CapabilityInventory = z.infer<typeof capabilityInventorySchema>;
