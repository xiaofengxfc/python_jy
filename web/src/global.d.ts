/*
 * The Electron preload bridge. Absent in a normal browser, where the title bar
 * renders nothing and the OS frame does the job instead.
 */
interface Window {
  bitrader?: {
    platform: string;
    minimize: () => Promise<void>;
    toggleMaximize: () => Promise<boolean>;
    close: () => Promise<void>;
  };
}
