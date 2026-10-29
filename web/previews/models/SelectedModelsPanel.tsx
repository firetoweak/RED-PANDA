import { ActionIcon, Button, Collapse, Group, NumberInput, Select, Text, Tooltip } from "@mantine/core";
import { IconAdjustments, IconAlertCircle, IconCheck, IconPlayerPlay, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { providerName, type CatalogModel, type Favorite, type ModelPreviewApi, type ModelTestResult, type Preferences } from "./api";

type ModelRowProps = {
  model: CatalogModel;
  favorite: Favorite;
  isDefault: boolean;
  saving: boolean;
  onChange(values: Partial<Favorite>): void;
  onRemove(): void;
  testModel: ModelPreviewApi["testModel"];
};

function SelectedModelRow({ model, favorite, isDefault, saving, onChange, onRemove, testModel }: ModelRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<ModelTestResult | null>(null);
  const label = `${providerName(model.provider)} ${model.name}`;

  async function test() {
    setTesting(true); setResult(null);
    try { setResult(await testModel(model, favorite.effort)); }
    catch (cause) { setResult({ ok: false, message: cause instanceof Error ? cause.message : String(cause) }); }
    finally { setTesting(false); }
  }

  return <div role="listitem" aria-label={label} className="selected-model-row">
    <div className="catalog-model">
      <Group gap={8} wrap="nowrap"><Text fw={550} size="sm">{model.name}</Text>{isDefault ? <span className="default-model-label">默认</span> : null}</Group>
      <Text className="model-id" size="xs" c="dimmed">{providerName(model.provider)}</Text>
    </div>
    <div className="catalog-effort selected-model-effort">
      {model.efforts.length ? <Select label="推理强度" aria-label={`${label} 默认推理强度`}
        data={[{ value: "default", label: "供应商默认" }, ...model.efforts]} value={favorite.effort ?? "default"}
        allowDeselect={false} disabled={saving || testing} size="xs"
        onChange={(value) => onChange({ effort: value === "default" ? null : value })} />
        : <Text size="xs" c="dimmed">未提供推理档位</Text>}
    </div>
    <Button className="selected-model-test" aria-label={`测试 ${label}`} size="xs" variant="default"
      loading={testing} disabled={saving} leftSection={<IconPlayerPlay size={13} />}
      onClick={() => void test()}>{result?.ok === false ? "重试" : "测试"}</Button>
    <Group gap={4} wrap="nowrap" className="selected-model-actions">
      <Tooltip label="更多设置"><ActionIcon variant="subtle" color="gray"
        aria-label={`${label} 更多设置`} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        <IconAdjustments size={16} /></ActionIcon></Tooltip>
      <Tooltip label={isDefault ? "请先更换新会话默认模型" : "删除模型"}><span>
        <ActionIcon variant="subtle" color="gray" aria-label={`删除 ${label}`} disabled={isDefault || saving} onClick={onRemove}>
          <IconTrash size={16} /></ActionIcon>
      </span></Tooltip>
    </Group>
    {testing || result ? <div className="model-test-result" role="status">
      {testing ? <Text size="xs" c="dimmed">正在测试…</Text>
        : result?.ok ? <Text size="xs" c="teal"><IconCheck size={13} />示例测试通过 · {result.latencyMs} ms</Text>
          : result ? <Text size="xs" c="red"><IconAlertCircle size={13} />{result.message}</Text> : null}
    </div> : null}
    <Collapse expanded={expanded} className="selected-model-advanced">
      <div className="catalog-advanced-content">
        <NumberInput label="上下文整理阈值" description="输入达到此 token 数时整理上下文。" value={favorite.compactTokens}
          min={1} allowNegative={false} allowDecimal={false} disabled={saving}
          onChange={(value) => onChange({ compactTokens: typeof value === "number" ? value : 0 })} />
        <Text size="xs" c="dimmed">示例上下文容量：{model.contextTokens.toLocaleString()} tokens</Text>
      </div>
    </Collapse>
  </div>;
}

export function SelectedModelsPanel({ models, preferences, saving, onChange, onRemove, onDefaultChange, testModel }: {
  models: CatalogModel[];
  preferences: Preferences;
  saving: boolean;
  onChange(id: string, values: Partial<Favorite>): void;
  onRemove(model: CatalogModel): void;
  onDefaultChange(id: string): void;
  testModel: ModelPreviewApi["testModel"];
}) {
  return <>
    <div className="selected-models-intro"><Text size="sm" c="dimmed">每个测试只检查当前模型与推理强度，此预览返回模拟结果。</Text></div>
    <div className="selected-models-scroll">
      <div className="selected-models-column-head" aria-hidden="true"><span>模型</span><span>默认推理强度</span><span>连接测试</span><span /></div>
      <div role="list" aria-label="已选模型">
        {models.map((model) => <SelectedModelRow key={`${model.id}:${preferences.favorites[model.id].effort ?? "default"}`}
          model={model} favorite={preferences.favorites[model.id]} isDefault={model.id === preferences.defaultModel}
          saving={saving} onChange={(values) => onChange(model.id, values)} onRemove={() => onRemove(model)} testModel={testModel} />)}
      </div>
    </div>
    <div className="selected-models-default catalog-default">
      <div><Text size="sm" fw={550}>新会话默认模型</Text><Text size="xs" c="dimmed" mt={4}>已有会话保留各自的选择。</Text></div>
      <Select aria-label="新会话默认模型" searchable allowDeselect={false} disabled={saving}
        value={preferences.defaultModel} data={models.map((item) => ({ value: item.id, label: `${providerName(item.provider)} · ${item.name}` }))}
        onChange={(value) => onDefaultChange(value!)} />
    </div>
  </>;
}
