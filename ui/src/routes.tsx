import { lazy, Suspense, type ReactNode } from "react";
import { createBrowserRouter, Navigate } from "react-router";
import { Layout, NAV } from "./components/Layout";
import { ServersPage } from "./pages/ServersPage";
import { ToolsPage } from "./pages/ToolsPage";
import { DuplicatesPage } from "./pages/DuplicatesPage";
import { LensPage } from "./pages/LensPage";
import { HealthPage } from "./pages/HealthPage";
import { ExecutionsPage } from "./pages/ExecutionsPage";
import { PolicyPage } from "./pages/PolicyPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { SkillSourcesPage } from "./pages/SkillSourcesPage";
import { SkillsPage } from "./pages/SkillsPage";
import { LoadingRow } from "./components/common";

// The three heaviest, least-visited pages load on demand (P-411), keeping the
// first paint's `index` chunk under its budget (scripts/check-bundle.mjs).
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then((m) => ({ default: m.PlaygroundPage })));
const SetupPage = lazy(() => import("./pages/setup/SetupPage").then((m) => ({ default: m.SetupPage })));
const page = (el: ReactNode) => <Suspense fallback={<LoadingRow label="Loading page…" />}>{el}</Suspense>;

const PAGES: Record<string, ReactNode> = {
  "/servers": <ServersPage />,
  "/tools": <ToolsPage />,
  "/duplicates": <DuplicatesPage />,
  "/lens": <LensPage />,
  "/analytics": page(<AnalyticsPage />),
  "/health": <HealthPage />,
  "/executions": <ExecutionsPage />,
  "/policy": <PolicyPage />,
  "/playground": page(<PlaygroundPage />),
  "/approvals": <ApprovalsPage />,
  "/skill-sources": <SkillSourcesPage />,
  "/skills": <SkillsPage />,
};

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/servers" replace /> },
      // The free-text-identity simulator was replaced by the agent lens.
      { path: "setup", element: page(<SetupPage />) },
      { path: "simulator", element: <Navigate to="/lens" replace /> },
      ...NAV.map((n) => ({ path: n.to.slice(1), element: PAGES[n.to] })),
      { path: "*", element: <Navigate to="/servers" replace /> },
    ],
  },
]);
