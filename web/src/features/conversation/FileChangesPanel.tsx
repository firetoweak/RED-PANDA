import { ActionIcon, Alert, Button, Drawer, Group, Loader, Stack, Text } from "@mantine/core";
import { IconFileDiff, IconRefresh } from "@tabler/icons-react";
import { useState } from "react";
import { useGetFileChangesQuery, useGetFileDiffQuery } from "../../api/redpandaApi";
import type { ChangedFile } from "../../api/fileChanges";

const statuses = { added: "新增", deleted: "删除", modified: "修改" };

function Counts({ file }: { file: { added: number | null; removed: number | null } }) {
  return file.added === null || file.removed === null ? null : <>
    <span className="changes-added">+{file.added}</span>
    <span className="changes-removed">−{file.removed}</span>
  </>;
}

function Diff({ sessionId, file, refresh }: { sessionId: string; file: ChangedFile; refresh: () => void }) {
  const [offset, setOffset] = useState(0);
  const { currentData: data, isFetching, isError } = useGetFileDiffQuery({ sessionId, path: file.path, revision: file.revision, offset });
  if (isError) return <Alert color="orange">文件变化已更新或暂时无法读取。<Button variant="subtle" onClick={refresh}>刷新列表</Button></Alert>;
  if (data === undefined) return <Loader size="sm" />;
  return <Stack gap="xs">
    {data.partial && <Text size="xs" c="dimmed">部分统计：混合来源、二进制或不完整原文不计入精确行数；? 表示无法确定的旧行号。</Text>}
    {data.origin === "mixed" && <Text size="xs" c="orange">包含当前会话之外的变化，请结合来源标记审阅。</Text>}
    {data.binary && <Text size="sm">二进制或非 UTF-8 内容，无法显示文本差异。</Text>}
    {data.properties.map((property, index) => <Text key={index} size="xs" c="dimmed">{property.text}{property.origin === "external" ? "（会话外）" : ""}</Text>)}
    <div className="file-diff" aria-label={`${file.path} 的差异`} aria-busy={isFetching}>
      {data.lines.map((line, index) => <div key={`${offset}-${index}`} className={`diff-line diff-${line.kind}`}>
        <span className="diff-number">{line.old_line ?? (line.kind === "delete" || line.kind === "context" ? "?" : "")}</span>
        <span className="diff-number">{line.new_line ?? ""}</span>
        <span className="diff-sign">{line.kind === "add" ? "+" : line.kind === "delete" ? "−" : ""}</span>
        <span className="diff-text">{line.text.replace(/\r?\n$/, "")}</span>
        {line.kind !== "context" && line.kind !== "hunk" && line.origin !== "agent" &&
          <span className="diff-origin">{line.origin === "external" ? "会话外" : line.origin === "mixed" ? "混合" : "来源未知"}</span>}
      </div>)}
    </div>
    <Group justify="space-between">
      <Button variant="subtle" size="xs" disabled={offset === 0 || isFetching} onClick={() => setOffset(Math.max(0, offset - 200))}>上一页</Button>
      <Text size="xs" c="dimmed">第 {Math.floor(offset / 200) + 1} 页</Text>
      <Button variant="subtle" size="xs" disabled={data.next_offset === null || isFetching} onClick={() => { if (data.next_offset !== null) setOffset(data.next_offset); }}>下一页</Button>
    </Group>
  </Stack>;
}

export function FileChangesPanel({ sessionId }: { sessionId: string }) {
  const [opened, setOpened] = useState(false);
  const [path, setPath] = useState<string | null>(null);
  const { currentData: data, isFetching, isError, refetch } = useGetFileChangesQuery(sessionId, { refetchOnMountOrArgChange: true });
  const selected = data?.files.find((file) => file.path === path) ?? null;
  function refresh() { void refetch(); }
  return <>
    <Group justify="flex-end" gap="xs" className="file-changes-summary">
      <Button variant="default" size="compact-sm" radius="xl" leftSection={<IconFileDiff size={15} />}
        onClick={() => setOpened(true)} loading={data === undefined && isFetching}>
        {isError ? "文件变更暂不可用" : data === undefined ? "读取文件变更" : `${data.files.length} 个文件已更改`}
        {data !== undefined && !isError && <span className="changes-counts"><Counts file={data} />{data.partial && <span>部分统计</span>}</span>}
      </Button>
      <ActionIcon variant="subtle" color="gray" aria-label="刷新文件变更" loading={isFetching} onClick={refresh}><IconRefresh size={15} /></ActionIcon>
    </Group>
    <Drawer opened={opened} onClose={() => setOpened(false)} position="right" size="xl" title="会话变更">
      <Stack gap="md">
        <Group justify="space-between"><Text size="sm" c="dimmed">当前会话仍保留的净变更</Text><Button variant="subtle" size="xs" onClick={refresh} loading={isFetching}>刷新</Button></Group>
        {isError ? <Alert color="orange">无法读取文件变更，请刷新重试。</Alert> : data === undefined ? <Loader size="sm" /> : <>
          {data.files.length === 0 && <Text c="dimmed" size="sm">当前没有保留的会话变更。</Text>}
          <div className="changed-files">
            {data.files.map((file) => <button key={file.path} type="button" className={`changed-file${path === file.path ? " is-selected" : ""}`} onClick={() => setPath(file.path)}>
              <span className="changed-file-path">{file.path}</span><span>{statuses[file.status]}</span>
              <Counts file={file} />{file.partial && <span className="changes-partial">部分</span>}
            </button>)}
          </div>
          {selected !== null && <><Text size="sm" ff="monospace">{selected.path}</Text><Diff key={`${sessionId}:${selected.path}:${selected.revision}`} sessionId={sessionId} file={selected} refresh={refresh} /></>}
        </>}
      </Stack>
    </Drawer>
  </>;
}
