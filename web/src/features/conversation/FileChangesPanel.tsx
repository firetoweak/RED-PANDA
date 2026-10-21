import { ActionIcon, Alert, Button, Group, Loader, ScrollArea, Stack, Text } from "@mantine/core";
import { IconFileDiff, IconFolder, IconRefresh } from "@tabler/icons-react";
import { useState } from "react";
import { useGetFileChangesQuery, useGetFileDiffQuery } from "../../api/redpandaApi";
import type { ChangedFile } from "../../api/fileChanges";

const statuses = { added: "新增", deleted: "删除", modified: "修改" };
const statusMarks = { added: "A", deleted: "D", modified: "M" };

function Counts({ file }: { file: { added: number | null; removed: number | null } }) {
  return file.added === null || file.removed === null ? null : <>
    <span className="changes-added">+{file.added}</span>
    <span className="changes-removed">−{file.removed}</span>
  </>;
}

function Diff({ sessionId, project, file, refresh }: { sessionId: string; project?: string; file: ChangedFile; refresh: () => void }) {
  const [offset, setOffset] = useState(0);
  const { currentData: data, isFetching, isError, refetch } = useGetFileDiffQuery({
    sessionId, ...(project === undefined ? {} : { project }), path: file.path, revision: file.revision, offset });
  if (isError) return <Alert color="orange">文件变化已更新或暂时无法读取。<Button variant="subtle" onClick={() => { refresh(); void refetch(); }}>刷新列表</Button></Alert>;
  if (data === undefined) return <Loader size="sm" />;
  return <div className="file-diff-page">
    <ScrollArea key={offset} className="file-diff-scroll" type="hover">
      <Stack gap="xs" p="sm">
      {data.binary && <Text size="sm">二进制或非 UTF-8 内容，无法显示文本差异。</Text>}
      {data.properties.map((property, index) => <Text key={index} size="xs" c="dimmed">{property.text}</Text>)}
      <div className="file-diff" aria-label={`${file.path} 的差异`} aria-busy={isFetching}>
        {data.lines.map((line, index) => <div key={`${offset}-${index}`} className={`diff-line diff-${line.kind}`}>
          <span className="diff-number">{line.old_line ?? (line.kind === "delete" || line.kind === "context" ? "?" : "")}</span>
          <span className="diff-number">{line.new_line ?? ""}</span>
          <span className="diff-sign">{line.kind === "add" ? "+" : line.kind === "delete" ? "−" : ""}</span>
          <span className="diff-text">{line.text.replace(/\r?\n$/, "")}</span>
        </div>)}
      </div>
      </Stack>
    </ScrollArea>
    <Group className="file-diff-pagination" justify="space-between">
      <Button variant="subtle" size="xs" disabled={offset === 0 || isFetching} onClick={() => setOffset(Math.max(0, offset - 200))}>上一页</Button>
      <Text size="xs" c="dimmed">第 {Math.floor(offset / 200) + 1} 页</Text>
      <Button variant="subtle" size="xs" disabled={data.next_offset === null || isFetching} onClick={() => { if (data.next_offset !== null) setOffset(data.next_offset); }}>下一页</Button>
    </Group>
  </div>;
}

export function FileChangesSummary({ sessionId, project, onOpen }: { sessionId: string; project?: string; onOpen: () => void }) {
  const { currentData: data, isFetching, isError, refetch } = useGetFileChangesQuery(
    { sessionId, ...(project === undefined ? {} : { project }) }, { refetchOnMountOrArgChange: true });
  const prefix = project === undefined ? "" : `${project} · `;
  function refresh() { void refetch(); }
  return <Group justify="flex-end" gap="xs" className="file-changes-summary">
      <Button variant="default" size="compact-sm" radius="xl" leftSection={<IconFileDiff size={15} />}
        onClick={onOpen} loading={data === undefined && isFetching}>
        {prefix}{isError ? "文件变更暂不可用" : data === undefined ? "读取文件变更" : `${data.files.length} 个文件已更改`}
        {data !== undefined && !isError && <span className="changes-counts"><Counts file={data} /></span>}
      </Button>
      <ActionIcon variant="subtle" color="gray" aria-label={`刷新${prefix}文件变更`} loading={isFetching} onClick={refresh}><IconRefresh size={15} /></ActionIcon>
    </Group>;
}

