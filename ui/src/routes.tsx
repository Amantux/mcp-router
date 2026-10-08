import { createBrowserRouter, Navigate } from "react-router";
import { Layout, NAV } from "./components/Layout";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/servers" replace /> },
      ...NAV.map((n) => ({ path: n.to.slice(1), element: <h1>{n.label}</h1> })),
    ],
  },
]);
