import { useEffect, useState } from 'react';

interface BottomSheetProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  /** 是否占满更高高度（默认 70vh，full 为 90vh） */
  full?: boolean;
}

/**
 * 底部半屏弹层：从底部滑出，带拖拽手柄与遮罩。
 * 始终挂载以保证进出动画流畅，靠 .show 控制显隐。
 */
export default function BottomSheet({ open, onClose, title, children, full }: BottomSheetProps) {
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

  return (
    <>
      <div
        className={`sheet-overlay ${open ? 'show' : ''}`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <div
        className={`sheet ${open ? 'show' : ''}`}
        style={full ? { height: '90vh', maxHeight: '90vh' } : undefined}
        role="dialog"
        aria-modal="true"
      >
        <div className="sheet-handle" />
        {title && (
          <div className="sheet-header">
            <h3>{title}</h3>
            <button className="btn-ghost-sm" onClick={onClose} aria-label="关闭">✕</button>
          </div>
        )}
        <div className="sheet-body">{children}</div>
      </div>
    </>
  );
}
