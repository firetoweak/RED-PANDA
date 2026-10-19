import { createTheme } from "@mantine/core";

export const theme = createTheme({
  primaryColor: "ember",
  primaryShade: { light: 7, dark: 5 },
  colors: {
    dark: ["#ececec", "#cccccc", "#b3b3b3", "#969696", "#686868", "#414141", "#2c2c2c", "#181818", "#141414", "#111111"],
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
    'Inter, "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif',
  fontFamilyMonospace:
    '"JetBrains Mono", "Cascadia Code", Consolas, monospace',
  defaultRadius: "md",
  radius: { xs: "4px", sm: "6px", md: "10px", lg: "14px", xl: "20px" },
  autoContrast: true,
  cursorType: "pointer",
});
