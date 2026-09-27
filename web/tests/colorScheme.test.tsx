import { MantineProvider } from "@mantine/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ColorSchemeSwitcher } from "../src/app/ColorSchemeSwitcher";
import { theme } from "../src/app/theme";

beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.removeItem("mantine-color-scheme-value");
  document.documentElement.removeAttribute("data-mantine-color-scheme");
});

it("右上角外观菜单可以把界面切到亮色", () => {
  render(
    <MantineProvider defaultColorScheme="dark" env="test" theme={theme}>
      <ColorSchemeSwitcher />
    </MantineProvider>,
  );
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe(
    "dark",
  );
  fireEvent.click(screen.getByRole("button", { name: "外观" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "亮色" }));
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe(
    "light",
  );
});
