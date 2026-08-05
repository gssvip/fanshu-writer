// 科技感跑车 Logo（SVG 自绘，霓虹蓝紫色调，流线型车身 + 速度线）
// 用法：<CarLogo size={24} /> 或 <CarLogo />（默认 24px）
interface CarLogoProps {
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}

export default function CarLogo({ size = 24, className, style }: CarLogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      style={style}
      aria-label="AI 智驾"
    >
      <defs>
        <linearGradient id="carBody" x1="8" y1="20" x2="56" y2="48" gradientUnits="userSpaceOnUse">
          <stop stopColor="#22d3ee" />
          <stop offset="0.5" stopColor="#6366f1" />
          <stop offset="1" stopColor="#a855f7" />
        </linearGradient>
        <linearGradient id="carGlass" x1="22" y1="20" x2="46" y2="32" gradientUnits="userSpaceOnUse">
          <stop stopColor="#e0f2fe" />
          <stop offset="1" stopColor="#7dd3fc" />
        </linearGradient>
        <filter id="carGlow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="1.2" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* 速度尾迹 */}
      <g stroke="#22d3ee" strokeWidth="1.5" strokeLinecap="round" opacity="0.55">
        <line x1="4" y1="38" x2="12" y2="38" />
        <line x1="2" y1="42" x2="10" y2="42" opacity="0.7" />
        <line x1="6" y1="46" x2="12" y2="46" opacity="0.4" />
      </g>

      {/* 车身（流线型低趴跑车） */}
      <g filter="url(#carGlow)">
        <path
          d="M14 44 L12 40 C12 38.5 13 37.5 15 37.2 L24 36 C26 32 30 30 34 30 L42 30 C46 30 50 32 52 36 L58 38 C60 38.6 61 39.8 61 41.5 L61 44 C61 45.1 60.1 46 59 46 L55 46"
          stroke="url(#carBody)"
          strokeWidth="2.2"
          strokeLinejoin="round"
          fill="url(#carBody)"
          fillOpacity="0.18"
        />
        {/* 车顶玻璃舱 */}
        <path
          d="M26 35 C27.5 32 30 31 33 31 L41 31 C44 31 46.5 32 48 35 L46 36 L28 36 Z"
          fill="url(#carGlass)"
          opacity="0.9"
        />
        {/* 车身腰线高光 */}
        <path d="M15 40 L58 40" stroke="#e0f2fe" strokeWidth="0.8" opacity="0.6" />
        {/* 前大灯 */}
        <circle cx="57" cy="41" r="1.6" fill="#fef08a" />
        <circle cx="57" cy="41" r="3" fill="#fef08a" opacity="0.35" />
      </g>

      {/* 车轮 */}
      <g>
        <circle cx="22" cy="46" r="5.5" fill="#0f172a" stroke="#22d3ee" strokeWidth="1.8" />
        <circle cx="22" cy="46" r="2.2" fill="#6366f1" />
        <circle cx="22" cy="46" r="0.8" fill="#e0f2fe" />
        <circle cx="50" cy="46" r="5.5" fill="#0f172a" stroke="#22d3ee" strokeWidth="1.8" />
        <circle cx="50" cy="46" r="2.2" fill="#a855f7" />
        <circle cx="50" cy="46" r="0.8" fill="#e0f2fe" />
      </g>

      {/* 底部地面反光线 */}
      <line x1="10" y1="54" x2="58" y2="54" stroke="#6366f1" strokeWidth="0.6" opacity="0.25" />
    </svg>
  );
}
