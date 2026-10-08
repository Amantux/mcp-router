import { NavLink, Outlet } from "react-router";
import { makeStyles, mergeClasses, Text, tokens } from "@fluentui/react-components";
import {
  ServerRegular,
  WrenchRegular,
  BranchForkRegular,
  DirectionsRegular,
  HeartPulseRegular,
  HistoryRegular,
  ShieldKeyholeRegular,
} from "@fluentui/react-icons";
import type { ReactNode } from "react";
import { NotificationStack } from "./Notifications";

export const NAV: { to: string; label: string; icon: ReactNode }[] = [
  { to: "/servers", label: "Servers", icon: <ServerRegular /> },
  { to: "/tools", label: "Tools", icon: <WrenchRegular /> },
  { to: "/duplicates", label: "Duplicates", icon: <BranchForkRegular /> },
  { to: "/simulator", label: "Routing simulator", icon: <DirectionsRegular /> },
  { to: "/health", label: "Models & health", icon: <HeartPulseRegular /> },
  { to: "/executions", label: "Execution history", icon: <HistoryRegular /> },
  { to: "/policy", label: "Policy", icon: <ShieldKeyholeRegular /> },
];

const useStyles = makeStyles({
  shell: { display: "grid", gridTemplateColumns: "200px 1fr", height: "100%", backgroundColor: tokens.colorNeutralBackground2 },
  nav: {
    display: "flex",
    flexDirection: "column",
    gap: "2px",
    padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalS}`,
    borderRight: `1px solid ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  brand: { padding: `${tokens.spacingVerticalS} ${tokens.spacingHorizontalS} ${tokens.spacingVerticalM}` },
  link: {
    display: "flex",
    alignItems: "center",
    gap: tokens.spacingHorizontalS,
    padding: `6px ${tokens.spacingHorizontalS}`,
    borderRadius: tokens.borderRadiusMedium,
    color: tokens.colorNeutralForeground2,
    textDecoration: "none",
    fontSize: tokens.fontSizeBase300,
    ":hover": { backgroundColor: tokens.colorNeutralBackground1Hover, color: tokens.colorNeutralForeground1 },
  },
  active: {
    backgroundColor: tokens.colorNeutralBackground1Selected,
    color: tokens.colorNeutralForeground1,
    fontWeight: tokens.fontWeightSemibold,
  },
  main: { overflow: "auto", padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalL}`, minWidth: 0 },
});

export function Layout() {
  const s = useStyles();
  return (
    <div className={s.shell}>
      <nav className={s.nav} aria-label="Primary">
        <Text weight="semibold" size={400} className={s.brand}>
          MCP Router
        </Text>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => mergeClasses(s.link, isActive && s.active)}>
            {n.icon}
            {n.label}
          </NavLink>
        ))}
      </nav>
      <main className={s.main}>
        <NotificationStack />
        <Outlet />
      </main>
    </div>
  );
}
