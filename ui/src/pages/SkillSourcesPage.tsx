import { useState } from "react";
import {
  Badge,
  Button,
  Caption1,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Radio,
  RadioGroup,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@fluentui/react-components";
import { AddRegular, ArrowSyncRegular, BookRegular } from "@fluentui/react-icons";
import { createSkillSource, listSkillSources, setSkillSourceEnabled, syncSkillSource } from "../api/client";
import type { SkillSource, SkillSourceKind, SyncReport } from "../api/types";
import { ConfirmDialog, EmptyState, ErrorState, fmtInt, fmtTime, LoadingRow, PageHeader, useCommonStyles } from "../components/common";
import { useNotify } from "../components/Notifications";
import { useLoader } from "../hooks/useLoader";

/** Client-side guard only; the server re-validates. Git sources must be https (no ssh/http/file). */
export function validateSourceLocation(kind: SkillSourceKind, location: string): string | null {
  const v = location.trim();
  if (!v) return kind === "git" ? "Enter the repository's https URL." : "Enter the directory's absolute path.";
  if (kind === "directory") return v.startsWith("/") ? null : "Use an absolute path, starting with /.";
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    return "That is not a valid URL. Use https://host/owner/repo.";
  }
  if (url.protocol !== "https:") return "Only https:// git URLs are accepted. Use the repository's https clone URL.";
  if (url.username || url.password) return "Remove the credentials from the URL; private repos are not supported here.";
  return null;
}

/** Git URL → host; path → last two segments. */
export function locationLabel(s: SkillSource): string {
  if (s.kind === "git") {
    try {
      return new URL(s.location).host + (s.gitRef ? ` @ ${s.gitRef}` : "");
    } catch {
      return s.location;
    }
  }
  const parts = s.location.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : s.location;
}

function AddSourceDialog({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: () => void }) {
  const notify = useNotify();
  const [kind, setKind] = useState<SkillSourceKind>("directory");
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [gitRef, setGitRef] = useState("");
  const [touched, setTouched] = useState(false);
  const [pending, setPending] = useState(false);
  const locError = validateSourceLocation(kind, location);
  const nameError = name.trim() ? null : "Enter a name.";
  // Every close (added or cancelled) starts the next open blank, errors hidden.
  const close = () => {
    setKind("directory");
    setName("");
    setLocation("");
    setGitRef("");
    setTouched(false);
    onClose();
  };

  const submit = async () => {
    setTouched(true);
    if (locError || nameError) return;
    setPending(true);
    try {
      await createSkillSource({ name: name.trim(), kind, location: location.trim(), ...(kind === "git" && gitRef.trim() ? { gitRef: gitRef.trim() } : {}) });
      notify.success(`Added skill source “${name.trim()}”. Sync it to catalog its skills.`);
      onAdded();
      close();
    } catch (e) {
      notify.error(`Add skill source “${name.trim()}”`, e);
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(_, d) => !d.open && !pending && close()}>
      <DialogSurface>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogBody>
            <DialogTitle>Add skill source</DialogTitle>
            <DialogContent style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <Field label="Name" validationMessage={touched ? nameError : undefined}>
                <Input value={name} onChange={(_, d) => setName(d.value)} />
              </Field>
              <Field label="Kind">
                <RadioGroup layout="horizontal" value={kind} onChange={(_, d) => setKind(d.value as SkillSourceKind)}>
                  <Radio value="directory" label="Directory" />
                  <Radio value="git" label="Git (https)" />
                </RadioGroup>
              </Field>
              <Field
                label={kind === "git" ? "Repository URL" : "Directory path"}
                hint={kind === "git" ? "https only, e.g. https://github.com/org/skills" : "Absolute path on the router host"}
                validationMessage={touched ? locError : undefined}
              >
                <Input value={location} onChange={(_, d) => setLocation(d.value)} />
              </Field>
              {kind === "git" && (
                <Field label="Ref" hint="Branch, tag or commit. Leave blank for the default branch.">
                  <Input value={gitRef} onChange={(_, d) => setGitRef(d.value)} />
                </Field>
              )}
            </DialogContent>
            <DialogActions>
              <Button onClick={close} disabled={pending}>
                Cancel
              </Button>
              <Button appearance="primary" type="submit" disabled={pending}>
                {pending ? "Adding…" : "Add source"}
              </Button>
            </DialogActions>
          </DialogBody>
        </form>
      </DialogSurface>
    </Dialog>
  );
}

