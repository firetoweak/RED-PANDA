import "@mantine/core/styles.css";
import "@mantine/code-highlight/styles.css";
import "@ai-markdown/react-mantine/styles.css";
import "katex/dist/katex.min.css";

import {
  CodeHighlightAdapterProvider,
  createHighlightJsAdapter,
} from "@mantine/code-highlight";
import { MantineProvider } from "@mantine/core";
import hljs from "highlight.js/lib/common";
import { Provider } from "react-redux";
import { BrowserRouter } from "react-router-dom";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { store } from "./app/store";
import { theme } from "./app/theme";
import "./styles/app.css";

const codeHighlightAdapter = createHighlightJsAdapter(hljs);

createRoot(document.getElementById("root")!).render(
  <MantineProvider theme={theme} defaultColorScheme="light">
    <CodeHighlightAdapterProvider adapter={codeHighlightAdapter}>
      <Provider store={store}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </Provider>
    </CodeHighlightAdapterProvider>
  </MantineProvider>,
);
