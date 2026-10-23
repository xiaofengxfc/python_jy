import { api } from "./api";
import type { AppSettings } from "./types";

/** 改动即存：连点几个输入框只打一次接口，不要每敲一个字写一次盘。 */
export const SAVE_DEBOUNCE_MS = 500;

export type SettingsSection = "ai_auto" | "exec";

/** 一段设置攒 500ms 再写盘。同一段只排一个定时器，后来的改动覆盖前面的。
 *  存不上不抛：这些参数是运行配置，写盘失败不该把正在填的表单打断。 */
export function createSectionSaver(section: SettingsSection) {
  let timer: number | null = null;
  let pending: Record<string, unknown> | null = null;

  const flush = async () => {
    timer = null;
    const body = pending;
    pending = null;
    if (!body) return;
    try {
      await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ [section]: body }),
      });
    } catch {
      // 这次没存上，下一次改动还会再写一遍。
    }
  };

  return {
    /** 攒着，500ms 后一起写。 */
    queue(patch: Record<string, unknown>) {
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
