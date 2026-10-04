export const DEFAULT_SIDEBAR_WIDTH = 288;
export const MIN_SIDEBAR_WIDTH = 220;
export const MAX_SIDEBAR_WIDTH = 520;

const STORAGE_KEY = "redpanda.sidebarWidth";

export function clampSidebarWidth(width: number): number {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, width));
}

/** 读取侧栏宽度；缺省或非法时回到当前默认宽度。 */
export function readSidebarWidth(): number {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) {
      return DEFAULT_SIDEBAR_WIDTH;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      return DEFAULT_SIDEBAR_WIDTH;
    }
    return clampSidebarWidth(value);
  } catch {
    return DEFAULT_SIDEBAR_WIDTH;
  }
}

/** 宽度是纯界面偏好，写不进去就当作没记住。 */
export function writeSidebarWidth(width: number): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(clampSidebarWidth(width)));
  } catch {
    // 忽略：宽度偏好丢失不影响功能。
  }
}
