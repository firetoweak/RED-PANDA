import { Stack, Text, Title } from "@mantine/core";
import type { PropsWithChildren } from "react";

import { redPandaFull } from "./brand";

export function ConversationWelcome({ children }: PropsWithChildren) {
  return (
    <div className="conversation-intro">
      <Stack className="conversation-welcome" align="center" gap="sm" ta="center">
        <img className="welcome-mascot" src={redPandaFull} alt="折纸小熊猫" />
        <Text className="welcome-brand" fw={700} fz={11}>RED PANDA</Text>
        <Title order={1} className="welcome-title" fw={600}>
          今天想一起完成什么？
        </Title>
        <Text c="dimmed" size="sm" className="welcome-description">
          一起推敲问题，也把想法变成实际成果。
        </Text>
        {children}
      </Stack>
    </div>
  );
}
