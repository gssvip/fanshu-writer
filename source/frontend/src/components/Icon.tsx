import type { CSSProperties } from 'react';

/**
 * 统一矢量图标组件。
 * - 全部基于 24x24 viewBox，stroke="currentColor"，通过 color 控制配色，天然支持主题换肤。
 * - 替代全站 emoji 图标，解决跨平台字形漂移与对齐问题。
 * - 用法：<Icon name="home" size={20} /> 或 <Icon name="search" className="foo" />
 */
export type IconName =
  | 'home' | 'edit' | 'tools' | 'user' | 'users'
  | 'bulb' | 'settings' | 'clipboard' | 'book' | 'book-open' | 'library'
  | 'backpack' | 'folder' | 'folder-open' | 'crystal' | 'map'
  | 'search' | 'bot' | 'brain' | 'building'
  | 'package' | 'bar-chart' | 'trending-up' | 'chart-pie'
  | 'chevron-down' | 'chevron-up' | 'plus' | 'minus' | 'x' | 'check' | 'check-circle' | 'x-circle' | 'arrow-left'
  | 'globe' | 'receipt' | 'save' | 'palette' | 'lock' | 'key' | 'ban' | 'tag' | 'clock' | 'hourglass'
  | 'refresh' | 'plug' | 'pin' | 'notepad' | 'inbox' | 'upload' | 'cloud' | 'smartphone'
  | 'chair' | 'target' | 'flame' | 'brick' | 'mic' | 'broom'
  | 'message-circle' | 'party' | 'hook' | 'diamond' | 'shield' | 'ruler' | 'memo'
  | 'warning' | 'sparkles' | 'bomb' | 'fishing' | 'archive' | 'speaker' | 'star' | 'file-text' | 'info' | 'trash'
  | 'rocket' | 'scale' | 'puzzle' | 'wand' | 'paperclip' | 'help-circle' | 'wrench' | 'link' | 'check-square' | 'zap' | 'sword' | 'paw' | 'calendar' | 'thermometer' | 'tomato' | 'castle' | 'stop' | 'sun' | 'moon' | 'crown' | 'download' | 'upload-cloud' | 'lightbulb' | 'edit-pencil' | 'trash-2' | 'folder-closed' | 'zip' | 'key-2' | 'heart' | 'drama' | 'circle' | 'chevron-right' | 'chevron-left';