function FileTree({ files, selected, onSelect, prefix = "" }: {
  files: ChangedFile[]; selected: string | undefined; onSelect: (path: string) => void; prefix?: string;
}) {
  const folders = new Map<string, ChangedFile[]>();
  const leaves: ChangedFile[] = [];
  for (const file of files) {
    const rest = file.path.slice(prefix.length);
    const slash = rest.indexOf("/");
    if (slash < 0) leaves.push(file);
    else {
      const folder = rest.slice(0, slash);
      const group = folders.get(folder) ?? [];
      group.push(file);
      folders.set(folder, group);
    }
  }
  return <>
    {[...folders].map(([folder, children]) => <details className="file-tree-group" open key={folder}>
      <summary><IconFolder size={13} /><span>{folder}</span></summary>
      <div className="file-tree-children"><FileTree files={children} selected={selected}
        onSelect={onSelect} prefix={`${prefix}${folder}/`} /></div>
    </details>)}
    {leaves.map((file) => <button key={file.path} type="button" title={`${file.path} · ${statuses[file.status]}`}
      aria-label={`查看 ${file.path} 的差异`} aria-pressed={selected === file.path}
      className={`changed-file${selected === file.path ? " is-selected" : ""}`} onClick={() => onSelect(file.path)}>
      <span className={`file-change-status file-change-${file.status}`} aria-label={statuses[file.status]}>{statusMarks[file.status]}</span>
      <span className="changed-file-path">{file.path.slice(prefix.length)}</span>
      <span className="changes-counts"><Counts file={file} /></span>
    </button>)}
  </>;
}

export function FileChangesPanel({ sessionId, project }: { sessionId: string; project?: string }) {
  const [filesOpened, setFilesOpened] = useState(true);
  const [path, setPath] = useState<string | null>(null);
  const { currentData: data, isFetching, isError, refetch } = useGetFileChangesQuery(
    { sessionId, ...(project === undefined ? {} : { project }) });
  const selected = data?.files.find((file) => file.path === path) ?? data?.files[0];
  function refresh() { void refetch(); }

  return <div className="file-changes-page">
    <Group className="content-page-toolbar" justify="space-between" wrap="nowrap" gap="xs">
      <Text size="xs" ff="monospace" truncate style={{ flex: 1, minWidth: 0 }} title={selected?.path}>
        {selected?.path ?? "当前会话仍保留的净变更"}
      </Text>
      <Group gap="xs" wrap="nowrap" style={{ flexShrink: 0 }}>
        {selected === undefined ? null : <span className="changes-counts"><Counts file={selected} /></span>}
        <ActionIcon variant="subtle" aria-label="刷新文件变更" loading={isFetching} onClick={refresh}><IconRefresh size={15} /></ActionIcon>
        <Button size="compact-xs" variant={filesOpened ? "light" : "subtle"} aria-expanded={filesOpened}
          onClick={() => setFilesOpened((value) => !value)}>文件列表</Button>
      </Group>
    </Group>
    <div className="file-changes-body">
      <div className="file-changes-diff">
        {isError ? <Alert color="orange" m="sm">无法读取文件变更，请刷新重试。</Alert>
          : data === undefined ? <Loader size="sm" m="md" />
          : selected === undefined ? <Text c="dimmed" size="sm" p="md">当前没有保留的会话变更。</Text>
          : <Diff key={`${sessionId}:${project ?? ""}:${selected.path}:${selected.revision}`} sessionId={sessionId}
            project={project} file={selected} refresh={refresh} />}
      </div>
      {!filesOpened ? null : <aside className="file-changes-files" aria-label="改动文件列表">
        <Text size="xs" fw={600} p="sm">改动文件 · {data?.files.length ?? 0}</Text>
        <ScrollArea className="file-tree-scroll" type="hover">
          <div className="changed-files"><FileTree files={data?.files ?? []} selected={selected?.path} onSelect={setPath} /></div>
        </ScrollArea>
      </aside>}
    </div>
  </div>;
}
