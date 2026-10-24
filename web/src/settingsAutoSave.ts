import { api } from "./api";
import type { AppSettings } from "./types";

/** 改动即存：连点几个输入框只打一次接口，不要每敲一个字写一次盘。 */
export const SAVE_DEBOUNCE_MS = 500;

/** trade 是下单框按市场记住的杠杆（settings.trade.leverage）。 */
export type SettingsSection = "ai_auto" | "exec" | "trade";

/** 存盘的回执：失败要说一声，成功要能把那条提示收回去。 */
export interface SaverHandlers {
  onError?: (error: unknown) => void;
  onSaved?: () => void;
}

/** 一段设置攒 500ms 再写盘。同一段只排一个定时器，后来的改动覆盖前面的。
 *
 * 以前失败是 catch {} 吞掉的：后端 400 / 落盘失败时界面一个字都不说，用户看到
 * 的只是「我明明改了，一刷新又回去了」——杠杆那一栏正是这么被当成「设不了」的。
 */
export function createSectionSaver(section: SettingsSection, handlers: SaverHandlers = {}) {
  let timer: number | null = null;
  let pending: Record<string, unknown> | null = null;
  /** 有改动还没落盘（在攒、在写、或上次写失败了）。 */
  let unsaved = false;

  const flush = async () => {
    timer = null;
    const body = pending;
    pending = null;
    if (!body) return true;
    try {
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ [section]: body }),
      });
      unsaved = false;
      handlers.onSaved?.();
      return true;
    } catch (error) {
      // 这次没存上：保持 unsaved，下一次改动还会再写一遍；界面也该知道
      // 服务器上那份还是旧的，别拿它盖掉用户刚敲的数。
      handlers.onError?.(error);
      return false;
    }
  };

  return {
    /** 攒着，500ms 后一起写。 */
    queue(patch: Record<string, unknown>) {
      unsaved = true;
      pending = { ...(pending ?? {}), ...patch };
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(() => void flush(), SAVE_DEBOUNCE_MS);
    },
    /** 离开页面时把还没写的补上。 */
    flush() {
      if (timer !== null) window.clearTimeout(timer);
      void flush();
    },
    /** 恢复默认之前把待写的丢掉，否则旧值会在重置之后又写回去。 */
    cancel() {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      pending = null;
      unsaved = false;
    },
    /** 有改动没落盘：重读服务器值时要避开用户正在改的那一项。 */
    get pending(): boolean {
      return unsaved;
    },
  };
}

/** 恢复默认只重置点名的这一段；网络、市场、主题这些顶层设置后端也不重置。
 *  回执是 `{ok, section, settings}`（和 GET /api/settings 同样的包一层），
 *  直接取 `ai_auto` 会拿到 undefined，界面就会退回前端兜底值——看着对，其实没读后端。 */
export async function resetSection(section: SettingsSection): Promise<AppSettings> {
  const result = await api<{ settings?: AppSettings } & Partial<AppSettings>>(
    "/api/settings/reset",
    { method: "POST", body: JSON.stringify({ section }) },
  );
  return (result.settings ?? result) as AppSettings;
}
