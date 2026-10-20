import { expect, it } from "vitest";
import { formatSchedule } from "../src/features/conversation/formatSchedule";

it.each([
  ["*/40 * * * *", "*/40 * * * *"],
  ["*/90 * * * *", "*/90 * * * *"],
  ["0 */5 * * *", "0 */5 * * *"],
  ["0 */25 * * *", "0 */25 * * *"],
  ["*/15 * * * *", "每 15 分钟"],
  ["0 */6 * * *", "每 6 小时"],
  ["@every 40m", "每 40 分钟"],
])("仅将等间隔安排译为固定间隔：%s", (schedule, expected) => {
  expect(formatSchedule(schedule)).toBe(expected);
});
