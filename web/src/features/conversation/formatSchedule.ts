const UNITS: Record<string, string> = { m: "分钟", h: "小时", d: "天" };
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

function clock(minute: number, hour: number): string | null {
  if (!Number.isInteger(minute) || !Number.isInteger(hour) || minute < 0 || minute > 59 || hour < 0 || hour > 23) {
    return null;
  }
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

/** 把保存的时间表译成人能读的安排。译不出时保留原文。 */
export function formatSchedule(schedule: string): string {
  const text = schedule.trim().replace(/\s+/g, " ");
  const every = /^@every (\d+)([mhd])$/.exec(text);
  if (every !== null) {
    return `每 ${every[1]} ${UNITS[every[2]]}`;
  }
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(text);
  if (daily !== null) {
    const time = clock(Number(daily[1]), Number(daily[2]));
    if (time !== null) return `每天 ${time}`;
  }
  const weekdays = /^(\d{1,2}) (\d{1,2}) \* \* 1-5$/.exec(text);
  if (weekdays !== null) {
    const time = clock(Number(weekdays[1]), Number(weekdays[2]));
    if (time !== null) return `工作日 ${time}`;
  }
  const weekday = /^(\d{1,2}) (\d{1,2}) \* \* ([0-7])$/.exec(text);
  if (weekday !== null) {
    const time = clock(Number(weekday[1]), Number(weekday[2]));
    const day = Number(weekday[3]);
    if (time !== null) return `每周${WEEKDAYS[day === 7 ? 0 : day]} ${time}`;
  }
  const minutes = /^\*\/(\d+) \* \* \* \*$/.exec(text);
  if (minutes !== null && 60 % Number(minutes[1]) === 0) {
    return `每 ${minutes[1]} 分钟`;
  }
  const hours = /^0 \*\/(\d+) \* \* \*$/.exec(text);
  if (hours !== null && 24 % Number(hours[1]) === 0) {
    return `每 ${hours[1]} 小时`;
  }
  const monthly = /^(\d{1,2}) (\d{1,2}) (\d{1,2}) \* \*$/.exec(text);
  if (monthly !== null) {
    const time = clock(Number(monthly[1]), Number(monthly[2]));
    const day = Number(monthly[3]);
    if (time !== null && day >= 1 && day <= 31) return `每月 ${day} 日 ${time}`;
  }
  return schedule;
}
