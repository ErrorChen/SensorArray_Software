import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { defaultSetupProfile } from "../../state/setupProfile";
import { createBackendSnapshot } from "../../testUtils/snapshot";
import { SetupPanel, supportedRowOptions } from "./SetupPanel";

describe("SetupPanel ROWS selector", () => {
  it("offers every integer row geometry from 1 through 8", () => {
    expect(supportedRowOptions).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    const html = renderToStaticMarkup(createElement(SetupPanel, {
      client: null,
      snapshot: createBackendSnapshot(),
      setupProfile: defaultSetupProfile("."),
      onSetupProfileChange: () => undefined,
      onError: () => undefined
    }));
    for (const rows of supportedRowOptions) {
      expect(html).toContain(`<option value="${rows}"`);
    }
    expect(html).toContain("Automatically reconnect the selected physical device");
    expect(html).toContain("Resume measurement configuration after device restart");
    expect(html).toContain("Device default");
    expect(html).toContain("never restored automatically");
  });
});