const PATHS: Record<IconName, JSX.Element> = {
  home: (<><path d="M4 10.5 12 4l8 6.5" /><path d="M6 9.8V19a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V9.8" /><path d="M10 20v-5h4v5" /></>),
  edit: (<><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>),
  tools: (<><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2.3-.7-.7-2.3 2.3-2.3z" /></>),
  user: (<><circle cx="12" cy="8" r="3.6" /><path d="M5.5 19.6c1.2-2.7 3.7-4.1 6.5-4.1s5.3 1.4 6.5 4.1" /></>),
  users: (<><circle cx="9" cy="8" r="3.2" /><path d="M2.5 19c1-2.4 3-3.6 6.5-3.6s5.5 1.2 6.5 3.6" /><circle cx="17" cy="9" r="2.6" /><path d="M16 15.4c2.8.2 4.8 1.4 5.5 3.6" /></>),
  bulb: (<><path d="M9 18h6" /><path d="M10 21h4" /><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.1 2.2h5c0-.8.4-1.6 1.1-2.2A6 6 0 0 0 12 3z" /></>),
  settings: (<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>),
  clipboard: (<><rect x="6" y="4" width="12" height="17" rx="2" /><path d="M9 4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2" /><path d="M9 11h6M9 15h6M9 19h4" /></>),
  book: (<><path d="M4 5a2 2 0 0 1 2-2h11a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H6a2 2 0 0 1-2-2V5z" /><path d="M8 3v17" /><path d="M12 8h4M12 12h4" /></>),
  'book-open': (<><path d="M12 7v14" /><path d="M3 5a1 1 0 0 1 1-1h5a3 3 0 0 1 3 3v13a2 2 0 0 0-2-2H4a1 1 0 0 1-1-1V5z" /><path d="M21 5a1 1 0 0 0-1-1h-5a3 3 0 0 0-3 3v13a2 2 0 0 1 2-2h6a1 1 0 0 0 1-1V5z" /></>),
  library: (<><path d="M4 4h4v16H4z" /><path d="M9 4h4v16H9z" /><path d="M14.5 4.5 19 4l.6 15.5-4.2.5z" /></>),
  backpack: (<><path d="M7 8V6a5 5 0 0 1 10 0v2" /><rect x="5" y="8" width="14" height="13" rx="2" /><path d="M10 8h4" /><path d="M9 14h6M9 18h6" /></>),
  folder: (<><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" /></>),
  'folder-open': (<><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H3V7z" /><path d="M3 10h18l-2 7a2 2 0 0 1-2 1.6H5A2 2 0 0 1 3 17l-0-7z" /></>),
  crystal: (<><path d="M12 3 5 10l7 11 7-11-7-7z" /><path d="M5 10h14" /><path d="M12 3v18" /></>),
  map: (<><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z" /><path d="M9 4v14" /><path d="M15 6v14" /></>),
  search: (<><circle cx="11" cy="11" r="6" /><path d="m15.5 15.5 4.5 4.5" /></>),
  bot: (<><rect x="5" y="8" width="14" height="11" rx="2" /><path d="M12 5v3" /><circle cx="12" cy="4" r="1" /><circle cx="9.5" cy="13" r="1" /><circle cx="14.5" cy="13" r="1" /><path d="M2 13h3M19 13h3" /></>),
  brain: (<><path d="M9.5 4.5a2.5 2.5 0 0 0-2.5 2.5 2.5 2.5 0 0 0-1 4.8 2.5 2.5 0 0 0 1 4.7A2.5 2.5 0 0 0 9.5 19a2.5 2.5 0 0 0 2.5-2.5V7A2.5 2.5 0 0 0 9.5 4.5z" /><path d="M14.5 4.5A2.5 2.5 0 0 1 17 7a2.5 2.5 0 0 1 1 4.8 2.5 2.5 0 0 1-1 4.7A2.5 2.5 0 0 1 14.5 19 2.5 2.5 0 0 1 12 16.5" /></>),
  building: (<><rect x="5" y="3" width="14" height="18" rx="1" /><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2" /><path d="M10 21v-3h4v3" /></>),
  package: (<><path d="M21 8 12 3 3 8v8l9 5 9-5V8z" /><path d="M3 8l9 5 9-5" /><path d="M12 13v8" /></>),
  'bar-chart': (<><path d="M4 20V10" /><path d="M10 20V4" /><path d="M16 20v-7" /><path d="M22 20H2" /></>),
  'trending-up': (<><path d="M3 17 9 11l4 4 8-8" /><path d="M15 7h6v6" /></>),
  'chart-pie': (<><path d="M12 3a9 9 0 1 0 9 9h-9V3z" /><path d="M12 3v9h9" /></>),
  'chevron-down': <path d="m6 9 6 6 6-6" />,
  'chevron-up': <path d="m6 15 6-6 6 6" />,
  'chevron-right': <path d="m9 6 6 6-6 6" />,
  'chevron-left': <path d="m15 6-6 6 6 6" />,
  plus: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  check: <path d="M5 12.5 10 17l9-10" />,
  'check-circle': (<><circle cx="12" cy="12" r="9" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>),
  'x-circle': (<><circle cx="12" cy="12" r="9" /><path d="m9 9 6 6M15 9l-6 6" /></>),
  'arrow-left': <path d="M19 12H5M12 19l-7-7 7-7" />,
  globe: (<><circle cx="12" cy="12" r="9" /><path d="M3 12h18" /><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z" /></>),
  receipt: (<><path d="M5 3h14v18l-3-2-3 2-3-2-3 2-2-2V3z" /><path d="M8 8h8M8 12h8M8 16h5" /></>),
  save: (<><path d="M5 3h11l3 3v15a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M8 3v5h7V3" /><path d="M8 14h8v6H8z" /></>),
  palette: (<><circle cx="12" cy="12" r="9" /><circle cx="8" cy="10" r="1.2" fill="currentColor" /><circle cx="12" cy="7.5" r="1.2" fill="currentColor" /><circle cx="16" cy="10" r="1.2" fill="currentColor" /><circle cx="15.5" cy="14.5" r="1.2" fill="currentColor" /></>),
  lock: (<><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></>),
  key: (<><circle cx="8" cy="15" r="4" /><path d="M11 13l9-9" /><path d="M16 6l2 2M19 3l2 2" /></>),
  ban: (<><circle cx="12" cy="12" r="9" /><path d="m6 6 12 12" /></>),
  tag: (<><path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9-9-9z" /><circle cx="8" cy="8" r="1.5" fill="currentColor" /></>),
  clock: (<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>),
  hourglass: (<><path d="M6 3h12M6 21h12" /><path d="M6 3v4a6 6 0 0 0 12 0V3" /><path d="M6 21v-4a6 6 0 0 1 12 0v4" /></>),
  refresh: (<><path d="M21 12a9 9 0 1 1-2.6-6.4" /><path d="M21 4v4h-4" /></>),
  plug: (<><path d="M9 3v5M15 3v5" /><path d="M6 8h12v3a6 6 0 0 1-12 0V8z" /><path d="M12 17v4" /></>),
  pin: (<><path d="M12 2l2 5 5 2-5 2-2 5-2-5-5-2 5-2 2-5z" /></>),
  notepad: (<><path d="M5 3h11l3 3v15a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M8 8h8M8 12h8M8 16h5" /></>),
  inbox: (<><path d="M3 13h5l2 3h4l2-3h5" /><path d="M3 13v6a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6" /><path d="M5 5h14v8" /></>),
  upload: (<><path d="M12 3v12" /><path d="m7 8 5-5 5 5" /><path d="M5 17h14" /></>),
  cloud: (<><path d="M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.5-1.5A3.5 3.5 0 0 1 17 18H7z" /></>),
  smartphone: (<><rect x="7" y="3" width="10" height="18" rx="2" /><path d="M11 18h2" /></>),
  chair: (<><path d="M7 5h10v6a3 3 0 0 1-3 3h-4a3 3 0 0 1-3-3V5z" /><path d="M7 14v6M17 14v6M9 11h6" /></>),
  target: (<><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1.5" fill="currentColor" /></>),
  flame: (<><path d="M12 3c1 3 4 4 4 8a4 4 0 1 1-8 0c0-2 1-3 2-4 0 2 1 3 2 3 1-2 0-4 0-7z" /></>),
  brick: (<><rect x="3" y="4" width="18" height="16" rx="1" /><path d="M3 10h18M9 4v6M15 10v6M6 16h6M15 16h3" /></>),
  mic: (<><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0" /><path d="M12 18v3" /></>),
  broom: (<><path d="m14 4 6 6-4 4-6-6 4-4z" /><path d="M10 8 4 14l3 3 6-6" /><path d="M7 17c-2 1-3 3-3 4 2 0 4-1 5-2" /></>),
  'message-circle': (<><path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12z" /></>),
  party: (<><path d="M12 3v2M12 19v2M3 12h2M19 12h2" /><circle cx="12" cy="12" r="6" /><path d="m8 12 3 2 5-4" /></>),
  hook: (<><path d="M15 4a3 3 0 0 0-3 3v8" /><path d="M12 15a3 3 0 1 1-6 0c0-2 2-3 3-5" /></>),
  diamond: <path d="M12 3l9 9-9 9-9-9 9-9z" />,
  shield: (<><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z" /><path d="m9 12 2 2 4-4" /></>),
  ruler: (<><path d="M4 14 14 4l6 6L10 20z" /><path d="M7 11l2 2M10 8l2 2M13 5l2 2M8 16l2 2" /></>),
  memo: (<><path d="M5 3h11l3 3v15a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M8 8h8M8 12h8M8 16h5" /></>),
  warning: (<><path d="M12 3 2 20h20L12 3z" /><path d="M12 10v4" /><circle cx="12" cy="17" r="1" fill="currentColor" /></>),
  sparkles: (<><path d="M12 4l1.5 4.5L18 10l-4.5 1.5L12 16l-1.5-4.5L6 10l4.5-1.5L12 4z" /><path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z" /></>),
  bomb: (<><circle cx="11" cy="14" r="7" /><path d="M17 7l3-3" /><path d="M18 4l1-1" /><path d="M15 8c1-1 2-1 3 0" /></>),
  fishing: (<><path d="M5 5l14 14" /><path d="M16 8a3 3 0 1 1 6 0" /><path d="M19 11v8" /><circle cx="19" cy="20" r="1" fill="currentColor" /></>),
  archive: (<><rect x="3" y="4" width="18" height="4" rx="1" /><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" /><path d="M10 12h4" /></>),
  speaker: (<><path d="M4 10v4h3l5 4V6L7 10H4z" /><path d="M16 9a3 3 0 0 1 0 6" /></>),
  star: (<><path d="M12 3l2.7 5.8 6.3.6-4.7 4.2 1.4 6.1L12 17l-5.7 2.7 1.4-6.1L3 9.4l6.3-.6L12 3z" /></>),
  'file-text': (<><path d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" /><path d="M14 3v4h4" /><path d="M9 13h6M9 17h6" /></>),
  info: (<><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><circle cx="12" cy="8" r="1" fill="currentColor" /></>),
  trash: (<><path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" /><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /></>),
  rocket: (<><path d="M5 15c-1.5 1-2 4-2 4s3-.5 4-2c.6-.9.4-2-.4-2.6-.8-.6-1.7-.4-1.6.6z" /><path d="M9 12a14 14 0 0 1 8-8c2 0 3 1 3 3a14 14 0 0 1-8 8l-3-3z" /><circle cx="15" cy="9" r="1.4" /><path d="M12 15l-3 3" /></>),
  scale: (<><path d="M12 4v16" /><path d="M5 7h14" /><path d="M5 7l-3 6a3 3 0 0 0 6 0L5 7z" /><path d="M19 7l-3 6a3 3 0 0 0 6 0l-3-6z" /><path d="M8 21h8" /></>),
  puzzle: (<><path d="M10 4a2 2 0 0 1 4 0v2h4a1 1 0 0 1 1 1v3a2 2 0 1 0 0 4v3a1 1 0 0 1-1 1h-4v-1a2 2 0 1 0-4 0v1H6a1 1 0 0 1-1-1v-3a2 2 0 1 0 0-4V7a1 1 0 0 1 1-1h4V4z" /></>),
  wand: (<><path d="M15 4V2M15 10V8M9 4H7M19 10h-2M17.8 6.2 19 5M12 8l1.4 1.4M5 19l9-9 2 2-9 9H5v-2z" /></>),
  paperclip: (<><path d="M20 12l-7 7a4.5 4.5 0 0 1-6.4-6.4l8-8a3 3 0 0 1 4.3 4.3l-8 8a1.5 1.5 0 0 1-2.1-2.1l7-7" /></>),
  'help-circle': (<><circle cx="12" cy="12" r="9" /><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7" /><circle cx="12" cy="17" r=".6" /></>),
  wrench: (<><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2.3-.7-.7-2.3 2.3-2.3z" /></>),
  link: (<><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5" /><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5" /></>),
  'check-square': (<><rect x="3" y="3" width="18" height="18" rx="3" /><path d="m8 12 3 3 5-6" /></>),
  zap: (<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" />),
  sword: (<><path d="M14.5 4.5 19 9l-9 9-2-2 9-9z" /><path d="M11 7l6 6" /><path d="m5 19 3-3" /><path d="m4 20 1-1 2 2-1 1z" /></>),
  paw: (<><circle cx="6" cy="11" r="1.6" /><circle cx="10" cy="7" r="1.6" /><circle cx="14" cy="7" r="1.6" /><circle cx="18" cy="11" r="1.6" /><path d="M7.5 15c0-2 2-3 4.5-3s4.5 1 4.5 3c0 1.8-1.7 3-4.5 3s-4.5-1.2-4.5-3z" /></>),
  calendar: (<><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M3 9h18" /><path d="M8 3v4" /><path d="M16 3v4" /></>),
  thermometer: (<><path d="M14 14.76V5a2 2 0 0 0-4 0v9.76a4 4 0 1 0 4 0z" /></>),
  tomato: (<><path d="M12 7c-3.5 0-6 2.5-6 6.5S8.5 20 12 20s6-2.5 6-6.5S15.5 7 12 7z" /><path d="M12 7c0-1.5 1-3 3-3" /><path d="M12 7c0-1.5-1-3-3-3" /><path d="M9 4c1.5 1 4.5 1 6 0" /></>),
  castle: (<><path d="M3 21V9l2-2 2 2V7l2-2 2 2v2l2-2 2 2v2l2-2 2 2v12" /><path d="M3 21h18" /><path d="M9 21v-6h6v6" /><path d="M9 11h.01M15 11h.01" /></>),
  stop: (<><rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none" /></>),
  sun: (<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>),
  moon: (<><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></>),
  crown: (<><path d="M3 17l2-9 4 4 3-6 3 6 4-4 2 9H3z" /><path d="M3 21h18" /></>),
  download: (<><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>),
  'upload-cloud': (<><path d="M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.5-1.5A3.5 3.5 0 0 1 17 18H7z" /><path d="M12 11v6" /><path d="m9 14 3-3 3 3" /></>),
  lightbulb: (<><path d="M9 18h6" /><path d="M10 21h4" /><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.4 1.1 2.2h5c0-.8.4-1.6 1.1-2.2A6 6 0 0 0 12 3z" /></>),
  'edit-pencil': (<><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></>),
  'trash-2': (<><path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" /><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" /></>),
  'folder-closed': (<><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" /></>),
  zip: (<><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" /><path d="M14 3v6h6" /><path d="M10 12v2M10 16v2M10 20v1" /></>),
  'key-2': (<><circle cx="8" cy="15" r="4" /><path d="M11 13l9-9" /><path d="M16 6l2 2M19 3l2 2" /></>),
  heart: (<><path d="M12 20s-7-4.5-9.5-9C1 8 2.5 5 5.5 5c1.8 0 3 1 3.5 2 .5-1 1.7-2 3.5-2 3 0 4.5 3 3 6-2.5 4.5-9.5 9-9.5 9z" /></>),
  drama: (<><path d="M10 4a4 4 0 0 0-4 4v1a3 3 0 0 0-2 5 3 3 0 0 0 3 3 3 3 0 0 0 3-3V8a4 4 0 0 0 0-4z" /><path d="M14 4a4 4 0 0 1 4 4v1a3 3 0 0 1 2 5 3 3 0 0 1-3 3 3 3 0 0 1-3-3V8a4 4 0 0 1 0-4z" /></>),
  circle: (<><circle cx="12" cy="12" r="9" /></>),
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
      style={{ flexShrink: 0, display: 'inline-block', verticalAlign: 'middle', ...style }}
      aria-hidden="true"
    >
      {PATHS[name]}
    </svg>
  );
}
