import { useEffect, useState } from 'react';

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  side?: 'left' | 'right';
  width?: number;
}

/**
 * 侧边抽屉：从左/右滑出，带遮罩。
 * 用于移动端项目级导航、书籍切换等。
 */
export default function Drawer({ open, onClose, children, side = 'left', width }: DrawerProps) {
  const [rendered, setRendered] = useState(open);

  useEffect(() => {
    if (open) {
      setRendered(true);
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
      const t = setTimeout(() => setRendered(false), 300);
      return () => clearTimeout(t);
    }
    return () => { document.body.style.overflow = ''; };
  }, [open]);

  if (!rendered) return null;

  const style: React.CSSProperties = {};
  if (width) style.width = width;
  if (side === 'right') {
    style.left = 'auto';
    style.right = 0;
    style.transform = open ? 'translateX(0)' : 'translateX(100%)';
    style.boxShadow = '-4px 0 24px rgba(0,0,0,0.15)';
  }

  return (
    <>
      <div
        className={`drawer-overlay ${open ? 'show' : ''}`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <div
        className={`drawer ${open ? 'show' : ''}`}
        style={style}
        role="dialog"
        aria-modal="true"
        data-side={side}
      >
        {children}
      </div>
    </>
  );
}
