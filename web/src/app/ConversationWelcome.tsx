import { Stack, Text, Title } from "@mantine/core";
import type { PropsWithChildren } from "react";

import { kondenMark } from "./brand";

export function ConversationWelcome({ children }: PropsWithChildren) {
  return (
    <div className="conversation-intro">
      <Stack className="conversation-welcome" align="center" gap="sm" ta="center">
        <img className="welcome-mark" src={kondenMark} alt="konden 标志" />
        <Text className="welcome-brand" fw={500} fz={11}>konden</Text>
        <Title order={1} className="welcome-title" fw={600}>
          今天想一起完成什么？
        </Title>
        <Text c="dimmed" size="sm" className="welcome-description">
          描述目标、贴一段代码，或把需要处理的文件拖进来。
        </Text>
        {children}
      </Stack>
    </div>
  );
}
