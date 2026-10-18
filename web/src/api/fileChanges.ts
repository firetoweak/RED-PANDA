import { z } from "zod";

const count = z.number().int().nonnegative();
export const changedFileSchema = z.object({
  path: z.string().min(1),
  status: z.enum(["added", "deleted", "modified"]),
  added: count.nullable(),
  removed: count.nullable(),
  binary: z.boolean(),
  revision: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
export const fileChangesSchema = z.object({
  files: changedFileSchema.array(), added: count, removed: count,
}).strict();
export const fileDiffSchema = changedFileSchema.extend({
  properties: z.object({ text: z.string() }).strict().array(),
  lines: z.object({
    kind: z.enum(["hunk", "context", "add", "delete"]),
    old_line: count.nullable(), new_line: count.nullable(), text: z.string(),
  }).strict().array(),
  next_offset: count.nullable(),
}).strict();
export type ChangedFile = z.infer<typeof changedFileSchema>;
export type FileChanges = z.infer<typeof fileChangesSchema>;
export type FileDiff = z.infer<typeof fileDiffSchema>;
