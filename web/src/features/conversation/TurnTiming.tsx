import { Text } from "@mantine/core";
import { useEffect, useState } from "react";
import { formatElapsedTime } from "./timelineTurns";

export function TurnTiming({
  startedAt,
  running,
  elapsedMs,
}: {
  startedAt: string | null;
  running: boolean;
  elapsedMs: number | null;
}) {
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    if (!running || startedAt === null) {
      return;
    }
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);

  const counting = running && startedAt !== null;
  const duration = counting
    ? Math.floor((now - Date.parse(startedAt)) / 1000) * 1000
    : elapsedMs;
  if (duration === null) {
    return null;
  }

  return (
    <Text
      aria-live="off"
      c="dimmed"
      fz={12}
      title="从本轮消息提交起计时，包含思考、工具执行和等待时间"
    >
      {counting ? "正在执行 · " : "耗时 "}{formatElapsedTime(duration)}
    </Text>
  );
}
