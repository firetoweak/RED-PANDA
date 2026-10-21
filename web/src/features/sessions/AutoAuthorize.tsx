import { Stack, Switch, Text, Tooltip } from "@mantine/core";
import { useGetAutoAuthorizeQuery, useSetAutoAuthorizeMutation } from "../../api/redpandaApi";
import { useAppSelector } from "../../app/hooks";

export function AutoAuthorize() {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const { data: policy } = useGetAutoAuthorizeQuery();
  const [setAutoAuthorize, { isLoading, error }] = useSetAutoAuthorizeMutation();
  return <Stack gap={3} className="sidebar-auto-authorize">
    <Tooltip label="自动放行所有会话的写文件等工具">
      <Switch aria-label="自动放行写文件等工具" label="自动放行" size="xs"
        checked={policy?.enabled ?? false}
        disabled={connectionId === null || policy === undefined || isLoading}
        onChange={(event) => void setAutoAuthorize({ connectionId: connectionId!, enabled: event.currentTarget.checked })} />
    </Tooltip>
    {error ? <Text c="red" fz={11} role="alert">自动放行设置失败</Text> : null}
  </Stack>;
}
