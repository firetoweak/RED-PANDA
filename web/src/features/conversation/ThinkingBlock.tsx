import { Collapse, Loader, Paper, Tooltip, UnstyledButton } from "@mantine/core";
import { IconBrain } from "@tabler/icons-react";
import { memo, useState } from "react";

export const ThinkingBlock = memo(function ThinkingBlock({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  const [opened, setOpened] = useState(false);

  return (
    <Paper className={`thinking-block${opened ? " is-open" : ""}`} radius="md" withBorder>
      <Tooltip label={streaming ? "思考中" : opened ? "收起思考" : "查看思考"} events={{ hover: true, focus: true, touch: false }}>
        <UnstyledButton
          aria-label="思考"
          aria-expanded={opened}
          className="thinking-toggle"
          onClick={() => setOpened((value) => !value)}
        >
          {streaming ? <Loader color="ember" size={14} /> : <IconBrain aria-hidden="true" size={16} />}
        </UnstyledButton>
      </Tooltip>
      <Collapse expanded={opened}>
        {opened ? <div className="thinking-content">{text}</div> : null}
      </Collapse>
    </Paper>
  );
});
