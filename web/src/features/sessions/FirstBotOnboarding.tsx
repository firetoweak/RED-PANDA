import { Alert, Button, Group, Skeleton, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { useCreateAgentMutation, useGetModelSettingsQuery } from "../../api/kondenApi";
import { ModelSettings } from "../models/ModelSettings";

/** 名册为空时的起点：先确保 Bot 能开口，再给第一位伙伴起名，由它向用户介绍团队能做什么。 */
export function FirstBotOnboarding({ connectionId, onCreated }: { connectionId: string | null; onCreated: (sessionId: string) => void }) {
  const { data: settings, isLoading: settingsLoading } = useGetModelSettingsQuery();
  const [createAgent, { isLoading }] = useCreateAgentMutation();
  const [name, setName] = useState("小柯");
  const [error, setError] = useState<string | null>(null);
  const defaultModel = settings?.config.model.default ?? null;
  const modelReady = defaultModel !== null && settings !== undefined
    && settings.providers.some((item) => item.provider === defaultModel.split("/")[0] && item.configured);
  const canSubmit = connectionId !== null && modelReady && name.trim() !== "" && !isLoading;

  async function submit() {
    if (!canSubmit || connectionId === null) return;
    setError(null);
    try {
      const agent = await createAgent({ connectionId, path: null, name }).unwrap();
      onCreated(agent.session_id);
    } catch (cause) {
      setError(typeof cause === "string" ? cause : "创建 Bot 失败");
    }
  }

  return <div className="onboarding-panel">
    <Title order={2}>认识你的第一位伙伴</Title>
    <Text c="dimmed">它会介绍这支团队能做什么，并陪你迈出第一步：聊想法、分析资料、推敲方案，或关联代码项目一起开发。</Text>
    {settingsLoading ? <Skeleton h={60} /> : modelReady ? null : <div className="onboarding-step">
      <Text fw={600}>先配置一个默认模型</Text>
      <Text c="dimmed" size="sm">Bot 需要可用的默认模型才能开口。配置保存后即可继续。</Text>
      <ModelSettings />
    </div>}
    <Group align="flex-end" gap="sm">
      <TextInput label="给它起个名字" description="之后随时可以修改" value={name} disabled={isLoading || !modelReady}
        onChange={(event) => setName(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault();
            void submit();
          }
        }} />
      <Button loading={isLoading} disabled={!canSubmit} onClick={() => void submit()}>开始</Button>
    </Group>
    {error === null ? null : <Alert color="red" title="无法创建">{error}</Alert>}
  </div>;
}
