/*
 * Desktop-only title bar.
 *
 * Electron runs the window without an OS frame, so dragging and the three
 * window commands have to be drawn here. In a plain browser there is no
 * `window.bitrader` bridge and the component renders nothing, which keeps the
 * web build byte-for-byte the same shell it always was.
 */

const ICON = {
  minimize: <path d="M3 8.5h10" />,
  maximize: <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" />,
  close: <path d="M4 4l8 8M12 4l-8 8" />,
};

export default function TitleBar() {
  const bridge = window.bitrader;
  if (!bridge) return null;

  return (
    <header className="titlebar">
      <span className="titlebar-brand">biTrader</span>

      <div className="titlebar-controls">
        <button
          type="button"
          aria-label="最小化"
          onClick={() => void bridge.minimize()}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
            {ICON.minimize}
          </svg>
        </button>
        <button
          type="button"
          aria-label="最大化或还原"
          onClick={() => void bridge.toggleMaximize()}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round">
            {ICON.maximize}
          </svg>
        </button>
        <button
          type="button"
          className="danger"
          aria-label="关闭"
          onClick={() => void bridge.close()}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
            {ICON.close}
          </svg>
        </button>
      </div>
    </header>
  );
}
