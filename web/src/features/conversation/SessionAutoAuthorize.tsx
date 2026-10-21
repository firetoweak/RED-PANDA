import { Stack, Switch, Text, Tooltip } from "@mantine/core";
import { useGetConversationQuery, useSetAutoAuthorizeMutation } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";

export function SessionAutoAuthorize({ sessionId }: { sessionId: string }) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { currentData: conversation } = useGetConversationQuery(sessionId);
  const [setAutoAuthorize, { isLoading, error }] = useSetAutoAuthorizeMutation();
  return <Stack gap={3} className="sidebar-auto-authorize">
    <Tooltip label="仅自动放行当前 Bot 的写文件等工具">
      <Switch aria-label="自动放行写文件等工具" label="自动放行" size="xs"
        checked={conversation?.session.auto_authorize ?? false}
        disabled={connectionId === null || conversation === undefined || isLoading}
        onChange={(event) => void setAutoAuthorize({ sessionId, connectionId: connectionId!, enabled: event.currentTarget.checked })} />
    </Tooltip>
    {error ? <Text c="red" fz={11} role="alert">自动放行设置失败</Text> : null}
  </Stack>;
}
