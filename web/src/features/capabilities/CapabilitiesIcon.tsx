import type { SVGProps } from "react";

export function CapabilitiesIcon({ size = 20, ...props }: SVGProps<SVGSVGElement> & { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
    <path d="M19.8 15.2a8.7 8.7 0 1 0-4.2 4.4c2.2 1.4 4.5 1 5.7-.5" />
    <path d="m8.1 10.2 2-2 5.5 5.5-2 2a3.9 3.9 0 0 1-5.5-5.5Z" />
    <path d="m11.5 9.6 2.1-2.1m.6 4.8 2.1-2.1m-8.2 5.5-2 2" />
  </svg>;
}
