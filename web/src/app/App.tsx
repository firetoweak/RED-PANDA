import { ActionIcon, AppShell } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconMenu2 } from "@tabler/icons-react";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { Route, Routes } from "react-router-dom";

import { useAppDispatch } from "./hooks";
import {
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
  clampSidebarWidth,
  readSidebarWidth,
  writeSidebarWidth,
} from "./sidebarWidth";
import { SessionConversation } from "../features/conversation/SessionConversation";
import { ModelSettingsPage } from "../features/models/ModelSettingsPage";
import { DraftRedirect } from "../features/sessions/DraftRedirect";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { openEventBridge } from "../realtime/eventBridge";

export function App() {
  const dispatch = useAppDispatch();
  const [navigationOpened, { close: closeNavigation, toggle: toggleNavigation }] =
    useDisclosure(false);
  const [sidebarWidth, setSidebarWidth] = useState(readSidebarWidth);
  const [resizingSidebar, setResizingSidebar] = useState(false);
  const sidebarWidthRef = useRef(sidebarWidth);
  const resizingSidebarRef = useRef(false);
  sidebarWidthRef.current = sidebarWidth;

  useEffect(() => openEventBridge(dispatch), [dispatch]);

  function beginSidebarResize(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) {
      return;
    }
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // 没有真实 pointer 时仍按拖动手势处理
    }
    resizingSidebarRef.current = true;
    setResizingSidebar(true);
    document.body.classList.add("is-resizing-sidebar");
  }

  function moveSidebarResize(event: PointerEvent<HTMLDivElement>) {
    if (!resizingSidebarRef.current) {
      return;
    }
    const next = clampSidebarWidth(event.clientX);
    sidebarWidthRef.current = next;
    setSidebarWidth(next);
  }

  function endSidebarResize() {
    if (!resizingSidebarRef.current) {
      return;
    }
    resizingSidebarRef.current = false;
    setResizingSidebar(false);
    document.body.classList.remove("is-resizing-sidebar");
    writeSidebarWidth(sidebarWidthRef.current);
  }

  return (
    <AppShell
      className="app-shell"
      navbar={{
        width: sidebarWidth,
        breakpoint: "sm",
        collapsed: { mobile: !navigationOpened },
      }}
      padding={0}
      transitionDuration={resizingSidebar ? 0 : 180}
    >
      <AppShell.Navbar className="app-navbar" p="md">
        <SessionSidebar onNavigate={closeNavigation} />
        <div
          aria-label="调节侧栏宽度"
          aria-orientation="vertical"
          aria-valuemax={MAX_SIDEBAR_WIDTH}
          aria-valuemin={MIN_SIDEBAR_WIDTH}
          aria-valuenow={sidebarWidth}
          className={
            resizingSidebar ? "sidebar-resize is-dragging" : "sidebar-resize"
          }
          onPointerCancel={endSidebarResize}
          onPointerDown={beginSidebarResize}
          onPointerMove={moveSidebarResize}
          onPointerUp={endSidebarResize}
          role="separator"
        />
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
        <Routes>
          <Route path="/settings/models" element={<ModelSettingsPage />} />
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
