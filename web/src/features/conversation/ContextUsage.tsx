import { Text, Tooltip } from "@mantine/core";

export function ContextUsage({ inputTokens, limit }: { inputTokens: number | null; limit: number }) {
  const percent = inputTokens !== null && limit > 0 ? Math.round(inputTokens / limit * 100) : null;
  const title = percent !== null ? `${percent}% 已用`
    : inputTokens === null ? "暂无本窗口用量" : "上下文用量";
  const tokens = `${inputTokens === null ? "—" : formatTokens(inputTokens)} / ${limit > 0 ? formatTokens(limit) : "—"} tokens`;
  const ratio = inputTokens !== null && limit > 0 ? Math.min(inputTokens / limit, 1) : 0;
  const radius = 5;
  const circumference = 2 * Math.PI * radius;
  return <Tooltip radius="md" label={<div>
    <Text size="sm" ta="center">
      {percent === null ? title : (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <span>{percent}%</span><span>已用</span>
        </span>
      )}
    </Text>
    <Text size="sm" ta="center" style={{ opacity: 0.65 }}>{tokens}</Text>
  </div>}>
    <span role="img" aria-label={`${title} · ${tokens}`} style={{ display: "inline-flex", padding: 6 }}>
      <svg aria-hidden className="context-ring" height="14" viewBox="0 0 14 14" width="14">
        <circle cx="7" cy="7" fill="none" r={radius} stroke="var(--konden-ring-track)" strokeWidth="2" />
        <circle cx="7" cy="7" fill="none" r={radius} stroke="var(--mantine-color-ember-4)"
          strokeDasharray={circumference} strokeDashoffset={circumference * (1 - ratio)}
          strokeLinecap="round" strokeWidth="2" transform="rotate(-90 7 7)" />
      </svg>
    </span>
  </Tooltip>;
}

function formatTokens(tokens: number) {
  if (tokens < 1000) return String(tokens);
  if (tokens % 1000 === 0) return `${tokens / 1000}K`;
  return `${(tokens / 1000).toFixed(1)}K`;
}
