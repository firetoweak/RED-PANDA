import { Button, Text } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";

import { useGetWorkspacesQuery } from "../../api/redpandaApi";
import { ConversationWelcome } from "../../app/ConversationWelcome";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";

export function AgentHome() {
  const { data: workspaces = [], isLoading } = useGetWorkspacesQuery();
  if (!isLoading && workspaces.length === 0) {
    return <NoWorkspaceYet />;
  }
  return (
    <ConversationWelcome>
      <Text c="dimmed" maw={380} size="sm" mt="sm">
        从左侧选择一个 Agent，或在项目里新建一个。
      </Text>
    </ConversationWelcome>
  );
}

function NoWorkspaceYet() {
  const [opened, { open, close }] = useDisclosure(false);
  return (
    <ConversationWelcome>
      <Text c="dimmed" maw={380} size="sm" mt="sm">
        先选择一个文件夹作为工作区，小熊猫会在这里与你处理文件和任务。
      </Text>
      <Button onClick={open} variant="light">
        创建工作区
      </Button>
      <CreateWorkspaceModal onClose={close} opened={opened} />
    </ConversationWelcome>
  );
}
