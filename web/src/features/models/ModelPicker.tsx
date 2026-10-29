import { Button, Group, Menu, Text } from "@mantine/core";
import { IconBrain, IconCheck, IconChevronDown } from "@tabler/icons-react";
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
  const selected = models.find((item) => item.id === selection.model)!;
  const visible = models.filter((item) => preferences.favorites[item.id]);

  return <div className="composer-model-controls">
    <Menu position="top-start" withinPortal shadow="md">
      <Menu.Target><Button variant="subtle" color="gray" size="compact-sm" radius="sm"
        disabled={disabled}
        className="model-picker-trigger" aria-label={`切换模型：${providerName(selected.provider)} ${selected.name}`}
        rightSection={<IconChevronDown size={13} />}>
        <span className="current-model-name">{selected.name}</span><span className="current-model-provider">{providerName(selected.provider)}</span>
      </Button></Menu.Target>
      <Menu.Dropdown className="model-picker-dropdown" p={6}>
        {visible.map((item) => <Menu.Item key={item.id} aria-current={item.id === selected.id ? true : undefined}
          disabled={disabled || disabledProviders.includes(item.provider)}
          aria-label={`${providerName(item.provider)} ${item.name}`} className="model-picker-option"
          rightSection={item.id === selected.id ? <IconCheck size={15} /> : null}
          onClick={() => onSelect({ model: item.id, effort: preferences.favorites[item.id].effort })}>
          {item.name}
        </Menu.Item>)}
      </Menu.Dropdown>
    </Menu>
    {selected.efforts.length ? <Menu position="top-start" withinPortal shadow="md">
      <Menu.Target><Button variant="subtle" color="gray" size="compact-sm" radius="sm" className="effort-picker-trigger"
        disabled={disabled}
        aria-label={`推理强度：${effortName(selected, selection.effort)}`} leftSection={<IconBrain size={14} />}
        rightSection={<IconChevronDown size={12} />}>推理：{effortName(selected, selection.effort)}</Button></Menu.Target>
      <Menu.Dropdown miw={205}>
        <Menu.Label>当前会话的推理强度</Menu.Label>
        <Menu.Item rightSection={selection.effort === null ? <IconCheck size={14} /> : null}
          onClick={() => onSelect({ ...selection, effort: null })}>供应商默认（{effortName(selected, null)}）</Menu.Item>
        <Menu.Divider />
        {selected.efforts.map((choice) => <Menu.Item key={choice.value}
          rightSection={selection.effort === choice.value ? <IconCheck size={14} /> : null}
          onClick={() => onSelect({ ...selection, effort: choice.value })}>{choice.label}</Menu.Item>)}
      </Menu.Dropdown>
    </Menu> : null}
    {pending ? <Group gap={4} className="selection-pending"><span className="pending-dot" /><Text size="xs" c="dimmed">下次回复生效</Text></Group> : null}
  </div>;
}
