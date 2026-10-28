import React from "react";
import { Button } from "@/components/ui/button";

export default function AuthRecovery(props) {
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  async function notifyPortal() {
    setBusy(true);
    try {
      const result = await callAction({
        name: "continue_protected_signin",
        payload: { ticket: props.ticket },
      });
      setStatus(result?.status === "ready"
        ? "Use Continue sign-in in your portal. Then start a new request."
        : "Sign-in could not start. Return to your portal, sign in again, and reopen the assistant.");
    } catch {
      setStatus("Sign-in could not start. Return to your portal and reopen the assistant.");
    } finally { setBusy(false); }
  }
  return <section aria-label="Protected access sign-in">
    {props.href
      ? <Button asChild><a href={props.href} rel="noreferrer">Continue sign-in</a></Button>
      : <Button onClick={notifyPortal} disabled={busy}>Continue sign-in</Button>}
    <p role="status" aria-live="polite">{status}</p>
  </section>;
}
