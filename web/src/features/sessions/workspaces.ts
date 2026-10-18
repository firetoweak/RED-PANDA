const COLLAPSED_STORAGE_KEY = "redpanda.collapsedWorkspaces";

/** 读取折叠偏好；只认值为 true 的条目，解析失败当作没有偏好。 */
export function readCollapsedWorkspaces(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
    if (raw === null) {
      return {};
    }
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const collapsed: Record<string, boolean> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (value === true) {
        collapsed[key] = true;
      }
    }
    return collapsed;
  } catch {
    return {};
  }
}

/** 折叠状态是纯界面偏好，写不进去（隐私模式等）就当作没记住。 */
export function writeCollapsedWorkspaces(
  collapsed: Record<string, boolean>,
): void {
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(collapsed));
  } catch {
    // 忽略：折叠偏好丢失不影响功能。
  }
}
