import type { ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import { Layout, NAV } from "./components/Layout";
import { ServersPage } from "./pages/ServersPage";
import { ToolsPage } from "./pages/ToolsPage";
import { DuplicatesPage } from "./pages/DuplicatesPage";
import { LensPage } from "./pages/LensPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { HealthPage } from "./pages/HealthPage";
import { ExecutionsPage } from "./pages/ExecutionsPage";
import { PolicyPage } from "./pages/PolicyPage";
import { PlaygroundPage } from "./pages/PlaygroundPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";

const PAGES: Record<string, ReactNode> = {
  "/servers": <ServersPage />,
  "/tools": <ToolsPage />,
  "/duplicates": <DuplicatesPage />,
  "/lens": <LensPage />,
  "/analytics": <AnalyticsPage />,
  "/health": <HealthPage />,
  "/executions": <ExecutionsPage />,
  "/policy": <PolicyPage />,
  "/playground": <PlaygroundPage />,
  "/approvals": <ApprovalsPage />,
};

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/servers" replace /> },
      // The free-text-identity simulator was replaced by the agent lens.
      { path: "simulator", element: <Navigate to="/lens" replace /> },
      ...NAV.map((n) => ({ path: n.to.slice(1), element: PAGES[n.to] })),
      { path: "*", element: <Navigate to="/servers" replace /> },
    ],
  },
]);
