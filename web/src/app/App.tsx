import { ActionIcon, AppShell } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconMenu2 } from "@tabler/icons-react";
import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";

import { ColorSchemeSwitcher } from "./ColorSchemeSwitcher";
import { useAppDispatch } from "./hooks";
import { SessionConversation } from "../features/conversation/SessionConversation";
import { DraftRedirect } from "../features/sessions/DraftRedirect";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { openEventBridge } from "../realtime/eventBridge";

export function App() {
  const dispatch = useAppDispatch();
  const [navigationOpened, { close: closeNavigation, toggle: toggleNavigation }] =
    useDisclosure(false);

  useEffect(() => openEventBridge(dispatch), [dispatch]);

  return (
    <AppShell
      className="app-shell"
      navbar={{
        width: 288,
        breakpoint: "sm",
        collapsed: { mobile: !navigationOpened },
      }}
      padding={0}
      transitionDuration={180}
    >
      <AppShell.Navbar className="app-navbar" p="md">
        <SessionSidebar onNavigate={closeNavigation} />
      </AppShell.Navbar>
      <AppShell.Main className="main-pane">
        <ActionIcon
          className="mobile-nav-trigger"
          aria-label="打开会话列表"
          hiddenFrom="sm"
          onClick={toggleNavigation}
          radius="xl"
          size="lg"
          variant="default"
        >
          <IconMenu2 size={19} />
        </ActionIcon>
        <ColorSchemeSwitcher />
        <Routes>
          <Route path="/" element={<DraftRedirect />} />
          <Route
            path="/workspaces/:workspaceId"
            element={<DraftRedirect />}
          />
          <Route path="/sessions/:sessionId" element={<SessionConversation />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  );
}
