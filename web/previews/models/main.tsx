import "@mantine/core/styles.css";
import "../../src/styles/app.css";
import "./preview.css";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";
import { theme } from "../../src/app/theme";
import { ModelPreview } from "./ModelPreview";

createRoot(document.getElementById("root")!).render(
  <MantineProvider theme={theme} defaultColorScheme="dark"><ModelPreview /></MantineProvider>,
);
