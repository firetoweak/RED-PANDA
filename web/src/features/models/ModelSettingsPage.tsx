import { Alert, Badge, Button, Container, Group, Modal, NumberInput, Paper, Radio, Select, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import type { ModelConfig, ModelProfile } from "../../api/contracts";
import { useGetModelSettingsQuery, useSaveModelSettingsMutation } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";
import { ModelTestButton } from "./ModelTestButton";

export function ModelSettingsPage() {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data, error, isLoading } = useGetModelSettingsQuery(undefined, { pollingInterval: 5000 });
  const [save, { isLoading: saving, error: saveError }] = useSaveModelSettingsMutation();
  const [draft, setDraft] = useState<ModelConfig | null>(null);
  const saved = useRef<ModelConfig | null>(null);
  const [editing, setEditing] = useState<ModelProfile | "new" | null>(null);
  const [provider, setProvider] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [threshold, setThreshold] = useState<string | number>(200000);
  const [reasoningEffort, setReasoningEffort] = useState<ModelProfile["reasoning_effort"]>();
  const [notice, setNotice] = useState("");
  const [formError, setFormError] = useState("");

  useEffect(() => {
    if (data === undefined) return;
    const previous = saved.current;
    setDraft((current) => current === null || JSON.stringify(current) === JSON.stringify(previous) ? data.config : current);
    saved.current = data.config;
  }, [data]);

  function edit(profile: ModelProfile | "new") {
    setEditing(profile);
    setProvider(profile === "new" ? data?.providers[0]?.provider ?? null : profile.model.split("/")[0]);
    setName(profile === "new" ? "" : profile.model.slice(profile.model.indexOf("/") + 1));
    setThreshold(profile === "new" ? 200000 : profile.compact_threshold_tokens);
    setReasoningEffort(profile === "new" ? undefined : profile.reasoning_effort);
    setFormError("");
  }

  function applyEdit() {
    if (draft === null || provider === null) return;
    const tokens = Number(threshold);
    if (!name.trim() || !Number.isSafeInteger(tokens) || tokens <= 0) {
      setFormError("请输入模型名称和正整数 compact 触发值。"); return;
    }
    const profile: ModelProfile = { model: provider + "/" + name.trim(), compact_threshold_tokens: tokens };
    if (profile.model === "stepfun/step-5-preview" && reasoningEffort !== undefined) {
      profile.reasoning_effort = reasoningEffort;
    }
    const oldName = editing === "new" || editing === null ? null : editing.model;
    if (draft.model.candidates.some((item) => item.model === profile.model && item.model !== oldName)) {
      setFormError("候选列表已经包含这个模型。"); return;
    }
    const candidates = oldName === null ? [...draft.model.candidates, profile]
      : draft.model.candidates.map((item) => item.model === oldName ? profile : item);
    const defaultModel = draft.model.default === oldName || draft.model.default === null
      ? profile.model : draft.model.default;
    setDraft({ model: { default: defaultModel, candidates } });
    setNotice(""); setEditing(null);
  }

  async function submit() {
    if (draft === null || connectionId === null) return;
    try {
      const updated = await save({ connectionId, config: draft }).unwrap();
      saved.current = updated.config;
      setDraft(updated.config); setNotice("已保存。默认模型用于新会话；模型参数在下一次决策生效。");
    } catch { setNotice(""); } // 具体失败由 mutation 状态展示。
  }

  if (isLoading || draft === null) return <Container py={80}><Text>{error ? "读取模型配置失败：" + JSON.stringify(error) : "读取模型配置…"}</Text></Container>;
  const changed = data !== undefined && JSON.stringify(draft) !== JSON.stringify(data.config);

  return <Container size="md" py={70} style={{ height: "100%", overflowY: "auto" }}>
    <Stack gap="lg">
      {error ? <Alert color="red" title="读取配置失败">{JSON.stringify(error)}</Alert> : null}
      <Group justify="space-between"><div><Title order={2}>模型配置</Title><Text c="dimmed" mt={6} size="sm">维护可选模型，在每个会话的输入区切换。</Text></div>
        <Button onClick={() => edit("new")}>添加模型</Button></Group>
      <Paper withBorder p="md" radius="md">
        <Text fw={600} mb="xs">候选模型</Text>
        {draft.model.candidates.length === 0 ? <Text c="dimmed">先添加一个模型，再开始会话。</Text> :
        <Table.ScrollContainer minWidth={760}><Table verticalSpacing="sm">
          <Table.Thead><Table.Tr><Table.Th>新会话默认</Table.Th><Table.Th>模型</Table.Th><Table.Th>推理程度</Table.Th><Table.Th>compact 触发值</Table.Th><Table.Th>连接</Table.Th><Table.Th>操作</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>{draft.model.candidates.map((profile) => {
            const state = data?.providers.find((item) => item.provider === profile.model.split("/")[0]);
            return <Table.Tr key={profile.model}>
              <Table.Td><Radio aria-label={"默认模型 " + profile.model} checked={draft.model.default === profile.model}
                onChange={() => { setDraft({ model: { ...draft.model, default: profile.model } }); setNotice(""); }} /></Table.Td>
              <Table.Td><Text size="sm" ff="monospace">{profile.model}</Text></Table.Td>
              <Table.Td>{profile.reasoning_effort === undefined
                ? (profile.model === "stepfun/step-5-preview" ? "供应商默认" : "—")
                : { low: "低", medium: "中", high: "高" }[profile.reasoning_effort]}</Table.Td>
              <Table.Td>{profile.compact_threshold_tokens.toLocaleString()}</Table.Td>
              <Table.Td><Badge color={state?.configured ? "green" : "gray"} variant="light">{state?.configured ? "已配置" : "待配置"}</Badge></Table.Td>
              <Table.Td><Group gap={4} wrap="nowrap" align="flex-start">
                <ModelTestButton model={profile.model} connectionId={connectionId}
                  saved={data?.config.model.candidates.some((item) => item.model === profile.model) ?? false}
                  configured={state?.configured ?? false} />
                <Button size="xs" variant="subtle" onClick={() => edit(profile)}>编辑</Button>
                <Button size="xs" color="red" variant="subtle" disabled={profile.model === draft.model.default}
                  onClick={() => { setDraft({ model: { ...draft.model, candidates: draft.model.candidates.filter((item) => item.model !== profile.model) } }); setNotice(""); }}>删除</Button></Group></Table.Td>
            </Table.Tr>;
          })}</Table.Tbody>
        </Table></Table.ScrollContainer>}
        <Text c="dimmed" size="xs" mt="sm">删除默认模型前，先保存新的默认模型。仍被会话选用的模型需先在这些会话中切换。</Text>
        <Text c="dimmed" size="xs" mt={4}>测试会向该模型发送一条简短请求。新添加或改名的模型请先保存。</Text>
      </Paper>
      {saveError ? <Alert color="red" title="保存失败">{typeof saveError === "string" ? saveError : JSON.stringify(saveError)}</Alert> : null}
      {notice ? <Alert color="green">{notice}</Alert> : null}
      <Group><Button disabled={connectionId === null || !changed} loading={saving} onClick={() => void submit()}>保存配置</Button>
        <Button variant="subtle" disabled={!changed || saving} onClick={() => { if (data) setDraft(data.config); setNotice(""); }}>撤销修改</Button></Group>
      <Paper withBorder p="md" radius="md">
        <Text fw={600}>供应商连接</Text>
        <Text size="sm" c="dimmed" mt={6}>在下方个人连接文件中填写密钥或本地服务地址。保存后，下一次调用直接使用新连接；正在进行的请求继续完成。</Text>
        <Text size="sm" ff="monospace" mt="xs" style={{ overflowWrap: "anywhere" }}>{data?.connections_path}</Text>
        <Group mt="md">{data?.providers.map((item) => <Badge key={item.provider} color={item.configured ? "green" : "gray"} variant="light">
          {item.provider} · {item.configured ? "已配置" : "缺少 " + item.missing}
        </Badge>)}</Group>
      </Paper>
    </Stack>
    <Modal opened={editing !== null} onClose={() => setEditing(null)} title={editing === "new" ? "添加模型" : "编辑模型"} centered>
      <Stack>
        <Select label="供应商" data={data?.providers.map((item) => ({ value: item.provider, label: item.provider })) ?? []} value={provider} onChange={setProvider} allowDeselect={false} />
        <TextInput label="模型名称" description="填写服务接受的模型 ID；同一供应商可以添加多个模型。" value={name} onChange={(event) => setName(event.currentTarget.value)} />
        {provider === "stepfun" && name.trim() === "step-5-preview" ? <Select
          label="推理程度" description="更高的程度允许更深入的推理，响应可能更慢。供应商默认不指定程度。"
          data={[{ value: "default", label: "供应商默认" }, { value: "low", label: "低" }, { value: "medium", label: "中" }, { value: "high", label: "高" }]}
          value={reasoningEffort ?? "default"} allowDeselect={false}
          onChange={(value) => setReasoningEffort(value === "low" || value === "medium" || value === "high" ? value : undefined)}
        /> : null}
        <NumberInput label="compact 触发值（输入 tokens）" description="按模型的上下文容量设置，达到阈值时整理上下文。" value={threshold} onChange={setThreshold} min={1} allowDecimal={false} allowNegative={false} />
        {formError ? <Text size="sm" c="red">{formError}</Text> : null}
        <Button onClick={applyEdit}>加入待保存配置</Button>
      </Stack>
    </Modal>
  </Container>;
}
