import { useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  DrawerBody,
  DrawerHeader,
  DrawerHeaderTitle,
  Input,
  OverlayDrawer,
  Select,
  Tab,
  TabList,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@fluentui/react-components";
import { ArrowDownloadRegular, BookRegular, DismissRegular, FilterDismissRegular } from "@fluentui/react-icons";
import { Link } from "react-router";
import { downloadSkillBundle, getSkill, getSkillBody, listPrincipals, listSkills, listSkillSources, listSkillVersions, updateSkillClassification } from "../api/client";
import { ClassificationEditor } from "./ClassificationEditor";
import { ToolFunnelPanel } from "./ToolDetailDrawer";
import { DOMAINS, OPERATIONS, type Operation, type Skill, type SkillDetail } from "../api/types";
import { EmptyState, ErrorState, fmtInt, fmtTime, LoadingRow, OperationBadge, PageHeader, Pager, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useDebounced } from "../hooks/useDebounced";
import { useLoader } from "../hooks/useLoader";

const PAGE_SIZE = 50;
const FLAG_LABEL: Record<string, string> = { secret_like: "secret-like", body_truncated: "body truncated", oversize: "oversize", resource_oversize: "resource oversize" };

export function FlagChips({ flags }: { flags?: string[] }) {
  if (!flags?.length) return null;
  return (
    <>
      {flags.map((f) => (
        <Badge key={f} size="small" appearance="tint" color={f === "secret_like" ? "danger" : "warning"} style={{ marginRight: 4 }}>
          {FLAG_LABEL[f] ?? f}
        </Badge>
      ))}
    </>
  );
}

/** Untrusted SKILL.md body: React text node inside <pre> — never parsed as HTML or Markdown. */
export function SkillBodyText({ body }: { body: string }) {
  return (
    <pre aria-label="Skill body" style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: 12, margin: 0 }}>
      {body}
    </pre>
  );
}

type Tri = "" | "true" | "false";
const tri = (v: Tri) => (v === "" ? undefined : v === "true");

export function SkillDrawer({ skillId, onClose }: { skillId: string | null; onClose: () => void }) {
  return (
    <OverlayDrawer open={skillId !== null} position="end" size="large" onOpenChange={(_, o) => !o.open && onClose()}>
      {/* Keyed on the id: a different skill never shows the previous one's data or tab. */}
      {skillId !== null && <SkillDrawerContent key={skillId} skillId={skillId} onClose={onClose} />}
    </OverlayDrawer>
  );
}

