import { Alert, Box, Stack, Text } from "@mantine/core";

import type { WorkspaceFileResult } from "../../api/contracts";

/** 只有文件与回退后的会话对不上、且不是用户主动选择时才提示。 */
type AlertEffect = "partially_restored" | "preserved" | "failed";

const EFFECT_LABEL: Record<AlertEffect, string> = {
  failed: "文件未能恢复；本次回退没有修改文件",
  partially_restored: "文件部分回退，部分后续值已保留",
  preserved: "后续值已保留，本次没有修改文件",
};

function is_alert_effect(effect: WorkspaceFileResult["effect"]): effect is AlertEffect {
  return (
    effect === "partially_restored"
    || effect === "preserved"
    || effect === "failed"
  );
}

export function WorkspaceFileResultAlert({
  result,
}: {
  result: WorkspaceFileResult | null;
}) {
  if (result === null || !is_alert_effect(result.effect)) {
    return null;
  }
  const groups = [
    ["实际修改或删除", result.changed_files],
    ["其中已删除", result.deleted_files],
    ["保留后续值", result.preserved_files],
    ["未能处理", result.failed_files],
  ] as const;
  return (
    <Alert
      title="会话已回退 · 文件处理结果"
      color={result.effect === "failed" ? "red" : "yellow"}
      role="status"
    >
      <Stack gap={6}>
        <Text size="sm" fw={600}>{EFFECT_LABEL[result.effect]}</Text>
        {result.error === null ? null : <Text className="pre-wrap" size="sm">{result.error}</Text>}
        <Box mah={220} style={{ overflowY: "auto" }}>
          {groups.map(([label, files]) => files.length === 0 ? null : (
            <Box key={label} mb={6}>
              <Text size="xs" fw={600}>{label}（{files.length}）</Text>
              {files.map((path) => <Text key={path} size="xs" ff="monospace" style={{ overflowWrap: "anywhere" }}>{path}</Text>)}
            </Box>
          ))}
        </Box>
        {result.policy !== "original" ? null : (
          <Text size="xs">原值策略允许覆盖助手改变位置的后续值，或删除助手创建后又被修改的文件。</Text>
        )}
        <Text size="xs">
          当前文件可能与保留的对话记录不同。请核对文件；需要调整时，可继续说明目标，让助手按内容修改。
        </Text>
      </Stack>
    </Alert>
  );
}
