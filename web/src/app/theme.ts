import { createTheme, Modal } from "@mantine/core";

export const theme = createTheme({
  primaryColor: "ember",
  primaryShade: { light: 7, dark: 5 },
  colors: {
    dark: ["#e4e5e9", "#cbd0da", "#a0a6b2", "#969eac", "#626a78", "#3a3e48", "#282b32", "#202227", "#1c1e23", "#191b20"],
    ember: [
      "#fff3ec",
      "#ffe4d4",
      "#ffc6a9",
      "#ffa078",
      "#f77b4c",
      "#ed6334",
      "#d94f25",
      "#b93e1d",
      "#963419",
      "#782b18",
    ],
  },
  fontFamily:
    '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif',
  fontFamilyMonospace:
    '"JetBrains Mono", "Cascadia Code", Consolas, monospace',
  defaultRadius: "md",
  radius: { xs: "4px", sm: "6px", md: "10px", lg: "14px", xl: "20px" },
  autoContrast: true,
  cursorType: "pointer",
  components: {
    Modal: Modal.extend({
      defaultProps: { centered: true, size: 460, padding: 22, radius: "lg", zIndex: 300 },
      styles: {
        header: { paddingTop: 20, paddingBottom: 16 },
        title: { fontSize: 17, fontWeight: 650 },
      },
    }),
  },
});