function SkillDrawerContent({ skillId, onClose }: { skillId: string; onClose: () => void }) {
  const c = useCommonStyles();
  const [tab, setTab] = useState("overview");
  const detail = useLoader<SkillDetail>("Load skill", (sig) => getSkill(skillId, sig), [skillId]);
  const body = useLoader<string | null>("Load skill body", (sig) => (tab === "body" ? getSkillBody(skillId, sig) : Promise.resolve(null)), [skillId, tab]);
  const versions = useLoader("Load skill versions", (sig) => (tab === "versions" ? listSkillVersions(skillId, sig) : Promise.resolve(null)), [skillId, tab]);
  const d = detail.data;
  return (
    <>
      <DrawerHeader>
        <DrawerHeaderTitle action={<Button appearance="subtle" aria-label="Close" icon={<DismissRegular />} onClick={onClose} />}>{d?.name ?? "Skill"}</DrawerHeaderTitle>
        {d && <Link to={`/playground?skill=${encodeURIComponent(d.id)}`}>Try activation</Link>}
      </DrawerHeader>
      <DrawerBody>
        <TabList selectedValue={tab} onTabSelect={(_, t) => setTab(String(t.value))}>
          <Tab value="overview">Overview</Tab>
          <Tab value="body">Body</Tab>
          <Tab value="resources">Resources</Tab>
          <Tab value="versions">Versions</Tab>
          <Tab value="classification">Classification</Tab>
          <Tab value="funnel">Funnel</Tab>
        </TabList>
        {!d ? (
          detail.failed ? (
            <Caption1>Skill details couldn't be loaded.</Caption1>
          ) : (
            <LoadingRow label="Loading skill…" />
          )
        ) : tab === "overview" ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, paddingTop: 12 }}>
            <span>{d.description}</span>
            <FlagChips flags={d.ingestFlags} />
            <Caption1>License: {d.license ?? "—"} · Compatibility: {d.compatibility ?? "—"}</Caption1>
            <div>
              <Caption1>Allowed tools: </Caption1>
              {d.allowedTools?.length ? d.allowedTools.map((t) => <Badge key={t} appearance="outline" style={{ marginRight: 4 }}>{t}</Badge>) : "—"}
            </div>
            {d.metadata && <pre className={c.mono}>{JSON.stringify(d.metadata, null, 2)}</pre>}
          </div>
        ) : tab === "classification" ? (
          <ClassificationEditor<Skill> key={d.id} tool={d} save={updateSkillClassification} onSaved={() => detail.refresh()} />
        ) : tab === "funnel" ? (
          <ToolFunnelPanel toolId={d.id} kind="skill" />
        ) : tab === "body" ? (
          <div style={{ paddingTop: 12 }}>
            <Caption1 className={c.muted}>Shown as plain text. Skill bodies come from third-party sources, so Markdown and HTML are not rendered here.</Caption1>
            {body.data == null ? body.failed ? <Caption1>The body couldn't be loaded.</Caption1> : <LoadingRow label="Loading body…" /> : <SkillBodyText body={body.data} />}
          </div>
        ) : tab === "resources" ? (
          <Table size="extra-small" aria-label="Resources">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Kind</TableHeaderCell>
                <TableHeaderCell>Path</TableHeaderCell>
                <TableHeaderCell className={c.num}>Bytes</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...(d.resourceManifest ?? [])]
                .sort((a, b) => a.kind.localeCompare(b.kind) || a.path.localeCompare(b.path))
                .map((r) => (
                  <TableRow key={r.path}>
                    <TableCell>{r.kind}</TableCell>
                    <TableCell className={c.mono}>
                      {r.path} {r.oversize && <Badge size="small" color="warning">oversize</Badge>}
                    </TableCell>
                    <TableCell className={c.num}>{fmtInt(r.size)}</TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        ) : (
          versions.data == null ? (
            versions.failed ? <Caption1>The version history couldn't be loaded.</Caption1> : <LoadingRow label="Loading versions…" />
          ) : versions.data.length === 0 ? (
            <Caption1>No versions recorded yet.</Caption1>
          ) : (
            <ol aria-label="Versions">
              {[...versions.data].reverse().map((v) => (
                <li key={v.version}>
                  v{v.version} · {v.changeKind}{" "}
                  <Caption1 className={c.muted}>
                    {fmtTime(v.recordedAt)} <span className={c.mono}>{v.contentHash.slice(0, 8)}</span>
                  </Caption1>
                </li>
              ))}
            </ol>
          )
        )}
      </DrawerBody>
    </>
  );
}

