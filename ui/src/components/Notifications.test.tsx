import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { NotificationsProvider, NotificationStack, useNotify } from "./Notifications";

function Emit({ onReady }: { onReady: (n: ReturnType<typeof useNotify>) => void }) {
  onReady(useNotify());
  return null;
}

describe("Notifications", () => {
  it("renders one stack even when two are mounted", async () => {
    let notify!: ReturnType<typeof useNotify>;
    render(
      <FluentProvider theme={webLightTheme}>
        <NotificationsProvider>
          <NotificationStack />
          <NotificationStack />
          <Emit onReady={(n) => (notify = n)} />
        </NotificationsProvider>
      </FluentProvider>,
    );
    act(() => notify.success("Saved"));
    expect(await screen.findAllByText("Saved")).toHaveLength(1);
  });

  it("clears auto-dismiss timers when the provider unmounts", () => {
    vi.useFakeTimers();
    try {
      const clear = vi.spyOn(window, "clearTimeout");
      let notify!: ReturnType<typeof useNotify>;
      const r = render(
        <NotificationsProvider>
          <Emit onReady={(n) => (notify = n)} />
        </NotificationsProvider>,
      );
      act(() => notify.success("Saved"));
      const before = clear.mock.calls.length;
      r.unmount();
      expect(clear.mock.calls.length).toBe(before + 1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
