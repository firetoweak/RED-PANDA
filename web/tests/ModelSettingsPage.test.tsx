import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelSettings } from "../src/features/models/ModelSettings";

const {data,dispatch} = vi.hoisted(() => ({
  data:{config:{model:{default:"stepfun/step-5-preview",candidates:[{model:"stepfun/step-5-preview",compact_threshold_tokens:200000}]}},
    providers:[{provider:"stepfun",configured:true,missing:null,local:false}],connections_path:"/personal/connections.json"},
  dispatch:vi.fn(),
}));
vi.mock("../src/app/hooks",()=>({useAppSelector:()=>"connection",useAppDispatch:()=>dispatch}));
vi.mock("../src/api/kondenApi",()=>({
  useGetModelSettingsQuery:()=>({data}),
  kondenApi:{endpoints:Object.fromEntries(["getModelCatalog","saveModelSettings","getSearchConfig"].map((kind)=>[kind,{initiate:(value:unknown)=>({kind,value})}]))},
}));
beforeEach(()=>{
  dispatch.mockReset().mockImplementation((action)=>({unwrap:async()=>{
    if(action.kind==="getModelCatalog")return [{model:"stepfun/step-5-preview",name:"Step 5",context_window:256000,
      reasoning_levels:["low","medium","high"],default_reasoning_effort:null,web_search:false}];
    if(action.kind==="getSearchConfig")return {enabled:true,provider:"deepseek",openai_model:null};
    return {...data,config:action.value.config};
  }}));
  vi.stubGlobal("matchMedia",()=>({matches:false,addEventListener(){},removeEventListener(){}}));
  vi.stubGlobal("ResizeObserver",class{observe(){}unobserve(){}disconnect(){}});
  HTMLElement.prototype.scrollIntoView=vi.fn();
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});

it("真实接口适配保留原始模型标识，保存所选强度，恢复默认时省略档位",async()=>{
  render(<MantineProvider env="test"><ModelSettings/></MantineProvider>);
  fireEvent.click(await screen.findByRole("tab",{name:/已选模型/}));
  const effort=await screen.findByRole("combobox",{name:"StepFun Step 5 默认推理强度"});
  fireEvent.click(effort);
  fireEvent.click(await screen.findByRole("option",{name:"高",exact:true}));
  fireEvent.click(screen.getByRole("button",{name:"保存配置",exact:true}));
  await waitFor(()=>expect(dispatch).toHaveBeenCalledWith({kind:"saveModelSettings",value:{connectionId:"connection",config:{model:{
    default:"stepfun/step-5-preview",candidates:[{model:"stepfun/step-5-preview",compact_threshold_tokens:200000,reasoning_effort:"high"}],
  }}}}));
  await screen.findByText("常用模型已保存");
  fireEvent.click(screen.getByRole("combobox",{name:"StepFun Step 5 默认推理强度"}));
  fireEvent.click(await screen.findByRole("option",{name:"供应商默认",exact:true}));
  fireEvent.click(screen.getByRole("button",{name:"保存配置",exact:true}));
  await waitFor(()=>expect(dispatch).toHaveBeenLastCalledWith({kind:"saveModelSettings",value:{connectionId:"connection",config:data.config}}));
});
