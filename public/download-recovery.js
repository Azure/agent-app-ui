/* Only encrypted tickets cross window messaging; exact configured portal origins. */
(() => {
  const button = document.getElementById("recover");
  const status = document.getElementById("recovery-status");
  if (!button || !status) return;
  button.onclick = () => {
    if (!window.opener || window.opener.closed) {
      status.textContent = "This browser cannot notify your portal. Return to your portal, sign in again, and reopen the assistant.";
      return;
    }
    for (const origin of JSON.parse(button.dataset.origins)) {
      window.opener.postMessage({ type: "alz-protected-auth", ticket: button.dataset.ticket }, origin);
    }
    status.textContent = "Return to your portal to complete sign-in. If no control appears, sign in there and reopen the assistant.";
  };
})();
