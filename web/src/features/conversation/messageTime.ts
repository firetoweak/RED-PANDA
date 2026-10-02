export function formatMessageTime(value: string): { short: string; full: string } {
  const date = new Date(value);
  const today = new Date();
  const clock = date.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const sameDay = date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth()
    && date.getDate() === today.getDate();
  let short = clock;
  if (!sameDay) {
    short = `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
  }
  if (date.getFullYear() !== today.getFullYear()) {
    short = `${date.getFullYear()}/${short}`;
  }
  return { short, full: date.toLocaleString("zh-CN") };
}
