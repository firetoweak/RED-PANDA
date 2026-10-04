import { Collapse, Group, Loader, Paper, Text, UnstyledButton } from "@mantine/core";
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { memo, useEffect, useState } from "react";

export const ThinkingBlock = memo(function ThinkingBlock({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  const [opened, setOpened] = useState(streaming);

  useEffect(() => {
    setOpened(streaming);
  }, [streaming]);

  return (
    <Paper className="thinking-block" radius="md" withBorder>
      <UnstyledButton
        aria-expanded={opened}
        className="thinking-toggle"
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
            {streaming ? <Loader color="ember" size={12} /> : null}
            <Text c="dimmed" fw={600} size="sm">
              思考
            </Text>
          </Group>
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        {opened ? <div className="thinking-content">{text}</div> : null}
      </Collapse>
    </Paper>
  );
});
