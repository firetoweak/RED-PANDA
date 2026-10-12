import { Alert, Box, Stack, Text } from "@mantine/core";

import type { ConversationView } from "../../api/contracts";

const EFFECT_LABEL = {
  failed: "文件未能恢复；本次回退没有修改文件",
  partially_restored: "文件部分回退，部分后续值已保留",
  preserved: "后续值已保留，本次没有修改文件",
  restored: "已执行文件回退",
  unchanged: "本次文件处理没有改变文件",
  kept: "文件保持现状，未跟随会话回退",
};

export function WorkspaceFileResultAlert({ result }: {
  result: ConversationView["workspace_file_result"];
}) {
  if (result === null) {
    return null;
  }
  const incomplete = ["failed", "partially_restored", "preserved", "kept"].includes(result.effect);
  const groups = [
    ["实际修改或删除", result.changed_files],
    ["其中已删除", result.deleted_files],
    ["保留后续值", result.preserved_files],
    ["未能处理", result.failed_files],
  ] as const;
  return (
    <Alert
      title="会话已回退 · 文件处理结果"
      color={result.effect === "failed" ? "red" : incomplete ? "yellow" : "blue"}
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
          {incomplete ? "当前文件可能与保留的对话记录不同。" : "机械回退不保证符合你的意图。"}
          请核对文件；需要调整时，可继续说明目标，让助手按内容修改。
        </Text>
      </Stack>
    </Alert>
  );
}
