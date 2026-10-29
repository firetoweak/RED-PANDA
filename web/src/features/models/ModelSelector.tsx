import { Alert, Button, Stack } from "@mantine/core";
import { useGetModelCatalogQuery, useGetModelSettingsQuery, useGetSessionModelQuery, useSetSessionModelMutation } from "../../api/kondenApi";
import { ModelPicker, type Selection } from "./ModelPicker";
import { catalogModels, configuredModel, preferencesFrom } from "./modelUi";
import "./models.css";

export function ModelSelector({sessionId, connectionId, onChanging}: {sessionId:string;connectionId:string|null;onChanging?(changing:boolean):void}) {
  const {data:settings,error:settingsError} = useGetModelSettingsQuery(undefined,{pollingInterval:5000});
  const {data:selection,error} = useGetSessionModelQuery(sessionId,{pollingInterval:2000});
  const [select,{isLoading,error:selectError}] = useSetSessionModelMutation();
  const selected = selection?.selected;
  const provider = selected?.model.split("/")[0];
  const {currentData:catalog,error:catalogError} = useGetModelCatalogQuery(provider ?? "", {skip:provider === undefined});
  async function change(value:Selection) {
    if (connectionId === null) return;
    onChanging?.(true);
    try { await select({sessionId,connectionId,model:value.model,reasoningEffort:value.effort}).unwrap(); }
    catch { /* The mutation state displays its error. */ }
    finally { onChanging?.(false); }
  }
  const models = settings ? [...new Map([...settings.config.model.candidates.map(configuredModel),
    ...catalogModels(catalog ?? [])].map((item) => [item.id,item])).values()] : [];
  return <Stack gap={3}>
    {settings && selected ? <ModelPicker models={models} preferences={preferencesFrom(settings.config)}
      selection={{model:selected.model,effort:selected.reasoning_effort ?? null}} onSelect={(value) => void change(value)}
      pending={selection.pending} disabled={connectionId === null || isLoading}
      disabledProviders={settings.providers.filter((item) => !item.configured).map((item) => item.provider)} />
      : <Button size="compact-sm" variant="subtle" color="gray" disabled>选择模型</Button>}
    {selectError || error || settingsError || catalogError ? <Alert color="red" p="xs">{typeof (selectError ?? catalogError) === "string"
      ? String(selectError ?? catalogError) : JSON.stringify(selectError ?? error ?? settingsError ?? catalogError)}</Alert> : null}
  </Stack>;
}
