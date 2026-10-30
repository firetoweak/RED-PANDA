import { Alert, Badge, Button, Group, Select, Skeleton, Stack, Switch, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import type { ImageCatalog, ImageConfig } from "../../api/contracts";

export type ImageApi = {
  loadSettings(): Promise<ImageConfig>;
  loadModels(): Promise<ImageCatalog>;
  saveSettings(value:ImageConfig): Promise<ImageConfig>;
};

export function ImageSettingsPanel({active, connected, configured, api}: {
  active:boolean; connected:boolean; configured:boolean; api:ImageApi;
}) {
  const [saved, setSaved] = useState<ImageConfig | null>(null);
  const [draft, setDraft] = useState<ImageConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [models, setModels] = useState<ImageCatalog>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogReload, setCatalogReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState(false);

  useEffect(() => {
    if (!active || saved !== null) return;
    let current = true;
    setLoadError(null);
    api.loadSettings().then((value) => {
      if (current) { setSaved(value); setDraft(value); }
    }, (cause:unknown) => { if (current) setLoadError(String(cause)); });
    return () => { current = false; };
  }, [active, api, saved, reload]);
  useEffect(() => {
    if (!active || !configured) return;
    let current = true;
    setCatalogLoading(true); setCatalogError(null);
    api.loadModels().then((value) => { if (current) setModels(value); },
      (cause:unknown) => { if (current) setCatalogError(String(cause)); })
      .finally(() => { if (current) setCatalogLoading(false); });
    return () => { current = false; };
  }, [active, configured, api, catalogReload]);

  function change(value:Partial<ImageConfig>) {
    setDraft((previous) => ({...previous!, ...value})); setNotice(false); setSaveError(null);
  }
  async function save() {
    setSaving(true); setSaveError(null);
    try {
      const value = await api.saveSettings(draft!);
      setSaved(value); setDraft(value); setNotice(true);
    } catch (cause) { setSaveError(String(cause)); }
    finally { setSaving(false); }
  }
  if (loadError) return <Alert color="red" title="图片配置读取失败" m="lg">{loadError}
    <Button variant="subtle" size="xs" onClick={() => setReload((value) => value + 1)}>重新加载图片配置</Button></Alert>;
  if (!draft || !saved) return <Stack p="lg"><Skeleton height={40}/><Skeleton height={80}/></Stack>;
  const changed = draft.enabled !== saved.enabled || draft.model !== saved.model;
  const disabled = saving || !connected;
  const choices = models.map((item) => ({value:item.model,label:item.name}));
  if (draft.model !== null && !models.some((item) => item.model === draft.model)) {
    choices.push({value:draft.model,label:draft.model.slice(7) + "（当前选择）"});
  }
  return <>
    <div className="image-settings-scroll">
      <Stack gap="xl">
        <div><Switch label="启用图片生成" checked={draft.enabled} disabled={disabled}
          onChange={(event) => change({enabled:event.currentTarget.checked})}/>
          <Text size="xs" c="dimmed" mt="sm">在 Bot 对话中用文字生成图片，独立于聊天模型，按 OpenAI 用量计费。</Text></div>
        <Group gap="sm"><Text size="sm">OpenAI API</Text>
          <Badge size="xs" variant="light" color={configured ? "teal" : "gray"}>{configured ? "已配置" : "未配置"}</Badge></Group>
        {!configured ? <Text size="sm" c="dimmed">请先在供应商连接文件中配置 OpenAI API 密钥。</Text> : null}
        <Select label="图片模型" description="目录来自当前 OpenAI 连接；可用权限以实际生成结果为准。"
          placeholder={catalogLoading ? "正在读取图片模型…" : "选择图片模型"} searchable allowDeselect={false}
          value={draft.model} data={choices} disabled={disabled || !draft.enabled || !configured || catalogLoading || catalogError !== null}
          onChange={(value) => change({model:value})}/>
        {catalogError ? <Alert color="red" title="图片模型目录读取失败">{catalogError}
          <Button variant="subtle" size="xs" onClick={() => setCatalogReload((value) => value + 1)}>重新加载图片模型</Button></Alert>
          : configured && !catalogLoading && models.length === 0 ? <Text size="sm" c="dimmed">当前连接没有返回可选择的图片模型。</Text> : null}
        <Text size="sm" c="dimmed">保存配置不会生成图片。启用后，在 Bot 对话中描述想要的图片；生成后可查看预览并下载原图。第一版支持文字生图。</Text>
      </Stack>
    </div>
    <div className="catalog-footer">
      {saveError ? <Text size="xs" c="red" role="alert">{saveError}</Text> : null}
      <div className="catalog-actions"><Text size="xs" c={notice ? "teal" : "dimmed"} role="status">
        {notice ? "图片配置已保存" : changed ? "有未保存的图片修改" : "图片配置独立保存"}</Text>
        <Group gap="xs" wrap="nowrap"><Button variant="subtle" color="gray" disabled={disabled || !changed}
          onClick={() => {setDraft(saved); setNotice(false); setSaveError(null);}}>撤销修改</Button>
          <Button loading={saving} disabled={!changed || disabled || (draft.enabled && (!configured || !draft.model))}
            onClick={() => void save()}>保存图片配置</Button></Group>
      </div>
    </div>
  </>;
}
