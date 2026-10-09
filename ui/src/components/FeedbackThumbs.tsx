import { useState } from "react";
import { Button, Tooltip } from "@fluentui/react-components";
import { ThumbDislikeFilled, ThumbDislikeRegular, ThumbLikeFilled, ThumbLikeRegular } from "@fluentui/react-icons";
import { postRouteFeedback } from "../api/client";
import type { FeedbackItem } from "../api/types";
import { useNotify } from "./Notifications";

/** Feedback target: a tool or skill by id (preferred) or by name, scoped to one routing decision. */
export interface FeedbackTarget {
  kind: "tool" | "skill";
  id?: string | null;
  name?: string | null;
}

/** "skill:<id>" ids are skills; anything else is a tool. */
export function kindOfId(id: string): "tool" | "skill" {
  return id.startsWith("skill:") ? "skill" : "tool";
}

/**
 * Human thumbs up/down for one tool/skill on one routing decision. Optimistic:
 * the choice shows immediately and rolls back (with a persistent error toast) if
 * the POST fails. Disabled with a tooltip when the row has no routeRequestId.
 */
export function FeedbackThumbs({ routeRequestId, target, label }: { routeRequestId: string | null | undefined; target: FeedbackTarget; label: string }) {
  const notify = useNotify();
  const [vote, setVote] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const ref = target.id ? { id: target.id } : target.name ? { name: target.name } : null;
  const disabledWhy = !routeRequestId ? "No routing decision is attributed to this row, so there is nothing to rate." : !ref ? "This row has no tool or skill to rate." : null;

  async function send(helpful: boolean) {
    if (!routeRequestId || !ref || busy) return;
    const prev = vote;
    setVote(helpful);
    setBusy(true);
    const item: FeedbackItem = { kind: target.kind, ...ref, helpful };
    try {
      await postRouteFeedback(routeRequestId, [item]);
    } catch (err) {
      setVote(prev);
      notify.error(`Record feedback for ${label}`, err);
    } finally {
      setBusy(false);
    }
  }

  const up = (
    <Button
      size="small"
      appearance="subtle"
      aria-label={`Helpful: ${label}`}
      aria-pressed={vote === true}
      disabled={!!disabledWhy || busy}
      icon={vote === true ? <ThumbLikeFilled /> : <ThumbLikeRegular />}
      onClick={() => void send(true)}
    />
  );
  const down = (
    <Button
      size="small"
      appearance="subtle"
      aria-label={`Not helpful: ${label}`}
      aria-pressed={vote === false}
      disabled={!!disabledWhy || busy}
      icon={vote === false ? <ThumbDislikeFilled /> : <ThumbDislikeRegular />}
      onClick={() => void send(false)}
    />
  );
  if (disabledWhy)
    return (
      <Tooltip content={disabledWhy} relationship="description">
        {/* span: disabled buttons swallow pointer events, the tooltip needs a live target */}
        <span data-testid="feedback-disabled">
          {up}
          {down}
        </span>
      </Tooltip>
    );
  return (
    <span>
      {up}
      {down}
    </span>
  );
}
