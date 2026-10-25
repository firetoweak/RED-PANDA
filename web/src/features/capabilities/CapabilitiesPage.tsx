import { ActionIcon, Alert, Button, Skeleton, Stack, Switch, Tabs, Text, TextInput, Title, Tooltip } from "@mantine/core";
import { IconChevronDown, IconPackage, IconPlugConnected, IconRefresh, IconSearch, IconTerminal2, IconX } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { useGetCapabilitiesQuery, useSetCapabilityEnabledMutation } from "../../api/kondenApi";
import { useAppSelector } from "../../app/hooks";
import "./capabilities.css";

type Category = "skills" | "mcp" | "cli";
const labels = { skills: "Skill", mcp: "MCP", cli: "CLI" };
const checkLabels = { unknown: "未检测", available: "最近检测通过", unavailable: "最近检测失败" };
const originLabel = (origin: "builtin" | "installed") => origin === "builtin" ? "产品内置" : "系统共享";
const checkedAt = (value: string | null) => value === null ? "尚未检测" : new Date(value).toLocaleString("zh-CN");

function EnabledSwitch({ kind, id, name, enabled }: { kind: "skills" | "mcp"; id: string; name: string; enabled: boolean }) {
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const [setEnabled, { isLoading, error }] = useSetCapabilityEnabledMutation();
  return <div className="capability-toggle">
    <Tooltip label={enabled ? "已启用" : "已停用"}>
      <Switch checked={enabled} disabled={connectionId === null || isLoading}
        aria-label={`${enabled ? "停用" : "启用"} ${name}`} color="ember"
        onChange={(event) => {
          if (connectionId !== null) void setEnabled({ connectionId, kind, id, enabled: event.currentTarget.checked });
        }} />
    </Tooltip>
    {error === undefined ? null : <Text role="alert" size="xs" c="red">{typeof error === "string" ? error : "修改失败，请重试。"}</Text>}
  </div>;
}

function CapabilityRow({ name, description, origin, inactive = false, icon, control, fields }: {
  name: string; description: string; origin: string; inactive?: boolean; icon: ReactNode;
  control: ReactNode; fields: [string, string][];
}) {
  const [expanded, setExpanded] = useState(false);
  return <li className="capability-row" data-inactive={inactive || undefined}>
    <div className="capability-row-main">
      <button className="capability-summary" type="button" aria-label={`查看 ${name}`} aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}>
        <span className="capability-icon">{icon}</span>
        <span className="capability-copy"><span className="capability-name">{name}</span>
          <span className="capability-description">{description || "暂无说明"}</span></span>
        <span className="capability-origin">{origin}</span>
        <IconChevronDown className="capability-chevron" size={14} aria-hidden="true" />
      </button>
      <div className="capability-control">{control}</div>
    </div>
    {expanded ? <div className="capability-detail">
      <Text size="sm" className="capability-full-description">{description || "暂无说明"}</Text>
      <dl>{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    </div> : null}
  </li>;
}

