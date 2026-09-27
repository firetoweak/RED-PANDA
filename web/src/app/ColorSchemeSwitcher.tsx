import { ActionIcon, Menu, useMantineColorScheme } from "@mantine/core";
import { IconCheck, IconMoon, IconSun } from "@tabler/icons-react";
import { useEffect } from "react";

export function ColorSchemeSwitcher() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  useEffect(() => {
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor === null) {
      return;
    }
    themeColor.setAttribute(
      "content",
      colorScheme === "light" ? "#f5f6f2" : "#111212",
    );
  }, [colorScheme]);
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon
          aria-label="外观"
          className="color-scheme-trigger"
          radius="xl"
          size="lg"
          variant="default"
        >
          {colorScheme === "light" ? <IconSun size={18} /> : <IconMoon size={18} />}
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>外观</Menu.Label>
        <Menu.Item
          leftSection={<IconMoon size={16} />}
          onClick={() => setColorScheme("dark")}
          rightSection={colorScheme !== "light" ? <IconCheck size={14} /> : undefined}
        >
          深色
        </Menu.Item>
        <Menu.Item
          leftSection={<IconSun size={16} />}
          onClick={() => setColorScheme("light")}
          rightSection={colorScheme === "light" ? <IconCheck size={14} /> : undefined}
        >
          亮色
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