function BundleDownload() {
  const c = useCommonStyles();
  const notify = useNotify();
  const agents = useLoader("Load agents", (sig) => listPrincipals(sig), []);
  const [agentId, setAgentId] = useState("");
  const [pending, setPending] = useState(false);
  const go = async () => {
    setPending(true);
    try {
      const blob = await downloadSkillBundle(agentId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `skills-${agentId}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      notify.success(`Downloaded the skill bundle for “${agentId}”`);
    } catch (e) {
      notify.error(`Download skill bundle for “${agentId}”`, e);
    } finally {
      setPending(false);
    }
  };
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <Select aria-label="Bundle agent" value={agentId} onChange={(_, d) => setAgentId(d.value)}>
        <option value="">Choose agent…</option>
        {(agents.data ?? []).map((p) => (
          <option key={p.id} value={p.agentId}>{p.agentId}</option>
        ))}
      </Select>
      <Button icon={<ArrowDownloadRegular />} disabled={!agentId || pending} onClick={() => void go()}>
        {pending ? "Downloading…" : "Download bundle"}
      </Button>
      <Caption1 className={c.muted}>Zip of the skills this agent may use. Unzip into ~/.claude/skills (or the client's skills folder) to sync them.</Caption1>
    </div>
  );
}

export function SkillsPage() {
  const c = useCommonStyles();
  const [f, setF] = useState({ q: "", domain: "", operation: "" as Operation | "", sourceId: "", enabled: "" as Tri, available: "" as Tri, reviewed: "" as Tri, hasScripts: "" as Tri });
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const q = useDebounced(f.q, 200);
  const sources = useLoader("Load skill sources", (sig) => listSkillSources(sig), []);
  const skills = useLoader(
    "Load skills",
    (sig) => listSkills({ q, domain: f.domain, operation: f.operation, sourceId: f.sourceId, enabled: tri(f.enabled), available: tri(f.available), reviewed: tri(f.reviewed), hasScripts: tri(f.hasScripts), limit: PAGE_SIZE, offset }, sig),
    [q, f.domain, f.operation, f.sourceId, f.enabled, f.available, f.reviewed, f.hasScripts, offset],
  );
  const set = (patch: Partial<typeof f>) => {
    setF((x) => ({ ...x, ...patch }));
    setOffset(0);
  };
  const filtered = Object.values(f).some((v) => v !== "");
  const page = skills.data;
  const triSelect = (label: string, key: "enabled" | "available" | "reviewed" | "hasScripts") => (
    <Select aria-label={label} value={f[key]} onChange={(_, d) => set({ [key]: d.value as Tri })}>
      <option value="">{label}: any</option>
      <option value="true">{label}: yes</option>
      <option value="false">{label}: no</option>
    </Select>
  );
  return (
    <>
      <PageHeader title="Skills" meta={page && <Caption1 className={c.muted}>{fmtInt(page.total)} skills</Caption1>} actions={<BundleDownload />} />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <Input aria-label="Search skills" placeholder="Search skills" value={f.q} onChange={(_, d) => set({ q: d.value })} />
        <Select aria-label="Domain" value={f.domain} onChange={(_, d) => set({ domain: d.value })}>
          <option value="">Domain: any</option>
          {DOMAINS.map((x) => <option key={x} value={x}>{x}</option>)}
        </Select>
        <Select aria-label="Risk class" value={f.operation} onChange={(_, d) => set({ operation: d.value as Operation | "" })}>
          <option value="">Risk class: any</option>
          {OPERATIONS.map((x) => <option key={x} value={x}>{x}</option>)}
        </Select>
        <Select aria-label="Source" value={f.sourceId} onChange={(_, d) => set({ sourceId: d.value })}>
          <option value="">Source: any</option>
          {(sources.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
        {triSelect("Enabled", "enabled")}
        {triSelect("Available", "available")}
        {triSelect("Reviewed", "reviewed")}
        {triSelect("Scripts", "hasScripts")}
        {filtered && (
          <Button icon={<FilterDismissRegular />} onClick={() => set({ q: "", domain: "", operation: "", sourceId: "", enabled: "", available: "", reviewed: "", hasScripts: "" })}>
            Clear filters
          </Button>
        )}
      </div>
      {skills.loading && !page ? (
        <LoadingRow label="Loading skills…" />
      ) : skills.failed && !skills.data ? (
        <ErrorState what="Skills" onRetry={skills.reload} />
      ) : page && page.items.length === 0 && !skills.failed ? (
        filtered ? (
          <EmptyState icon={<BookRegular />} title="No skills match these filters" body="Loosen or clear the filters to see more skills." />
        ) : (
          <EmptyState
            icon={<BookRegular />}
            title="No skills cataloged yet"
            body="Skills are folders with a SKILL.md, synced from a skill source. Add a directory or https git source, then sync it."
            action={<Link to="/skill-sources">Go to skill sources</Link>}
          />
        )
      ) : (
        <>
          <Table size="small" aria-label="Skills">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Name</TableHeaderCell>
                <TableHeaderCell>Source</TableHeaderCell>
                <TableHeaderCell>Domain</TableHeaderCell>
                <TableHeaderCell>Risk</TableHeaderCell>
                <TableHeaderCell className={c.num}>Body tokens</TableHeaderCell>
                <TableHeaderCell className={c.num}>Activations</TableHeaderCell>
                <TableHeaderCell>Flags</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(page?.items ?? []).map((s: Skill) => (
                <TableRow
                  key={s.id}
                  tabIndex={0}
                  onClick={() => setOpen(s.id)}
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
                    e.preventDefault(); // Space would scroll the page
                    setOpen(s.id);
                  }}
                  style={{ cursor: "pointer" }}
                >
                  <TableCell>
                    <strong>{s.name}</strong>
                    {s.hasScripts && <Badge size="small" appearance="outline" style={{ marginLeft: 4 }}>scripts</Badge>}
                    {!s.enabled && <Caption1 className={c.muted}> disabled</Caption1>}
                  </TableCell>
                  <TableCell>{s.sourceName ?? s.sourceId}</TableCell>
                  <TableCell>{s.domain ?? "—"}</TableCell>
                  <TableCell>
                    <OperationBadge op={s.operation} />
                  </TableCell>
                  <TableCell className={c.num}>{fmtInt(s.bodyTokensEst)}</TableCell>
                  <TableCell className={c.num}>{fmtInt(s.activationCount)}</TableCell>
                  <TableCell>
                    <FlagChips flags={s.ingestFlags} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {page && <Pager offset={page.offset} limit={PAGE_SIZE} total={page.total} onChange={setOffset} />}
        </>
      )}
      <SkillDrawer skillId={open} onClose={() => setOpen(null)} />
    </>
  );
}
