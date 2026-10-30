import { Alert, Skeleton, Stack } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import { kondenApi, useGetModelSettingsQuery } from "../../api/kondenApi";
import type { SearchConfig } from "../../api/contracts";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { ModelSettingsPanel } from "./ModelSettingsPanel";
import { catalogModels, configuredModel, configFrom, preferencesFrom, type ModelSettingsApi, type Preferences } from "./modelUi";
import type { SearchApi, SearchSettings } from "./searchTypes";
import type { ImageApi } from "./ImageSettingsPanel";
import "./models.css";

function searchFrom(value: SearchConfig): SearchSettings {
  return {enabled:value.enabled, provider:value.provider, openaiModel:value.openai_model};
}
function searchConfig(value: SearchSettings): SearchConfig {
  return {enabled:value.enabled, provider:value.provider, openai_model:value.openaiModel};
}

export function ModelSettings({ active = true }: { active?: boolean }) {
  const dispatch = useAppDispatch();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const {data, error} = useGetModelSettingsQuery(undefined, {skip:!active, pollingInterval:active ? 5000 : 0});
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  useEffect(() => { if (data) setPreferences(preferencesFrom(data.config)); }, [data]);
  const api = useMemo<ModelSettingsApi>(() => ({
    async loadModels(provider) {
      const value = await dispatch(kondenApi.endpoints.getModelCatalog.initiate(provider, {subscribe:false, forceRefetch:true})).unwrap();
      return catalogModels(value);
    },
    async savePreferences(value) {
      if (connectionId === null) throw new Error("连接已断开，请等待重新连接。");
      const saved = await dispatch(kondenApi.endpoints.saveModelSettings.initiate({connectionId,config:configFrom(value)})).unwrap();
      return preferencesFrom(saved.config);
    },
    async testModel(model, effort) {
      if (connectionId === null) throw new Error("连接已断开，请等待重新连接。");
      const result = await dispatch(kondenApi.endpoints.testModel.initiate({connectionId, model:model.id, reasoningEffort:effort})).unwrap();
      return result.ok ? {ok:true,latencyMs:result.elapsed_ms} : {ok:false,message:result.message};
    },
  }), [connectionId, dispatch]);
  const searchApi = useMemo<SearchApi>(() => ({
    async loadSettings() {
      return searchFrom(await dispatch(kondenApi.endpoints.getSearchConfig.initiate(undefined,{subscribe:false,forceRefetch:true})).unwrap());
    },
    async saveSettings(value) {
      if (connectionId === null) throw new Error("连接已断开，请等待重新连接。");
      return searchFrom(await dispatch(kondenApi.endpoints.saveSearchConfig.initiate({connectionId,config:searchConfig(value)})).unwrap());
    },
    async testSearch(query, value) {
      if (connectionId === null) throw new Error("连接已断开，请等待重新连接。");
      const result = await dispatch(kondenApi.endpoints.testSearch.initiate({connectionId,query,config:searchConfig(value)})).unwrap();
      return {...result,latencyMs:result.elapsed_ms};
    },
  }), [connectionId, dispatch]);
  const imageApi = useMemo<ImageApi>(() => ({
    async loadSettings() {
      return dispatch(kondenApi.endpoints.getImageConfig.initiate(undefined,{subscribe:false,forceRefetch:true})).unwrap();
    },
    async loadModels() {
      return dispatch(kondenApi.endpoints.getImageCatalog.initiate(undefined,{subscribe:false,forceRefetch:true})).unwrap();
    },
    async saveSettings(config) {
      if (connectionId === null) throw new Error("连接已断开，请等待重新连接。");
      return dispatch(kondenApi.endpoints.saveImageConfig.initiate({connectionId,config})).unwrap();
    },
  }), [connectionId, dispatch]);
  if (error) return <Alert color="red" title="模型配置读取失败">{JSON.stringify(error)}</Alert>;
  if (!data || !preferences) return <Stack p="lg"><Skeleton height={50}/><Skeleton height={150}/></Stack>;
  return <ModelSettingsPanel active={active} connected={connectionId !== null} preferences={preferences}
    allModels={data.config.model.candidates.map(configuredModel)} onSaved={setPreferences} api={api}
    searchApi={searchApi} imageApi={imageApi} connections={data.providers} />;
}
