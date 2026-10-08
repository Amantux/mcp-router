import type { ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import { Layout, NAV } from "./components/Layout";
import { ServersPage } from "./pages/ServersPage";
import { ToolsPage } from "./pages/ToolsPage";

const PAGES: Record<string, ReactNode> = {
  "/servers": <ServersPage />,
  "/tools": <ToolsPage />,
};

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/servers" replace /> },
      ...NAV.map((n) => ({ path: n.to.slice(1), element: PAGES[n.to] ?? <h1>{n.label}</h1> })),
      { path: "*", element: <Navigate to="/servers" replace /> },
    ],
  },
]);