export function CapabilitiesPage() {
  const [category, setCategory] = useState<Category>("skills");
  const [search, setSearch] = useState("");
  const { data, isLoading, isFetching, isError, refetch } = useGetCapabilitiesQuery(undefined, {
    refetchOnMountOrArgChange: true, pollingInterval: 15000, skipPollingIfUnfocused: true,
  });
  const query = search.trim().toLocaleLowerCase();
  const matches = (item: { name: string; description: string }) => `${item.name}\n${item.description}`.toLocaleLowerCase().includes(query);
  const count = data?.[category].filter(matches).length ?? 0;
  return <section className="capabilities-page">
    <header className="page-header"><Text fw={600} size="md">能力</Text>
      <Tooltip label="刷新能力清单"><ActionIcon variant="subtle" color="gray" aria-label="刷新能力清单"
        loading={isFetching} onClick={() => void refetch()}><IconRefresh size={18} /></ActionIcon></Tooltip>
    </header>
    <div className="capabilities-scroll"><div className="capabilities-content">
      <div className="capabilities-heading"><Title order={1}>能力</Title>
        <Text c="dimmed" size="sm">查看系统提供的 Skill、MCP 和已登记 CLI。</Text></div>
      <Tabs value={category} variant="pills" onChange={(value) => { setCategory(value as Category); setSearch(""); }}>
      <div className="capabilities-toolbar">
          <Tabs.List aria-label="能力分类">{(Object.keys(labels) as Category[]).map((key) =>
            <Tabs.Tab key={key} value={key}>{labels[key]}<span className="capability-count">{data?.[key].length ?? "—"}</span></Tabs.Tab>)}
          </Tabs.List>
        <TextInput aria-label={`搜索 ${labels[category]}`} placeholder={`搜索 ${labels[category]}`} value={search}
          onChange={(event) => setSearch(event.currentTarget.value)} leftSection={<IconSearch size={16} />}
          rightSection={search === "" ? null : <ActionIcon aria-label="清除能力搜索" size="sm" variant="subtle" color="gray" onClick={() => setSearch("")}><IconX size={14} /></ActionIcon>} />
      </div>
      <Tabs.Panel value={category}>
      {isError ? <Alert color="red" title="能力清单加载失败"><Text size="sm">请检查连接后重试。</Text>
        <Button size="compact-xs" variant="light" color="red" mt="xs" onClick={() => void refetch()}>重试</Button></Alert> : null}
      {isLoading ? <Stack gap="xl" aria-label="正在加载能力清单">{[0, 1, 2].map((key) => <Skeleton key={key} h={58} />)}</Stack>
        : data === undefined ? null : <>
          {count === 0 ? <div className="capabilities-empty" role="status"><Text fw={500}>{query === "" ? `还没有已${category === "cli" ? "登记" : "安装"}的 ${labels[category]}` : "没有找到匹配的能力"}</Text>
            <Text size="sm" c="dimmed">{query !== "" ? "换个关键词，或清除搜索查看全部。" : "可以在与 Bot 的对话中安装或登记，完成后在这里查看。"}</Text></div> : null}
          <ul className="capabilities-list" aria-label={`${labels[category]} 清单`}>
            {category === "skills" ? data.skills.filter(matches).map((item) => <CapabilityRow key={item.id} name={item.name}
              description={item.description} origin={originLabel(item.origin)} inactive={!item.enabled} icon={<IconPackage size={21} />}
              control={item.origin === "builtin" ? null : <EnabledSwitch kind="skills" {...item} />}
              fields={[["来源", item.source], ["版本", item.version ?? "随产品更新"], ["安装位置", item.path], ["状态", item.enabled ? "已启用" : "已停用"]]} />) : null}
            {category === "mcp" ? data.mcp.filter(matches).map((item) => <CapabilityRow key={item.id} name={item.name}
              description={item.description} origin={originLabel(item.origin)} inactive={!item.enabled} icon={<IconPlugConnected size={21} />}
              control={<><Text className="capability-check" size="xs" c={item.last_status === "unavailable" ? "red" : "dimmed"}>{checkLabels[item.last_status]}</Text>
                {item.origin === "builtin" ? null : <EnabledSwitch kind="mcp" {...item} />}</>}
              fields={[["连接方式", item.transport === "stdio" ? "本地进程（stdio）" : "HTTP"], ["只读声明", item.read_only ? "只读" : "未声明只读"],
                ["启用状态", item.enabled ? "已启用" : "已停用"], ["最近检测", checkLabels[item.last_status]], ["检测时间", checkedAt(item.last_checked_at)],
                ...(item.last_error_summary === null ? [] : [["错误原因", item.last_error_summary] as [string, string]])]} />) : null}
            {category === "cli" ? data.cli.filter(matches).map((item) => <CapabilityRow key={item.id} name={item.name}
              description={item.description} origin="系统共享" icon={<IconTerminal2 size={21} />}
              control={<Text size="xs" c="dimmed">{item.version ?? "版本未记录"}</Text>}
              fields={[["来源", item.source], ["版本", item.version ?? "未记录"], ["可执行文件", item.path ?? "未记录"],
                ["最近体检", item.health === null ? "未检测" : item.health.help_ok && item.health.version_ok ? "通过" : "存在异常"],
                ["体检时间", checkedAt(item.health?.checked_at ?? null)]]} />) : null}
          </ul>
          <Text className="capabilities-scope" size="xs" c="dimmed">{category === "cli" ? "显示 konden 已登记的 CLI，版本与体检信息来自登记时的记录。" : category === "mcp" ? "系统共享项的启停对所有 Bot 生效；检测结果仅表示最近一次检测时的状态。" : "系统共享项的启停对所有 Bot 生效；工作目录中的项目 Skill 在各自目录中维护。"}</Text>
        </>}
      </Tabs.Panel>
      </Tabs>
    </div></div>
  </section>;
}
