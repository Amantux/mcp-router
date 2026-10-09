import { describe, expect, it } from "vitest";
import { stripBidi } from "./bidi";

describe("stripBidi", () => {
  it("removes every override and isolate control, keeps the rest", () => {
    const all = "‪‫‬‭‮⁦⁧⁨⁩";
    expect(stripBidi(`ok${all}ay`)).toBe("okay");
    expect(stripBidi("admin‮gnp.exe")).toBe("admingnp.exe");
  });

  it("leaves ordinary text, including RTL letters and LRM, untouched", () => {
    expect(stripBidi("שלום ‎ hello")).toBe("שלום ‎ hello");
  });
});
