import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarGroup,
  MessageBarTitle,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { DismissRegular } from "@fluentui/react-icons";
import { describeError, isAdminAuthError } from "../api/client";

interface Note {
  id: number;
  intent: "error" | "success";
  title: string;
  body: string;
}

interface NotifyApi {
  /** Persistent until dismissed. `what` = the action that failed, e.g. "Load servers". */
  error: (what: string, err: unknown) => void;
  /** Auto-dismisses after 4s. */
  success: (message: string) => void;
}

const Ctx = createContext<NotifyApi | null>(null);
const SUCCESS_MS = 4000;

const useStyles = makeStyles({
  group: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
});

export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [notes, setNotes] = useState<Note[]>([]);
  const nextId = useRef(1);
  const dismiss = useCallback((id: number) => setNotes((n) => n.filter((x) => x.id !== id)), []);
  // Auto-dismiss timers die with the provider, so none fires after unmount.
  const timers = useRef(new Set<number>());
  useEffect(() => {
    const live = timers.current;
    return () => {
      live.forEach((t) => window.clearTimeout(t));
      live.clear();
    };
  }, []);
  // Exactly one stack renders per provider, wherever several are mounted.
  const [stackOwner, setStackOwner] = useState<string | null>(null);
  const claimStack = useCallback((id: string) => setStackOwner((o) => o ?? id), []);
  const releaseStack = useCallback((id: string) => setStackOwner((o) => (o === id ? null : o)), []);

  const api = useMemo<NotifyApi>(
    () => ({
      error: (what, err) => {
        // Credential refusals are explained once by the global "not connected" bar (AuthBanner).
        if (isAdminAuthError(err)) return;
        const { status, advice } = describeError(err);
        const title = `${what} failed${status ? ` (${status})` : ""}`;
        const id = nextId.current++;
        // Polling must not stack identical bars: skip if this exact error is already showing.
        setNotes((n) =>
          n.some((x) => x.intent === "error" && x.title === title && x.body === advice)
            ? n
            : [...n, { id, intent: "error", title, body: advice }],
        );
      },
      success: (message) => {
        const id = nextId.current++;
        setNotes((n) => [...n, { id, intent: "success", title: message, body: "" }]);
        const t = window.setTimeout(() => {
          timers.current.delete(t);
          dismiss(id);
        }, SUCCESS_MS);
        timers.current.add(t);
      },
    }),
    [dismiss],
  );

  return (
    <Ctx.Provider value={api}>
      <NotesContext.Provider value={{ notes, dismiss, stackOwner, claimStack, releaseStack }}>{children}</NotesContext.Provider>
    </Ctx.Provider>
  );
}

const NotesContext = createContext<{
  notes: Note[];
  dismiss: (id: number) => void;
  stackOwner: string | null;
  claimStack: (id: string) => void;
  releaseStack: (id: string) => void;
}>({ notes: [], dismiss: () => {}, stackOwner: null, claimStack: () => {}, releaseStack: () => {} });

/**
 * Renders the notification stack; placed once at the top of the content column. If
 * more than one is mounted under a provider, only the first renders, so a bar never
 * shows twice (and tests see the same single stack as production).
 */
export function NotificationStack() {
  const { notes, dismiss, stackOwner, claimStack, releaseStack } = useContext(NotesContext);
  const styles = useStyles();
  const me = useId();
  useEffect(() => {
    claimStack(me);
    return () => releaseStack(me);
  }, [me, claimStack, releaseStack]);
  if (stackOwner !== me || notes.length === 0) return null;
  return (
    <MessageBarGroup className={styles.group} animate="exit-only">
      {notes.map((n) => (
        <MessageBar key={n.id} intent={n.intent} layout="multiline">
          <MessageBarBody>
            <MessageBarTitle>{n.title}</MessageBarTitle>
            {n.body}
          </MessageBarBody>
          <MessageBarActions
            containerAction={
              <Button aria-label="Dismiss" appearance="transparent" icon={<DismissRegular />} onClick={() => dismiss(n.id)} />
            }
          />
        </MessageBar>
      ))}
    </MessageBarGroup>
  );
}

export function useNotify(): NotifyApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useNotify outside NotificationsProvider");
  return ctx;
}
