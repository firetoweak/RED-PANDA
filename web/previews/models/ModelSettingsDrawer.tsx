import {
  ActionIcon, Alert, Badge, Button, Checkbox, Drawer, Group,
  Select, Skeleton, Stack, Tabs, Text, TextInput, Tooltip,
} from "@mantine/core";
import { IconCheck, IconRefresh, IconSearch, IconX } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { PROVIDERS, providerName, type CatalogModel, type ModelPreviewApi, type Preferences } from "./api";
import { SelectedModelsPanel } from "./SelectedModelsPanel";
import { SearchSettingsPanel } from "./SearchSettingsPanel";

export function ModelSettingsDrawer({ opened, onClose, preferences, allModels, onSaved, api }: {
  opened: boolean;
  onClose(): void;
  preferences: Preferences;
  allModels: CatalogModel[];
  onSaved(value: Preferences): void;
  api: ModelPreviewApi;
}) {
  const [draft, setDraft] = useState(() => structuredClone(preferences));
  const [page, setPage] = useState<string | null>("providers");
  const [provider, setProvider] = useState("openai");
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  useEffect(() => { setDraft(structuredClone(preferences)); }, [preferences]);
  useEffect(() => {
    if (!opened) return;
    let current = true;
    setLoading(true); setError(null); setCatalog([]);
    api.loadModels(provider).then((models) => {
      if (current) setCatalog(models);
    }, (cause: unknown) => {
      if (current) setError(cause instanceof Error ? cause.message : String(cause));
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [opened, provider, refresh, api]);

  const changed = JSON.stringify(draft) !== JSON.stringify(preferences);
  const hasInvalidThreshold = Object.values(draft.favorites).some((favorite) =>
    !Number.isSafeInteger(favorite.compactTokens) || favorite.compactTokens <= 0);
  const visible = catalog.filter((item) => `${item.name} ${item.id}`.toLowerCase().includes(query.trim().toLowerCase()));
  const selectedCount = Object.keys(draft.favorites).length;
  const selectedModels = allModels.filter((item) => draft.favorites[item.id]);

  function toggle(item: CatalogModel, checked: boolean) {
    setSavedNotice(false);
    setDraft((previous) => {
      const favorites = { ...previous.favorites };
      if (checked) favorites[item.id] = { effort: null, compactTokens: 50_000 };
      else delete favorites[item.id];
      return { ...previous, favorites };
    });
  }
  function updateFavorite(id: string, values: Partial<Preferences["favorites"][string]>) {
    setSavedNotice(false);
    setDraft((previous) => ({ ...previous, favorites: {
      ...previous.favorites, [id]: { ...previous.favorites[id], ...values },
    } }));
  }
  async function save() {
    setSaving(true); setSaveError(null);
    try { onSaved(await api.savePreferences(draft)); setSavedNotice(true); }
    catch (cause) { setSaveError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  }

  return <Drawer opened={opened} onClose={onClose} keepMounted position="right"
    title="模型配置" size="min(860px, 100vw)" padding={0} className="model-preview-settings"
    closeButtonProps={{ "aria-label": "关闭模型配置" }}>
    <Tabs value={page} onChange={setPage} className="model-settings-pages">
      <Tabs.List aria-label="模型配置分页" className="model-settings-nav">
        <Tabs.Tab value="providers">供应商与模型</Tabs.Tab>
        <Tabs.Tab value="selected" rightSection={<Badge size="sm" variant="light" color="gray">{selectedCount}</Badge>}>已选模型</Tabs.Tab>
        <Tabs.Tab value="search">网络搜索</Tabs.Tab>
      </Tabs.List>
      <Tabs.Panel value="providers" className="model-settings-page">
        <div className="catalog-toolbar">
          <div className="catalog-filters">
            <Select label="供应商" data={PROVIDERS.map((item) => ({ value: item.value, label: item.label + " · " + item.caption }))}
              value={provider} allowDeselect={false} disabled={saving}
              onChange={(value) => { setProvider(value!); setQuery(""); }} />
            <Button variant="default" leftSection={<IconRefresh size={15} />} loading={loading}
              onClick={() => setRefresh((value) => value + 1)}>刷新目录</Button>
          </div>
          <TextInput aria-label="搜索供应商模型" placeholder="搜索模型名称或 ID" value={query}
            onChange={(event) => setQuery(event.currentTarget.value)} leftSection={<IconSearch size={16} />}
            rightSection={query ? <ActionIcon variant="subtle" color="gray" aria-label="清空模型搜索" onClick={() => setQuery("")}><IconX size={14} /></ActionIcon> : null} />
        </div>
        <div className="catalog-scroll" aria-label="供应商模型目录" aria-busy={loading}>
          <div className="catalog-column-head" aria-hidden="true"><span>使用</span><span>模型</span></div>
          {loading ? <Stack gap={14} p="lg" aria-label="正在读取模型目录"><Skeleton height={48} /><Skeleton height={48} /><Skeleton height={48} /></Stack>
            : error ? <Alert color="red" title="模型目录读取失败" m="lg">
              <Text size="sm">{error}</Text><Button size="xs" variant="subtle" color="red" mt="sm" onClick={() => setRefresh((value) => value + 1)}>重新加载</Button>
            </Alert>
            : visible.length === 0 ? <div className="catalog-empty"><IconSearch size={25} stroke={1.4} /><Text fw={500}>没有找到匹配的模型</Text>
              <Text size="sm" c="dimmed">{query ? "试试其他关键词，或清空搜索。" : "当前供应商没有可展示的模型。"}</Text>
              {query ? <Button size="xs" variant="subtle" onClick={() => setQuery("")}>清空搜索</Button> : null}</div>
            : <div role="list" aria-label={`${providerName(provider)} 模型`}>
              {visible.map((item) => {
                const favorite = draft.favorites[item.id];
                const isDefault = item.id === draft.defaultModel;
                return <div key={item.id} role="listitem" className={`catalog-row${favorite ? " is-favorite" : ""}`}>
                  <div className="catalog-check"><Tooltip label={isDefault ? "请先在“已选模型”页更换默认模型" : "显示在聊天输入框"}>
                    <Checkbox aria-label={`在输入框显示 ${providerName(item.provider)} ${item.name}`} checked={favorite !== undefined}
                      disabled={isDefault || saving} onChange={(event) => toggle(item, event.currentTarget.checked)} />
                  </Tooltip></div>
                  <div className="catalog-model">
                    <Group gap={8} wrap="nowrap"><Text fw={550} size="sm">{item.name}</Text>{isDefault ? <span className="default-model-label">默认</span> : null}</Group>
                    <Text className="model-id" size="xs" c="dimmed">{item.id.slice(item.id.indexOf("/") + 1)}</Text>
                    <Text className="model-description" size="xs" c="dimmed">{item.description}</Text>
                  </div>
                </div>;
              })}
            </div>}
        </div>
      </Tabs.Panel>
      <Tabs.Panel value="selected" className="model-settings-page">
        <SelectedModelsPanel models={selectedModels} preferences={draft} saving={saving} onChange={updateFavorite}
          onRemove={(model) => toggle(model, false)}
          onDefaultChange={(value) => { setDraft((previous) => ({ ...previous, defaultModel: value })); setSavedNotice(false); }}
          testModel={api.testModel} />
      </Tabs.Panel>
      <Tabs.Panel value="search" className="model-settings-page"><SearchSettingsPanel models={allModels} /></Tabs.Panel>
    </Tabs>
    {page !== "search" ? <div className="catalog-footer">
      {hasInvalidThreshold ? <Text c="red" size="xs" role="alert">上下文整理阈值需要是正整数。</Text> : null}
      {saveError ? <Text c="red" size="xs" role="alert">{saveError}</Text> : null}
      <div className="catalog-actions">
        <Text size="xs" c={savedNotice ? "teal" : "dimmed"} role="status">
          {savedNotice ? <span className="saved-notice"><IconCheck size={13} />常用模型已保存</span>
            : changed ? "有未保存的修改" : "偏好保存在此预览的浏览器中"}
        </Text>
        <Group gap="xs" wrap="nowrap">
          <Button variant="subtle" color="gray" disabled={!changed || saving}
            onClick={() => { setDraft(structuredClone(preferences)); setSavedNotice(false); setSaveError(null); }}>撤销修改</Button>
          <Button disabled={!changed || hasInvalidThreshold} loading={saving} onClick={() => void save()}>保存配置</Button>
        </Group>
      </div>
    </div> : null}
  </Drawer>;
}
