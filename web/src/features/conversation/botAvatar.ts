/** 同一身份得到同一轮廓；不依赖显示名、时钟或浏览器存储。 */
export function identityHash(seed: string): number {
  let hash = 0;
  for (const char of seed) hash = (Math.imul(hash, 31) + char.codePointAt(0)!) | 0;
  return hash >>> 0;
}

export function botAvatarPath(seed: string): string {
  const hash = identityHash(seed);
  const variant = (hash >>> 4) % 4;
  const count = [3, 4, 6, 8][variant];
  const rounding = variant < 2 ? 0.11 : 0.23;
  const rotation = ((hash >>> 8) / 0xffffff) * Math.PI * 2;
  const width = 0.84 + ((hash >>> 24) & 15) / 75;
  const points = Array.from({ length: count }, (_, index) => {
    const angle = rotation + index * Math.PI * 2 / count;
    const lobe = variant >= 2 && index % 2 === 1 ? 0.62 : 1;
    const radius = (21 + ((hash >>> ((index * 5) % 24)) & 31) / 12) * lobe;
    return [32 + Math.cos(angle) * radius * width, 32 + Math.sin(angle) * radius];
  });
  const point = (index: number) => points[(index + count) % count];
  const format = (value: number) => value.toFixed(2);
  const [firstX, firstY] = point(0);
  let path = `M${format(firstX)} ${format(firstY)}`;
  for (let index = 0; index < count; index++) {
    const previous = point(index - 1);
    const current = point(index);
    const next = point(index + 1);
    const after = point(index + 2);
    path += `C${format(current[0] + (next[0] - previous[0]) * rounding)} ${format(current[1] + (next[1] - previous[1]) * rounding)} ${format(next[0] - (after[0] - current[0]) * rounding)} ${format(next[1] - (after[1] - current[1]) * rounding)} ${format(next[0])} ${format(next[1])}`;
  }
  return `${path}Z`;
}
