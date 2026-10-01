// getRandomValues 在局域网普通 HTTP 页面上也可用。
export function createClientId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
