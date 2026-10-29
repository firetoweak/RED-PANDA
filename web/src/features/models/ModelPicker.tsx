import { Button, Group, Menu, Text } from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconCheck, IconChevronDown } from "@tabler/icons-react";
import { useState } from "react";
import { effortName, providerName, type CatalogModel, type Preferences } from "./modelUi";

export type Selection = { model: string; effort: string | null };

export function ModelPicker({ models, preferences, selection, onSelect, pending, disabled = false, disabledProviders = [] }: {
  models: CatalogModel[];
  preferences: Preferences;
  selection: Selection;
  onSelect(value: Selection): void;
  pending: boolean;
  disabled?: boolean;
  disabledProviders?: string[];
}) {
  const [modelOpened, setModelOpened] = useState(false);
  const [effortOpened, setEffortOpened] = useState(false);
  const submenuPosition = useMediaQuery("(max-width: 48em)") ? "top-end" : "right-start";
  const selected = models.find((item) => item.id === selection.model)!;
  const visible = models.filter((item) => preferences.favorites[item.id]);
  const effort = effortName(selected, selection.effort);

  return <div className="composer-model-controls">
    <Menu position="top-end" width={240} withinPortal shadow="md" onClose={() => { setModelOpened(false); setEffortOpened(false); }}>
      <Menu.Target><Button variant="subtle" color="gray" size="compact-sm" radius="sm"
        disabled={disabled}
        className="model-picker-trigger" aria-label={`模型设置：${providerName(selected.provider)} ${selected.name}${selected.efforts.length ? `，推理强度：${effort}` : ""}`}
        rightSection={<IconChevronDown size={13} />}>
        <span className="current-model-name">{selected.name}</span>
        {selected.efforts.length ? <span className="current-model-effort">· {effort}</span> : null}
      </Button></Menu.Target>
      <Menu.Dropdown className="model-picker-dropdown" p={6}>
        {selected.efforts.length ? <Menu.Sub width={240} position={submenuPosition} opened={effortOpened} onChange={setEffortOpened}>
          <Menu.Sub.Target><Menu.Sub.Item className="model-picker-option" aria-label={`推理强度：${effort}`}
            onClick={() => setEffortOpened(true)}>
            <span className="model-setting-row"><span>推理强度</span><span className="model-setting-value">{effort}</span></span>
          </Menu.Sub.Item></Menu.Sub.Target>
          <Menu.Sub.Dropdown className="model-picker-dropdown model-picker-options" p={6}>
            <Menu.Item className="model-picker-option" rightSection={selection.effort === null ? <IconCheck size={14} /> : null}
              onClick={() => onSelect({ ...selection, effort: null })}>供应商默认（{effortName(selected, null)}）</Menu.Item>
            <Menu.Divider />
            {selected.efforts.map((choice) => <Menu.Item key={choice.value} className="model-picker-option"
              rightSection={selection.effort === choice.value ? <IconCheck size={14} /> : null}
              onClick={() => onSelect({ ...selection, effort: choice.value })}>{choice.label}</Menu.Item>)}
          </Menu.Sub.Dropdown>
        </Menu.Sub> : null}
        <Menu.Sub width={240} position={submenuPosition} opened={modelOpened} onChange={setModelOpened}>
          <Menu.Sub.Target><Menu.Sub.Item className="model-picker-option" aria-label={`切换模型：${providerName(selected.provider)} ${selected.name}`}
            onClick={() => setModelOpened(true)}>
            <span className="model-setting-row"><span>模型</span><span className="model-setting-value">{selected.name}</span></span>
          </Menu.Sub.Item></Menu.Sub.Target>
          <Menu.Sub.Dropdown className="model-picker-dropdown model-picker-options" p={6}>
            {visible.map((item) => <Menu.Item key={item.id} aria-current={item.id === selected.id ? true : undefined}
              disabled={disabled || disabledProviders.includes(item.provider)}
              aria-label={`${providerName(item.provider)} ${item.name}`} className="model-picker-option"
              rightSection={item.id === selected.id ? <IconCheck size={15} /> : null}
              onClick={() => onSelect({ model: item.id, effort: preferences.favorites[item.id].effort })}>
              {item.name}
            </Menu.Item>)}
          </Menu.Sub.Dropdown>
        </Menu.Sub>
        {pending ? <><Menu.Divider /><Group gap={5} className="selection-pending"><span className="pending-dot" /><Text size="xs" c="dimmed">下次回复生效</Text></Group></> : null}
      </Menu.Dropdown>
    </Menu>
  </div>;
}
