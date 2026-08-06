// AI 智驾 Logo（使用 ai-zhijia-logo.png 图片）
// 用法：<CarLogo size={24} /> 或 <CarLogo />（默认 24px）
// 图片为 491×589 竖向比例，渲染时按 width=size 自适应等比缩放
interface CarLogoProps {
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}

export default function CarLogo({ size = 24, className, style }: CarLogoProps) {
  return (
    <img
      src="/ai-zhijia-logo.png"
      width={size}
      height={size}
      alt="AI 智驾"
      aria-label="AI 智驾"
      className={`car-logo${className ? ' ' + className : ''}`}
      style={{ objectFit: 'contain', display: 'inline-block', verticalAlign: 'middle', background: 'transparent', ...style }}
      draggable={false}
    />
  );
}
