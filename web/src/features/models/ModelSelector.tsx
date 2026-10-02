import { Alert, Button, Menu, Stack } from "@mantine/core";
import { IconCheck, IconChevronDown } from "@tabler/icons-react";
import { useGetModelSettingsQuery, useGetSessionModelQuery, useSetSessionModelMutation } from "../../api/helpermeApi";

export function ModelSelector({ sessionId, connectionId }: { sessionId: string; connectionId: string | null }) {
  const { data: settings, error: settingsError } = useGetModelSettingsQuery(undefined, { pollingInterval: 5000 });
  const { data: selection, error } = useGetSessionModelQuery(sessionId, { pollingInterval: 2000 });
  const [select, { isLoading, error: selectError }] = useSetSessionModelMutation();
  const selected = selection?.selected?.model ?? null;
  async function change(model: string) {
    if (connectionId === null) return;
    try { await select({ sessionId, connectionId, model }).unwrap(); } catch { /* mutation 显示失败 */ }
  }
  return <Stack gap={3} maw={320}>
    <Menu trigger="click" position="top-end" withinPortal>
      <Menu.Target>
        <Button aria-label={"切换会话模型：" + (selected === null ? "选择模型" : modelName(selected))}
          size="compact-sm" radius="xl" variant="light" color="gray" fw={400}
          disabled={connectionId === null || isLoading} loading={isLoading}
          rightSection={<IconChevronDown size={13} />} style={{ maxWidth: 320 }}
          styles={{ label: { overflow: "hidden", textOverflow: "ellipsis" } }}>
          {selected === null ? "选择模型" : modelName(selected)}
        </Button>
      </Menu.Target>
      <Menu.Dropdown style={{ maxHeight: 260, overflowY: "auto" }}>
        {settings?.config.model.candidates.map((item) => {
          const status = settings.providers.find((provider) => provider.provider === item.model.split("/")[0]);
          return <Menu.Item key={item.model} disabled={!status?.configured || connectionId === null || isLoading}
            rightSection={item.model === selected ? <IconCheck size={14} /> : undefined}
            onClick={() => void change(item.model)}>
            {modelName(item.model)}
          </Menu.Item>;
        })}
      </Menu.Dropdown>
    </Menu>
    {selectError || error || settingsError ? <Alert color="red" p="xs">{typeof selectError === "string" ? selectError : JSON.stringify(selectError ?? error ?? settingsError)}</Alert> : null}
  </Stack>;
}

function modelName(model: string) {
  return model.slice(model.indexOf("/") + 1);
}
