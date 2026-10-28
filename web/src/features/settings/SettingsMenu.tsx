import { ActionIcon, Box, Drawer, Menu } from "@mantine/core";
import { IconAdjustments, IconBrain, IconPlugConnected, IconSettings } from "@tabler/icons-react";
import { useState } from "react";
import { ModelSettings } from "../models/ModelSettings";
import { ExternalAccess } from "./ExternalAccess";
import { SharedMemoryPanel } from "./SharedMemoryPanel";

export function SettingsMenu({ onOpen }: { onOpen: () => void }) {
  const [section, setSection] = useState<"models" | "memory" | "external">("models");
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
      <Menu.Target><ActionIcon aria-label="设置" title="设置" className="settings-trigger" variant="subtle" color="gray" size={32}><IconSettings size={18} /></ActionIcon></Menu.Target>
      <Menu.Dropdown>
        <Menu.Item leftSection={<IconAdjustments size={16} />} onClick={() => open("models")}>模型配置</Menu.Item>
        <Menu.Item leftSection={<IconBrain size={16} />} onClick={() => open("memory")}>共同记忆</Menu.Item>
        <Menu.Item leftSection={<IconPlugConnected size={16} />} onClick={() => open("external")}>Codex 接入</Menu.Item>
      </Menu.Dropdown>
    </Menu>
    <Drawer opened={opened} onClose={() => setOpened(false)} position="right" keepMounted
      title={section === "models" ? "模型配置" : section === "memory" ? "共同记忆" : "Codex 接入"}
      size={section === "models" ? "min(900px, 100vw)" : "min(560px, 100vw)"}
      padding="lg" className="settings-drawer" closeButtonProps={{ "aria-label": "关闭设置" }}>
      {visited.has("models") ? <Box hidden={section !== "models"}><ModelSettings active={opened && section === "models"} /></Box> : null}
      {visited.has("memory") ? <Box hidden={section !== "memory"}><SharedMemoryPanel active={opened && section === "memory"} /></Box> : null}
      {opened && section === "external" ? <ExternalAccess /> : null}
    </Drawer>
  </>;
}
