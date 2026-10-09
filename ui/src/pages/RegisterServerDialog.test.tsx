import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { callsTo, mockFetch, renderWithProviders } from "../test/render";
import { RegisterServerDialog, validateHttpUrl } from "./RegisterServerDialog";

describe("validateHttpUrl", () => {
  it("accepts http(s) only, and https only when asked", () => {
    expect(validateHttpUrl("")).toBe("Enter the server's URL.");
    expect(validateHttpUrl("mcp.example.com")).toMatch(/full URL/);
    expect(validateHttpUrl("ftp://x.example/mcp")).toMatch(/Only http:\/\/ and https:\/\//);
    expect(validateHttpUrl("http://x.example/mcp")).toBeNull();
    expect(validateHttpUrl("http://x.example/mcp", true)).toMatch(/Only https:\/\//);
    expect(validateHttpUrl("https://x.example/mcp", true)).toBeNull();
  });
});

describe("RegisterServerDialog", () => {
  it("validates per transport, posts an http endpoint, and keeps the dialog open on a 409", async () => {
    const user = userEvent.setup();
    let status = 409;
    const { calls } = mockFetch({ "POST /api/v1/servers": (b) => (status === 409 ? { status } : { json: { id: "s9", ...(b as object) } }) });
    const onRegistered = vi.fn();
    const onClose = vi.fn();
    renderWithProviders(<RegisterServerDialog open onClose={onClose} onRegistered={onRegistered} />);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Register server" }));
    expect(within(dialog).getByText("Enter a name for this server.")).toBeTruthy();
    expect(within(dialog).getByText(/Enter the executable to launch/)).toBeTruthy();
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "web");
    await user.click(within(dialog).getByRole("radio", { name: "Streamable HTTP" }));
    await user.type(within(dialog).getByRole("textbox", { name: /URL/ }), "http://mcp.local/mcp");
    await user.click(within(dialog).getByRole("button", { name: "Register server" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/v1/servers")).toHaveLength(1));
    expect(calls[0].body).toEqual({ name: "web", transport: "streamable-http", endpoint: "http://mcp.local/mcp" });
    expect(await screen.findByText(/Register server “web” failed \(HTTP 409\)/)).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    status = 200;
    await user.click(within(dialog).getByRole("button", { name: "Register server" }));
    await waitFor(() => expect(onRegistered).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Cancel clears the input and errors for the next open", async () => {
    const user = userEvent.setup();
    mockFetch({});
    let open = true;
    const r = renderWithProviders(<RegisterServerDialog open onClose={() => (open = false)} onRegistered={() => {}} />);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Command/ }), "npx");
    await user.click(within(dialog).getByRole("button", { name: "Register server" }));
    expect(within(dialog).getByText("Enter a name for this server.")).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(open).toBe(false);
    // The component stays mounted (the page keeps it): its state must already be blank.
    expect(within(dialog).queryByText("Enter a name for this server.")).toBeNull();
    expect((within(dialog).getByRole("textbox", { name: /Command/ }) as HTMLInputElement).value).toBe("");
    r.unmount();
  });
});
