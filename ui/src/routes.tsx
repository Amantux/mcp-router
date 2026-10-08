import type { ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import { Layout, NAV } from "./components/Layout";
import { ServersPage } from "./pages/ServersPage";
import { ToolsPage } from "./pages/ToolsPage";
import { DuplicatesPage } from "./pages/DuplicatesPage";
import { SimulatorPage } from "./pages/SimulatorPage";
import { HealthPage } from "./pages/HealthPage";
import { ExecutionsPage } from "./pages/ExecutionsPage";
import { PolicyPage } from "./pages/PolicyPage";

const PAGES: Record<string, ReactNode> = {
  "/servers": <ServersPage />,
  "/tools": <ToolsPage />,
  "/duplicates": <DuplicatesPage />,
  "/simulator": <SimulatorPage />,
  "/health": <HealthPage />,
  "/executions": <ExecutionsPage />,
  "/policy": <PolicyPage />,
};

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/servers" replace /> },
      ...NAV.map((n) => ({ path: n.to.slice(1), element: PAGES[n.to] })),
      { path: "*", element: <Navigate to="/servers" replace /> },
    ],
  },
]);
