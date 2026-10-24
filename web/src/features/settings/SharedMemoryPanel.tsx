import { Alert, Button, Group, Stack, Text, Textarea } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { sharedMemoryConflictSchema, type SharedMemory } from "../../api/contracts";
import { useGetSharedMemoryQuery, useSaveSharedMemoryMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";

export function SharedMemoryPanel({ active }: { active: boolean }) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data, error, refetch } = useGetSharedMemoryQuery(undefined, {
    skip: !active, pollingInterval: active ? 5000 : 0, refetchOnMountOrArgChange: true,
  });
  const [save, { isLoading: saving, error: saveError, reset }] = useSaveSharedMemoryMutation();
  const [base, setBase] = useState<SharedMemory | null>(null);
  const [content, setContent] = useState("");
  const [notice, setNotice] = useState("");
  const pending = useRef<{ deliveryId: string; version: number; content: string } | null>(null);
  const conflictResponse = sharedMemoryConflictSchema.safeParse(saveError);
  const returned = conflictResponse.success ? conflictResponse.data.current : undefined;
  const latest = returned && (!data || returned.version > data.version) ? returned : data;

  useEffect(() => {
    if (!latest || (base && latest.version <= base.version)) return;
    if (base === null || content === base.content) {
      setBase(latest);
      setContent(latest.content);
    }
  }, [latest, base, content]);

  if (base === null) return <Stack>
    <Text c={error ? "red" : "dimmed"} role={error ? "alert" : undefined}>
      {error ? "读取共同记忆失败：" + JSON.stringify(error) : "正在读取共同记忆…"}
    </Text>
    {error ? <Button variant="subtle" onClick={() => void refetch()}>重新读取</Button> : null}
  </Stack>;

  const count = Array.from(content).length;
  const changed = content !== base.content;
  const conflict = latest !== undefined && latest.version > base.version;

  async function submit() {
    if (connectionId === null || base === null) return;
    if (!pending.current || pending.current.version !== base.version || pending.current.content !== content) {
      pending.current = { deliveryId: crypto.randomUUID(), version: base.version, content };
    }
    try {
      const updated = await save({ connectionId, ...pending.current }).unwrap();
      setBase(updated);
      setContent(updated.content);
      pending.current = null;
      setNotice("已保存。各 Bot 在下一次工作时会看到新内容。");
    } catch { setNotice(""); } // mutation 状态呈现请求失败；重试同一正文沿用操作身份。
  }

  return <Stack gap="lg">
    <div><Text size="sm" c="dimmed">所有 Bot 共用的团队笔记，只保留长期协作约定和重要结论。</Text>
      <Text size="xs" c="dimmed" mt={8}>{base.updated_by === null ? "尚未写入共同记忆，可以留空。" : `最近修改：${base.updated_by}`}</Text></div>
    {error ? <Alert color="red" title="读取失败">草稿已保留。<Text size="sm">{JSON.stringify(error)}</Text><Button variant="subtle" size="xs" onClick={() => void refetch()}>重新读取</Button></Alert> : null}
    {conflict ? <Alert color="orange" title="共同记忆已有更新">
      <Text size="sm">你的草稿已保留。请核对最新正文，再整理需要保存的内容。</Text>
      <Text size="sm" mt="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{latest.content || "（最新正文为空）"}</Text>
      <Button size="xs" variant="light" mt="sm" onClick={() => { setBase(latest); reset(); }}>已核对，继续编辑</Button>
    </Alert> : null}
    <div><Textarea label="共同记忆正文" description="最多500字，标点、空白和换行也计数。可以清空后保存。"
      placeholder="只记大家长期需要知道的事，也可以留空。" autosize minRows={10} maxRows={20}
      value={content} disabled={saving} error={count > 500 ? "超过500字，请精简后保存。" : undefined}
      onChange={(event) => { setContent(event.currentTarget.value); setNotice(""); }} />
      <Text size="xs" c={count > 500 ? "red" : "dimmed"} ta="right" mt={6} aria-live="polite">{count} / 500</Text></div>
    {saveError && !conflictResponse.success ? <Alert color="red" title="保存失败">草稿已保留，请重试。<Text size="sm">{JSON.stringify(saveError)}</Text></Alert> : null}
    {notice ? <Text role="status" size="sm" c="dimmed">{notice}</Text> : null}
    <Group gap="sm">
      <Button onClick={() => void submit()} loading={saving} disabled={connectionId === null || !changed || count > 500 || conflict}>保存记忆</Button>
      <Button variant="subtle" disabled={saving || !changed} onClick={() => {
        const current = latest && latest.version >= base.version ? latest : base;
        setBase(current); setContent(current.content); setNotice(""); reset();
      }}>撤销修改</Button>
    </Group>
    <Text size="xs" c="dimmed">关闭或切换设置会保留草稿，只有保存后才会影响 Bot。共同记忆不会自动收集聊天内容。</Text>
  </Stack>;
}
