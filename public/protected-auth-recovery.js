/* Existing Chainlit 2.12 call_fn protocol; no tokens/claims in events or storage. */
(function (root) {
  "use strict";
  root.installProtectedAuthRecovery = function ({
    msalInstance, account, tenantId, scopes, chainlitServer, statusElement, widgetConfig = {}
  }) {
    const origin = new URL(chainlitServer).origin;
    const msalConfig = msalInstance.getConfiguration();
    const cache = msalConfig.cache;
    const sameAccount = () => msalInstance.getActiveAccount()?.homeAccountId === account.homeAccountId;
    if (origin !== chainlitServer.replace(/\/$/, "") || !chainlitServer.startsWith("https://")
        || !account || !Array.isArray(scopes) || !scopes.length
        || !/^[0-9a-f-]{36}$/i.test(tenantId) || !statusElement
        || cache.cacheLocation !== "memoryStorage" || cache.temporaryCacheLocation !== "memoryStorage"
        || cache.storeAuthStateInCookie === true || msalConfig.system?.loggerOptions?.piiLoggingEnabled === true
        || "accessToken" in widgetConfig || !sameAccount()) {
      throw new Error("Protected sign-in helper configuration is invalid.");
    }
    let active = true;
    let pending = false;
    const unavailable = () => {
      statusElement.hidden = false;
      statusElement.textContent = "Sign-in could not complete. Sign in through your portal and reopen the assistant.";
    };
    async function prepare(ticket) {
      if (!active || pending || typeof ticket !== "string" || ticket.length > 48000) return false;
      pending = true;
      try {
        const request = (operation, accessToken) => fetch(origin + "/copilot/auth/recovery", {
          method: "POST", credentials: "include", cache: "no-store", redirect: "error",
          headers: { "Content-Type": "application/json",
            ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
          body: JSON.stringify({ ticket, operation }),
        });
        const response = await request("challenge");
        if (!response.ok) throw new Error("recovery unavailable");
        const { claims } = await response.json();
        if (claims !== null && (typeof claims !== "string" || new TextEncoder().encode(claims).length > 8192))
          throw new Error("recovery unavailable");
        if (!active) return false;
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Continue sign-in";
        statusElement.replaceChildren(button);
        statusElement.hidden = false;
        button.onclick = async () => {
          if (!active || !sameAccount()) { unavailable(); return; }
          button.disabled = true;
          try {
            // Directly inside a real portal click: popup is not opened after a network await.
            const result = await msalInstance.acquireTokenPopup({
              account, scopes: [...scopes],
              authority: `https://login.microsoftonline.com/${tenantId}`,
              ...(claims ? { claims } : {}),
            });
            if (!active || !sameAccount() || !result.accessToken || result.account?.homeAccountId !== account.homeAccountId)
              throw new Error("account mismatch");
            const completed = await request("complete", result.accessToken);
            if (!completed.ok || !active) throw new Error("recovery unavailable");
            root.unmountChainlitWidget();
            root.clearChainlitCopilotThreadId();
            root.mountChainlitWidget({ ...widgetConfig, chainlitServer: origin });
            statusElement.textContent = "Sign-in complete. Submit a new request; the failed request was not replayed.";
          } catch { unavailable(); }
          finally { pending = false; }
        };
        return true;
      } catch { unavailable(); pending = false; return false; }
    }
    const call = async (event) => {
      if (event.detail?.name !== "protectedAuthRecovery") return;
      const ready = await prepare(event.detail.args?.ticket);
      event.detail.callback({ status: ready ? "ready" : "unavailable" });
    };
    const message = (event) => {
      if (event.origin === origin && event.source !== root && event.data?.type === "alz-protected-auth")
        void prepare(event.data.ticket);
    };
    root.addEventListener("chainlit-call-fn", call);
    root.addEventListener("message", message);
    // Call before portal logout/account switching. The backend independently
    // verifies current principal/session during challenge and completion.
    return () => {
      active = false;
      pending = false;
      statusElement.replaceChildren();
      root.removeEventListener("chainlit-call-fn", call);
      root.removeEventListener("message", message);
    };
  };
})(window);
