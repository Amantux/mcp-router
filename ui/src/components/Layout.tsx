import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { getSetupStatus } from "../api/client";
import { claimRedirect, redirectClaimed } from "../pages/setup/redirect";
import { Button, makeStyles, mergeClasses, Text, tokens } from "@fluentui/react-components";
import {
  ServerRegular,
  LibraryRegular,
  BookRegular,
  WrenchRegular,
  BranchForkRegular,
  DirectionsRegular,
  HeartPulseRegular,
  HistoryRegular,
  ShieldKeyholeRegular,
  SettingsRegular,
  PlayCircleRegular,
  PersonClockRegular,
  DataBarVerticalRegular,
} from "@fluentui/react-icons";
import type { ReactNode } from "react";
import { NotificationStack } from "./Notifications";
import { AuthBanner, ConnectionIndicator, ConnectPanel } from "./ConnectPanel";
import { useAuth } from "../api/auth";

export const NAV: { to: string; label: string; icon: ReactNode }[] = [
  { to: "/servers", label: "Servers", icon: <ServerRegular /> },
  { to: "/tools", label: "Tools", icon: <WrenchRegular /> },
  { to: "/skill-sources", label: "Skill sources", icon: <LibraryRegular /> },
  { to: "/skills", label: "Skills", icon: <BookRegular /> },
  { to: "/duplicates", label: "Duplicates", icon: <BranchForkRegular /> },
  { to: "/lens", label: "Agent lens", icon: <DirectionsRegular /> },
  { to: "/analytics", label: "Analytics", icon: <DataBarVerticalRegular /> },
  { to: "/playground", label: "Tool playground", icon: <PlayCircleRegular /> },
  { to: "/approvals", label: "Approvals", icon: <PersonClockRegular /> },
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
  spacer: { flex: 1 },
  connect: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS, padding: `0 ${tokens.spacingHorizontalS}` },
  main: { overflow: "auto", padding: `${tokens.spacingVerticalM} ${tokens.spacingHorizontalL}`, minWidth: 0 },
});

export function Layout() {
  const s = useStyles();
  const auth = useAuth();
  const [connectOpen, setConnectOpen] = useState(false);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // First-run: an admin on a fresh install lands in /setup once per session.
  // The claim is taken only once the status answers, so a failed probe (wrong
  // token) retries after reconnecting, and a claimed session never redirects again.
  useEffect(() => {
    if (!auth.hasAdminToken || redirectClaimed()) return;
    let live = true;
    getSetupStatus().then(
      (st) => {
        if (!live || !claimRedirect()) return;
        if (st.needsSetup && pathname !== "/setup") navigate("/setup");
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- probe per credential change, not per navigation
  }, [auth.hasAdminToken, auth.epoch]);
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
        <div className={s.spacer} />
        <div className={s.connect}>
          <ConnectionIndicator />
          <Button appearance="subtle" size="small" icon={<SettingsRegular />} onClick={() => setConnectOpen(true)} style={{ justifyContent: "flex-start" }}>
            Connect
          </Button>
        </div>
      </nav>
      <main className={s.main}>
        <AuthBanner onOpen={() => setConnectOpen(true)} />
        <NotificationStack />
        {/* Remount pages when credentials change so every view refetches under the new identity. */}
        <Outlet key={auth.epoch} />
      </main>
      <ConnectPanel open={connectOpen} onClose={() => setConnectOpen(false)} warnRemount={pathname.startsWith("/setup")} />
    </div>
  );
}
