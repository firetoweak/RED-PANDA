import { Button, Center, Loader, Stack, Text } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  useCreateSessionMutation,
  useGetSessionsQuery,
  useGetWorkspacesQuery,
} from "../../api/redpandaApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { ConversationWelcome } from "../../app/ConversationWelcome";
import { bindDraftSession } from "./bindDraft";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import { defaultWorkspaceId, draftSessionId, groupSessions } from "./workspaces";

export function DraftRedirect() {
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const { workspaceId: routeWorkspaceId } = useParams();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const draftSessions = useAppSelector((state) => state.runtime.draftSessions);
  const [createSession] = useCreateSessionMutation();
  const { data: sessions = [] } = useGetSessionsQuery();
  const { data: workspaces = [], isLoading: workspacesLoading } =
    useGetWorkspacesQuery();
  const targetWorkspaceId =
    routeWorkspaceId ??
    defaultWorkspaceId(groupSessions(sessions, workspaces), workspaces);
  const existingDraft = draftSessionId(draftSessions, targetWorkspaceId);
  const [openError, setOpenError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    setOpenError(null);
    if (connectionId === null) {
      return;
    }
    if (existingDraft !== undefined) {
      navigate(`/sessions/${encodeURIComponent(existingDraft)}`, {
        replace: true,
      });
      return;
    }
    if (targetWorkspaceId === null) {
      return;
    }
    let cancelled = false;
    void bindDraftSession(
      connectionId,
      targetWorkspaceId,
      createSession,
      dispatch,
    )
      .then((id) => {
        if (!cancelled) {
          navigate(`/sessions/${encodeURIComponent(id)}`, { replace: true });
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setOpenError(cause instanceof Error ? cause.message : String(cause));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    attempt,
    connectionId,
    createSession,
    dispatch,
    existingDraft,
    navigate,
    targetWorkspaceId,
  ]);

  if (
    connectionId !== null &&
    targetWorkspaceId === null &&
    !workspacesLoading &&
    workspaces.length === 0
  ) {
    return <NoWorkspaceYet />;
  }

  if (openError !== null) {
    return (
      <Center h="100%">
        <Stack align="center" gap="sm">
          <Text c="red" fw={600} size="sm">
            无法打开会话
          </Text>
          <Text c="dimmed" maw={420} size="sm" ta="center">
            {openError}
          </Text>
          <Button
            onClick={() => {
              setOpenError(null);
              setAttempt((value) => value + 1);
            }}
            variant="light"
          >
            重试
          </Button>
        </Stack>
      </Center>
    );
  }

  return (
    <Center h="100%">
      <Stack align="center" gap="sm">
        <Loader size="sm" />
        <Text c="dimmed" size="sm">
          {connectionId === null ? "正在连接…" : "正在打开会话…"}
        </Text>
      </Stack>
    </Center>
  );
}

/** 一个工作区都没有：会话无处可落，先引导创建。 */
function NoWorkspaceYet() {
  const [opened, { open, close }] = useDisclosure(false);
  return (
    <ConversationWelcome>
      <Text c="dimmed" maw={380} size="sm" mt="sm">
        先选择一个文件夹作为工作区，小熊猫会在这里与你处理文件和任务。
      </Text>
      <Button onClick={open} variant="light">
        创建工作区
      </Button>
      <CreateWorkspaceModal onClose={close} opened={opened} />
    </ConversationWelcome>
  );
}
