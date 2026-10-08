import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { FluentProvider, webDarkTheme, webLightTheme } from "@fluentui/react-components";
import { RouterProvider } from "react-router";
import "./index.css";
import { router } from "./routes";
import { useColorScheme } from "./hooks/useColorScheme";
import { NotificationsProvider } from "./components/Notifications";
import { hydrateAuth } from "./api/auth";

hydrateAuth();

function Root() {
  const scheme = useColorScheme();
  return (
    <FluentProvider theme={scheme === "dark" ? webDarkTheme : webLightTheme} style={{ height: "100%" }}>
      <NotificationsProvider>
        <RouterProvider router={router} />
      </NotificationsProvider>
    </FluentProvider>
  );
}

const el = document.getElementById("root");
if (!el) throw new Error("#root missing from index.html");
createRoot(el).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
