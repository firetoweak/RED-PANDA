import { Button, Stack, Text } from "@mantine/core";
import { useState } from "react";
import type { ModelTestResult } from "../../api/contracts";
import { useTestModelMutation } from "../../api/helpermeApi";

export function ModelTestButton({ model, connectionId, saved, configured }: {
  model: string;
  connectionId: string | null;
  saved: boolean;
  configured: boolean;
}) {
  const [test, { isLoading, error }] = useTestModelMutation();
  const [result, setResult] = useState<ModelTestResult | null>(null);

  async function run() {
    if (connectionId === null) return;
    setResult(null);
    try {
      setResult(await test({ connectionId, model }).unwrap());
    } catch { /* 请求错误由 mutation 状态展示。 */ }
  }

  const reason = !saved ? "请先保存模型" : !configured ? "请先填写供应商连接配置" : undefined;
  return <Stack gap={4} style={{ maxWidth: 220 }}>
    <Button size="xs" variant="subtle" aria-label={"测试 " + model} title={reason}
      disabled={connectionId === null || !saved || !configured} loading={isLoading}
      onClick={() => void run()}>测试</Button>
    {result ? <Text size="xs" c={result.ok ? "green" : "red"} role="status" style={{ overflowWrap: "anywhere" }}>
      {result.ok ? "测试通过" : "测试失败"} · {result.elapsed_ms.toLocaleString()} ms
      {!result.ok ? <><br />{result.message}</> : null}
    </Text> : null}
    {error ? <Text size="xs" c="red" role="alert" style={{ overflowWrap: "anywhere" }}>
      测试请求失败：{typeof error === "string" ? error : JSON.stringify(error)}
    </Text> : null}
  </Stack>;
}
