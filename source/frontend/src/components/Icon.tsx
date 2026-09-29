import type { CSSProperties } from 'react';

/**
 * 统一矢量图标组件。
 * - 全部基于 24x24 viewBox，stroke="currentColor"，通过 color 控制配色，天然支持主题换肤。
 * - 替代全站 emoji 图标，解决跨平台字形漂移与对齐问题。
 * - 用法：<Icon name="home" size={20} /> 或 <Icon name="search" className="foo" />
 */
export type IconName =
  | 'home' | 'edit' | 'tools' | 'user'
  | 'bulb' | 'settings' | 'clipboard' | 'book-open' | 'users'
  | 'library' | 'backpack' | 'folder' | 'crystal' | 'map'
  | 'search' | 'bot' | 'building'
  | 'package' | 'bar-chart' | 'trending-up'
  | 'chevron-down' | 'chevron-up' | 'plus' | 'minus' | 'x' | 'check' | 'arrow-left';

const PATHS: Record<IconName, JSX.Element> = {
  home: (
    <>
      <path d="M4 10.5 12 4l8 6.5" />
      <path d="M6 9.8V19a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V9.8" />
      <path d="M10 20v-5h4v5" />
    </>
  ),
  edit: (
    <>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </>
  ),
  tools: (
    <>
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2.3-.7-.7-2.3 2.3-2.3z" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3.6" />
      <path d="M5.5 19.6c1.2-2.7 3.7-4.1 6.5-4.1s5.3 1.4 6.5 4.1" />
    </>
  ),
  bulb: (
    <>
      <path d="M9 18h6" />
      <path d="M10 21h4" />
      <path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.1 2.2h5c0-.8.4-1.6 1.1-2.2A6 6 0 0 0 12 3z" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>
  ),
  clipboard: (
    <>
      <rect x="6" y="4" width="12" height="17" rx="2" />
      <path d="M9 4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2" />
      <path d="M9 11h6M9 15h6M9 19h4" />
    </>
  ),
  'book-open': (
    <>
      <path d="M12 7v14" />
      <path d="M3 5a1 1 0 0 1 1-1h5a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H4a1 1 0 0 1-1-1V5z" />
      <path d="M21 5a1 1 0 0 0-1-1h-5a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h6a1 1 0 0 0 1-1V5z" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M2.5 19c1-2.4 3-3.6 6.5-3.6s5.5 1.2 6.5 3.6" />
      <circle cx="17" cy="9" r="2.6" />
      <path d="M16 15.4c2.8.2 4.8 1.4 5.5 3.6" />
    </>
  ),
  library: (
    <>
      <path d="M4 4h4v16H4z" />
      <path d="M9 4h4v16H9z" />
      <path d="M14.5 4.5 19 4l.6 15.5-4.2.5z" />
    </>
  ),
  backpack: (
    <>
      <path d="M7 8V6a5 5 0 0 1 10 0v2" />
      <rect x="5" y="8" width="14" height="13" rx="2" />
      <path d="M10 8h4" />
      <path d="M9 14h6M9 18h6" />
    </>
  ),
  folder: (
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
    </>
  ),
  crystal: (
    <>
      <path d="M12 3 5 10l7 11 7-11-7-7z" />
      <path d="M5 10h14" />
      <path d="M12 3v18" />
    </>
  ),
  map: (
    <>
      <path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z" />
      <path d="M9 4v14" />
      <path d="M15 6v14" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6" />
      <path d="m15.5 15.5 4.5 4.5" />
    </>
  ),
  bot: (
    <>
      <rect x="5" y="8" width="14" height="11" rx="2" />
      <path d="M12 5v3" />
      <circle cx="12" cy="4" r="1" />
      <circle cx="9.5" cy="13" r="1" />
      <circle cx="14.5" cy="13" r="1" />
      <path d="M2 13h3M19 13h3" />
    </>
  ),
  building: (
    <>
      <rect x="5" y="3" width="14" height="18" rx="1" />
      <path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2" />
      <path d="M10 21v-3h4v3" />
    </>
  ),
  package: (
    <>
      <path d="M21 8 12 3 3 8v8l9 5 9-5V8z" />
      <path d="M3 8l9 5 9-5" />
      <path d="M12 13v8" />
    </>
  ),
  'bar-chart': (
    <>
      <path d="M4 20V10" />
      <path d="M10 20V4" />
      <path d="M16 20v-7" />
      <path d="M22 20H2" />
    </>
  ),
  'trending-up': (
    <>
      <path d="M3 17 9 11l4 4 8-8" />
      <path d="M15 7h6v6" />
    </>
  ),
  'chevron-down': <path d="m6 9 6 6 6-6" />,
  'chevron-up': <path d="m6 15 6-6 6 6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  check: <path d="M5 12.5 10 17l9-10" />,
  'arrow-left': <path d="M19 12H5M12 19l-7-7 7-7" />,
};

export default function Icon({
  name,
  size = 20,
  strokeWidth = 1.8,
  className,
  style,
}: {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0, display: 'block', ...style }}
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}
