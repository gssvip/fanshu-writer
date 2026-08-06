// AI 智驾 Logo（使用 wangbiao.webp 图片）
// 用法：<CarLogo size={24} /> 或 <CarLogo />（默认 24px）
interface CarLogoProps {
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}

export default function CarLogo({ size = 24, className, style }: CarLogoProps) {
  return (
    <img
      src="wangbiao.webp"
      alt="AI 智驾"
      width={size}
      height={size}
      className={className}
      style={{ objectFit: 'contain', display: 'inline-block', ...style }}
      aria-label="AI 智驾"
    />
  );
}
