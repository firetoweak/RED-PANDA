import { Alert, Anchor, Badge, Button, Group, Select, Skeleton, Stack, Switch, Text, TextInput } from "@mantine/core";
import { IconArrowUpRight, IconCheck, IconLink, IconSearch } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { providerName, type CatalogModel } from "./api";
import { searchPreviewApi, type SearchPreviewApi, type SearchProvider, type SearchSettings, type SearchTestResult } from "./searchApi";

export function SearchSettingsPanel({ models, api = searchPreviewApi }: { models: CatalogModel[]; api?: SearchPreviewApi }) {
  const [saved, setSaved] = useState<SearchSettings | null>(null);
  const [draft, setDraft] = useState<SearchSettings | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  const [query, setQuery] = useState("OpenAI Web Search 如何使用");
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [result, setResult] = useState<SearchTestResult | null>(null);

  useEffect(() => {
    let current = true;
    setLoadError(null);
    api.loadSettings().then((value) => {
      if (current) { setSaved(value); setDraft(structuredClone(value)); }
    }, (cause: unknown) => { if (current) setLoadError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { current = false; };
  }, [api, reload]);

  function change(values: Partial<SearchSettings>) {
    setDraft((value) => ({ ...value!, ...values }));
    setSavedNotice(false); setSaveError(null); setResult(null); setTestError(null);
  }
  async function save() {
    setSaving(true); setSaveError(null);
    try { setSaved(await api.saveSettings(draft!)); setSavedNotice(true); }
    catch (cause) { setSaveError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  }
  async function test() {
    if (testing || !draft!.enabled || !query.trim()) return;
    setTesting(true); setTestError(null); setResult(null);
    try { setResult(await api.testSearch(query.trim(), structuredClone(draft!))); }
    catch (cause) { setTestError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setTesting(false); }
  }

  if (loadError) return <Alert color="red" title="搜索配置读取失败" m="lg">{loadError}<Button mt="sm" size="xs" variant="subtle" onClick={() => setReload((value) => value + 1)}>重新加载</Button></Alert>;
  if (!draft || !saved) return <Stack p="lg"><Skeleton height={40} /><Skeleton height={80} /></Stack>;
  const changed = JSON.stringify(draft) !== JSON.stringify(saved);
  const disabled = !draft.enabled || saving || testing;

  return <>
    <div className="search-settings-scroll">
      <div className="search-enable-row">
        <div><Text fw={550} size="sm">启用网络搜索</Text><Text size="xs" c="dimmed" mt={5}>搜索服务独立于聊天模型，复用供应商连接。</Text></div>
        <Switch aria-label="启用网络搜索" checked={draft.enabled} disabled={saving || testing} onChange={(event) => change({ enabled: event.currentTarget.checked })} />
      </div>
      <div className="search-service-fields">
        <Select label="搜索服务" value={draft.provider} allowDeselect={false} disabled={disabled}
          data={[{ value: "deepseek", label: "DeepSeek" }, { value: "openai", label: "OpenAI" }]}
          onChange={(value) => change({ provider: value as SearchProvider })} />
        <div className="search-connection"><Text size="xs" c="dimmed">供应商连接</Text>
          <div><IconLink size={15} /><Text size="sm">{providerName(draft.provider)} · 示例连接</Text><Badge size="xs" variant="light" color="teal">已配置</Badge></div>
        </div>
        {draft.provider === "openai" ? <Select className="search-model-field" label="搜索模型" description="单独用于搜索，不改变聊天输入框的模型选择。"
          searchable allowDeselect={false} value={draft.openaiModel} disabled={disabled}
          data={models.filter((model) => model.provider === "openai").map((model) => ({ value: model.id, label: model.name }))}
          onChange={(value) => change({ openaiModel: value! })} /> : null}
      </div>
      {!draft.enabled ? <Text size="xs" c="dimmed" mt="md">网络搜索已停用，服务与模型选择会保留。</Text> : null}
      <section className="search-test-section" aria-label="搜索测试">
        <div className="search-test-heading"><Text fw={550} size="sm">测试搜索</Text><Badge size="xs" variant="light" color="gray">示例结果</Badge></div>
        <form className="search-test-form" onSubmit={(event) => { event.preventDefault(); void test(); }}>
          <TextInput label="搜索词" placeholder="输入想搜索的问题" value={query} disabled={disabled}
            onChange={(event) => { setQuery(event.currentTarget.value); setResult(null); setTestError(null); }} />
          <Button variant="default" type="submit" leftSection={<IconSearch size={15} />} loading={testing}
            disabled={!draft.enabled || saving || !query.trim()}>{testError ? "重试搜索" : "测试搜索"}</Button>
        </form>
        {testing ? <div className="search-result-loading" role="status"><Text size="sm" c="dimmed">正在搜索…</Text><Skeleton height={12} /><Skeleton height={12} w="75%" /></div>
          : testError ? <Alert color="red" title="搜索未完成" mt="lg">{testError}</Alert>
            : result ? <section className="search-result" aria-label="搜索测试结果">
              <Group gap={8} className="search-result-meta"><IconCheck size={14} /><Text size="xs">{providerName(result.provider)}
                {result.model ? ` · ${models.find((model) => model.id === result.model)!.name}` : ""} · {result.latencyMs} ms · {result.sources.length} 个来源</Text></Group>
              <Text size="sm" className="search-result-summary">{result.summary}</Text>
              <div role="list" aria-label="搜索来源">{result.sources.map((source) => <article key={source.url} role="listitem" className="search-source">
                <Anchor href={source.url} target="_blank" rel="noreferrer" size="sm">{source.title}<IconArrowUpRight size={13} /></Anchor>
                <Text size="xs" c="dimmed" className="search-source-url">{source.url}</Text><Text size="xs" c="dimmed" mt={6}>{source.snippet}</Text>
              </article>)}</div>
            </section> : <div className="search-result-empty"><IconSearch size={22} stroke={1.4} /><Text size="sm" c="dimmed">测试后在这里查看结果与来源。</Text></div>}
      </section>
    </div>
    <div className="catalog-footer">
      {saveError ? <Text size="xs" c="red" role="alert">{saveError}</Text> : null}
      <div className="catalog-actions"><Text size="xs" c={savedNotice ? "teal" : "dimmed"} role="status">
        {savedNotice ? <span className="saved-notice"><IconCheck size={13} />搜索配置已保存</span> : changed ? "有未保存的搜索修改" : "搜索配置独立保存在此预览中"}
      </Text><Group gap="xs" wrap="nowrap"><Button variant="subtle" color="gray" disabled={!changed || saving || testing}
        onClick={() => { setDraft(structuredClone(saved)); setSavedNotice(false); setSaveError(null); setResult(null); setTestError(null); }}>撤销修改</Button>
        <Button disabled={!changed || testing} loading={saving} onClick={() => void save()}>保存搜索配置</Button></Group>
      </div>
    </div>
  </>;
}
