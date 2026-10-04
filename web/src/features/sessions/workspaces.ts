import type { SessionSummary, Workspace } from "../../api/contracts";

/**
 * 侧栏的「工作区」视图模型。
 *
 * 工作区是沙箱边界，一条会话只属于一个工作区（后端 SessionSummary.workspace_id
 * 是会话创建时定下的归属，落进去即固定）。分组完全按归属来做，不再有
 * 「默认工作区」这种退化形态。
 */
export type WorkspaceGroup = {
  id: string;
  name: string;
  sessions: SessionSummary[];
};

function lastActivity(sessions: SessionSummary[]): number {
  let latest = 0;
  for (const session of sessions) {
    if (session.updated_at === null) {
      continue;
    }
    const timestamp = Date.parse(session.updated_at);
    if (!Number.isNaN(timestamp) && timestamp > latest) {
      latest = timestamp;
    }
  }
  return latest;
}

/**
 * 按归属分组。分组顺序按「最近有会话活动的在前」排，空工作区沉到后面——
 * 这样「新建会话缺省落点 = 最近一次聊天的工作区」就是第一个非空分组。
 */
export function groupSessions(
  sessions: SessionSummary[],
  workspaces: Workspace[],
): WorkspaceGroup[] {
  const groups = workspaces.map((workspace) => ({
    id: workspace.workspace_id,
    name: workspace.name,
    sessions: [] as SessionSummary[],
  }));
  const byId = new Map(groups.map((group) => [group.id, group]));
  const orphans: WorkspaceGroup[] = [];
  for (const session of sessions) {
    const group = byId.get(session.workspace_id);
    if (group !== undefined) {
      group.sessions.push(session);
      continue;
    }
    // registry 里没有这个归属（比如 registry 被手工改过）：照原样列出来，
    // 用 id 当名字，不静默丢数据。
    orphans.push({
      id: session.workspace_id,
      name: session.workspace_id,
      sessions: [session],
    });
  }
  return [...groups, ...orphans].sort(
    (left, right) => lastActivity(right.sessions) - lastActivity(left.sessions),
  );
}

export const WORKSPACE_SESSION_PREVIEW = 5;
export const DRAFT_TITLE = "new agent";

export function workspaceSessionRows(
  sessions: SessionSummary[],
  workspaceId: string,
  draftId: string | undefined,
  expanded: boolean,
  draftTitle: string = DRAFT_TITLE,
): { shown: SessionSummary[]; hiddenCount: number } {
  const real = sessions.filter((session) => session.session_id !== draftId);
  const shownReal = previewSessions(real, expanded);
  const draft =
    draftId === undefined
      ? []
      : [
          {
            session_id: draftId,
            workspace_id: workspaceId,
            title: draftTitle,
            updated_at: null,
            activity: "idle" as const,
            has_active_subagents: false,
          },
        ];
  return {
    shown: [...draft, ...shownReal],
    hiddenCount: real.length - shownReal.length,
  };
}

export function previewSessions<T>(sessions: T[], expanded: boolean): T[] {
  if (expanded || sessions.length <= WORKSPACE_SESSION_PREVIEW) {
    return sessions;
  }
  return sessions.slice(0, WORKSPACE_SESSION_PREVIEW);
}

export function draftSessionId(
  drafts: Record<string, string>,
  workspaceId: string | null,
): string | undefined {
  if (workspaceId === null) {
    return undefined;
  }
  return drafts[workspaceId];
}

export function workspaceOfSession(
  sessionId: string | undefined,
  sessions: SessionSummary[],
  drafts: Record<string, string>,
): string | undefined {
  if (sessionId === undefined) {
    return undefined;
  }
  const listed = sessions.find((session) => session.session_id === sessionId);
  if (listed !== undefined) {
    return listed.workspace_id;
  }
  return Object.entries(drafts).find(([, draftId]) => draftId === sessionId)?.[0];
}

/**
 * 新建会话的缺省落点：最近一次聊天的工作区；还没有任何带归属的会话时，
 * 落最近创建的工作区；一个工作区都没有时返回 null（由界面引导创建）。
 */
export function defaultWorkspaceId(
  groups: WorkspaceGroup[],
  workspaces: Workspace[],
): string | null {
  const withSessions = groups.find((group) => group.sessions.length > 0);
  if (withSessions !== undefined) {
    return withSessions.id;
  }
  if (workspaces.length === 0) {
    return null;
  }
  const [latest] = [...workspaces].sort(
    (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
  );
  return latest?.workspace_id ?? null;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** 把 updated_at 渲染成侧栏里的相对时间；无法解析时返回空串。 */
export function formatRelativeTime(
  value: string | null,
  now: number = Date.now(),
): string {
  if (value === null) {
    return "";
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return "";
  }
  const elapsed = now - timestamp;
  if (elapsed < MINUTE) {
    return "刚刚";
  }
  if (elapsed < HOUR) {
    return `${Math.floor(elapsed / MINUTE)}m`;
  }
  if (elapsed < DAY) {
    return `${Math.floor(elapsed / HOUR)}h`;
  }
  return `${Math.floor(elapsed / DAY)}d`;
}

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
