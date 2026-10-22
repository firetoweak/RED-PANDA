import { Box, Button, Drawer, Menu } from "@mantine/core";
import { IconAdjustments, IconBrain, IconSettings } from "@tabler/icons-react";
import { useState } from "react";
import { ModelSettings } from "../models/ModelSettings";
import { SharedMemoryPanel } from "./SharedMemoryPanel";

export function SettingsMenu({ onOpen }: { onOpen: () => void }) {
  const [section, setSection] = useState<"models" | "memory">("models");
  const [opened, setOpened] = useState(false);
  const [visited, setVisited] = useState<ReadonlySet<typeof section>>(new Set());
  function open(next: typeof section) {
    setSection(next);
    setOpened(true);
    setVisited((current) => new Set(current).add(next));
    onOpen();
  }
  return <>
    <Menu position="top-start" withinPortal>
      <Menu.Target><Button variant="subtle" leftSection={<IconSettings size={17} />}>设置</Button></Menu.Target>
      <Menu.Dropdown>
        <Menu.Item leftSection={<IconAdjustments size={16} />} onClick={() => open("models")}>模型配置</Menu.Item>
        <Menu.Item leftSection={<IconBrain size={16} />} onClick={() => open("memory")}>共同记忆</Menu.Item>
      </Menu.Dropdown>
    </Menu>
    <Drawer opened={opened} onClose={() => setOpened(false)} position="right" keepMounted
      title={section === "models" ? "模型配置" : "共同记忆"}
      size={section === "models" ? "min(900px, 100vw)" : "min(560px, 100vw)"}
      padding="lg" className="settings-drawer" closeButtonProps={{ "aria-label": "关闭设置" }}>
      {visited.has("models") ? <Box hidden={section !== "models"}><ModelSettings active={opened && section === "models"} /></Box> : null}
      {visited.has("memory") ? <Box hidden={section !== "memory"}><SharedMemoryPanel active={opened && section === "memory"} /></Box> : null}
    </Drawer>
  </>;
}