export function SyncReportView({ report }: { report: SyncReport }) {
  const c = useCommonStyles();
  return (
    <div aria-label="Sync report">
      <Caption1>
        {fmtInt(report.added)} added · {fmtInt(report.changed)} changed · {fmtInt(report.removed)} removed · {fmtInt(report.skipped.length)} skipped
      </Caption1>
      {report.skipped.length > 0 && (
        <Table size="extra-small" aria-label="Skipped skills">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Path</TableHeaderCell>
              <TableHeaderCell>Reason</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {report.skipped.map((s) => (
              <TableRow key={s.path}>
                <TableCell className={c.mono}>{s.path}</TableCell>
                <TableCell>{s.reason}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

export function SkillSourcesPage() {
  const c = useCommonStyles();
  const notify = useNotify();
  const sources = useLoader("Load skill sources", (sig) => listSkillSources(sig), []);
  const [addOpen, setAddOpen] = useState(false);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, SyncReport>>({});
  const [toggling, setToggling] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<SkillSource | null>(null);

  const doSync = async (s: SkillSource) => {
    setSyncing(s.id);
    try {
      const r = await syncSkillSource(s.id);
      setReports((m) => ({ ...m, [s.id]: r }));
      notify.success(`Synced “${s.name}” — ${r.added} added, ${r.changed} changed, ${r.removed} removed`);
      sources.refresh();
    } catch (e) {
      notify.error(`Sync skill source “${s.name}”`, e);
    } finally {
      setSyncing(null);
    }
  };

  const doToggle = async (s: SkillSource, enabled: boolean) => {
    setToggling(s.id);
    try {
      await setSkillSourceEnabled(s.id, enabled);
      notify.success(`${enabled ? "Enabled" : "Disabled"} skill source “${s.name}”`);
      sources.refresh();
    } catch (e) {
      notify.error(`${enabled ? "Enable" : "Disable"} skill source “${s.name}”`, e);
    } finally {
      setToggling(null);
      setConfirmDisable(null);
    }
  };

  const list = sources.data ?? [];
  return (
    <>
      <PageHeader
        title="Skill sources"
        meta={sources.data && <Caption1 className={c.muted}>{fmtInt(list.length)} registered</Caption1>}
        actions={
          <Button appearance="primary" icon={<AddRegular />} onClick={() => setAddOpen(true)}>
            Add source
          </Button>
        }
      />
      {sources.loading && !sources.data ? (
        <LoadingRow label="Loading skill sources…" />
      ) : sources.failed && !sources.data ? (
        <ErrorState what="Skill sources" onRetry={sources.reload} />
      ) : list.length === 0 && !sources.failed ? (
        <EmptyState
          icon={<BookRegular />}
          title="No skill sources yet"
          body="A skill source is a directory or https git repository of Agent Skills (folders with a SKILL.md). Add one, then sync it to catalog its skills."
          action={<Button onClick={() => setAddOpen(true)}>Add source</Button>}
        />
      ) : (
        <Table size="small" aria-label="Skill sources">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Name</TableHeaderCell>
              <TableHeaderCell>Kind</TableHeaderCell>
              <TableHeaderCell>Location</TableHeaderCell>
              <TableHeaderCell>Status</TableHeaderCell>
              <TableHeaderCell className={c.num}>Skills</TableHeaderCell>
              <TableHeaderCell>Last synced</TableHeaderCell>
              <TableHeaderCell>Enabled</TableHeaderCell>
              <TableHeaderCell aria-label="Actions" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.map((s) => (
              <TableRow key={s.id}>
                <TableCell>
                  <strong>{s.name}</strong>
                  {reports[s.id] && <SyncReportView report={reports[s.id]} />}
                </TableCell>
                <TableCell>
                  <Badge appearance="outline">{s.kind}</Badge>
                </TableCell>
                <TableCell className={c.mono} title={s.location}>
                  {locationLabel(s)}
                </TableCell>
                <TableCell>{s.status ?? "—"}</TableCell>
                <TableCell className={c.num}>{fmtInt(s.skillCount)}</TableCell>
                <TableCell>
                  {fmtTime(s.lastSyncedAt)}
                  {s.lastCommit && <Caption1 className={`${c.muted} ${c.mono}`}> {s.lastCommit.slice(0, 7)}</Caption1>}
                </TableCell>
                <TableCell>
                  <Switch
                    aria-label={`${s.enabled ? "Disable" : "Enable"} ${s.name}`}
                    checked={s.enabled}
                    disabled={toggling === s.id}
                    onChange={(_, d) => (d.checked ? void doToggle(s, true) : setConfirmDisable(s))}
                  />
                </TableCell>
                <TableCell>
                  <Button size="small" icon={<ArrowSyncRegular />} disabled={syncing === s.id} onClick={() => void doSync(s)}>
                    {syncing === s.id ? "Syncing…" : "Sync"}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <AddSourceDialog open={addOpen} onClose={() => setAddOpen(false)} onAdded={() => sources.refresh()} />
      <ConfirmDialog
        open={confirmDisable !== null}
        title={`Disable skill source “${confirmDisable?.name ?? ""}”?`}
        body={<>Its {confirmDisable?.skillCount != null ? `${fmtInt(confirmDisable.skillCount)} ` : ""}skills will stop being routed or exposed to agents until you re-enable it. Nothing is deleted.</>}
        confirmLabel="Disable source"
        pendingLabel="Disabling…"
        pending={toggling !== null}
        onConfirm={() => confirmDisable && void doToggle(confirmDisable, false)}
        onCancel={() => setConfirmDisable(null)}
      />
    </>
  );
}
