import { Button, Group, Menu, Popover, SegmentedControl, Text, TextInput } from "@mantine/core";
import { IconAdjustments, IconBrain, IconCheck, IconChevronDown, IconSearch } from "@tabler/icons-react";
import { useState } from "react";
import { PROVIDERS, effortName, providerName, type CatalogModel, type Preferences } from "./api";

export type Selection = { model: string; effort: string | null };

export function ModelPicker({ models, preferences, selection, onSelect, onOpenSettings, pending }: {
  models: CatalogModel[];
  preferences: Preferences;
  selection: Selection;
  onSelect(value: Selection): void;
  onOpenSettings(): void;
  pending: boolean;
}) {
  const [opened, setOpened] = useState(false);
  const [scope, setScope] = useState("favorites");
  const [query, setQuery] = useState("");
  const selected = models.find((item) => item.id === selection.model)!;
  const visible = models.filter((item) => (scope === "all" || preferences.favorites[item.id])
    && `${item.name} ${item.id} ${providerName(item.provider)}`.toLowerCase().includes(query.trim().toLowerCase()));

  function openModel(value: boolean) { setOpened(value); if (value) { setQuery(""); setScope("favorites"); } }
  function select(item: CatalogModel) {
    onSelect({ model: item.id, effort: preferences.favorites[item.id]?.effort ?? null });
    setOpened(false);
  }

  return <div className="composer-model-controls">
    <Popover opened={opened} onChange={openModel} position="top-start" withinPortal shadow="md" trapFocus>
      <Popover.Target><Button variant="subtle" color="gray" size="compact-sm" radius="sm"
        className="model-picker-trigger" aria-label={`切换模型：${providerName(selected.provider)} ${selected.name}`}
        onClick={() => openModel(!opened)} rightSection={<IconChevronDown size={13} />}>
        <span className="current-model-name">{selected.name}</span><span className="current-model-provider">{providerName(selected.provider)}</span>
      </Button></Popover.Target>
      <Popover.Dropdown className="model-picker-dropdown" p={0} onKeyDown={(event) => {
        const onOption = (event.target as HTMLElement).getAttribute("role") === "option";
        if (!["ArrowDown", "ArrowUp"].includes(event.key) && !(onOption && ["Home", "End"].includes(event.key))) return;
        const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'));
        if (!options.length) return;
        event.preventDefault();
        const index = options.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
          : event.key === "ArrowUp" ? (index <= 0 ? options.length - 1 : index - 1) : (index + 1) % options.length;
        options[next].focus();
      }}>
        <div className="model-picker-header"><Text size="sm" fw={600}>选择模型</Text>
          <SegmentedControl size="xs" value={scope} onChange={setScope} data={[{ value: "favorites", label: "常用" }, { value: "all", label: "全部" }]} fullWidth />
          <TextInput aria-label="搜索可选模型" placeholder="搜索模型或供应商" value={query} size="sm"
            onChange={(event) => setQuery(event.currentTarget.value)} leftSection={<IconSearch size={15} />} data-autofocus />
        </div>
        <div className="model-picker-options" role="listbox" aria-label="可选模型">
          {PROVIDERS.map((provider) => {
            const group = visible.filter((item) => item.provider === provider.value);
            return group.length ? <div key={provider.value} role="group" aria-label={provider.label}>
              <Text className="model-provider-heading" size="xs" c="dimmed">{provider.label}<span>{provider.caption}</span></Text>
              {group.map((item) => <button type="button" role="option" aria-selected={item.id === selected.id}
                aria-label={`${provider.label} ${item.name}`} key={item.id} onClick={() => select(item)} className="model-picker-option">
                <span>{item.name}<small>{item.description}</small></span>{item.id === selected.id ? <IconCheck size={16} /> : null}
              </button>)}
            </div> : null;
          })}
          {visible.length === 0 ? <Text size="sm" c="dimmed" ta="center" p="lg">没有匹配的{scope === "favorites" ? "常用" : ""}模型</Text> : null}
        </div>
        <div className="model-picker-footer">
          <Button variant="subtle" color="gray" fullWidth justify="flex-start" size="sm" leftSection={<IconAdjustments size={15} />}
            onClick={() => { setOpened(false); onOpenSettings(); }}>管理常用模型</Button>
          {scope === "all" ? <Text size="xs" c="dimmed">临时选用不会自动加入常用列表。</Text> : null}
        </div>
      </Popover.Dropdown>
    </Popover>
    {selected.efforts.length ? <Menu position="top-start" withinPortal shadow="md">
      <Menu.Target><Button variant="subtle" color="gray" size="compact-sm" radius="sm" className="effort-picker-trigger"
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
    {!preferences.favorites[selected.id] ? <Text size="xs" c="dimmed" className="temporary-model-label">临时使用</Text> : null}
    {pending ? <Group gap={4} className="selection-pending"><span className="pending-dot" /><Text size="xs" c="dimmed">下次回复生效</Text></Group> : null}
  </div>;
}
